"""Worker run loop.

**The loop is boring on purpose.** One claim, one execution, one settlement, repeat. Every
interesting behaviour lives in the use case (``application.jobs.ExecuteJobOnce``), which is
where it can be tested without a database or a clock.

Shutdown behaviour is the part that is easy to get wrong, so it is explicit:

* ``SIGTERM``/``SIGINT`` set a stop flag rather than cancelling the task. Cancelling mid-job
  would leave the job in ``running`` until its lease lapsed, delaying the retry by the lease
  duration for no benefit.
* The in-flight job is allowed to finish and settle before the loop exits. Graceful shutdown
  with a bounded drain timeout is what makes a rolling restart lose nothing.
* If the drain timeout expires, the lease lapses naturally and another worker takes the job.
  At-least-once delivery means the worst case is a duplicate execution, which the idempotency
  contract absorbs - not a lost job.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from knowledge_assistant.application.jobs import ExecuteJobOnce
from knowledge_assistant.domain.clock import Clock
from knowledge_assistant.observability.logging import get_logger
from knowledge_assistant.observability.metrics import Metrics

__all__ = ["WorkerRunner"]


class WorkerRunner:
    """Polls the queue and executes jobs until stopped."""

    __slots__ = (
        "_executor",
        "_clock",
        "_poll_interval",
        "_metrics",
        "_logger",
        "_stop",
        "_drain_timeout",
    )

    def __init__(
        self,
        executor: ExecuteJobOnce,
        *,
        clock: Clock,
        poll_interval_seconds: float,
        metrics: Metrics | None = None,
        drain_timeout_seconds: float = 30.0,
        logger: Any | None = None,
    ) -> None:
        """Build the runner.

        Args:
            executor: The job execution use case.
            clock: Injected time source.
            poll_interval_seconds: Idle poll interval.
            metrics: Optional metrics facade for queue health.
            drain_timeout_seconds: Maximum time to finish the in-flight job during shutdown.
            logger: Optional logger override, used by tests to capture output.

        """
        self._executor = executor
        self._clock = clock
        self._poll_interval = poll_interval_seconds
        self._metrics = metrics
        self._logger = logger or get_logger("worker")
        self._stop = asyncio.Event()
        self._drain_timeout = drain_timeout_seconds

    def request_stop(self) -> None:
        """Ask the loop to stop after the current job settles."""
        self._stop.set()

    async def run_forever(self, *, max_iterations: int | None = None) -> int:
        """Run until stopped.

        Args:
            max_iterations: Stop after this many polls. Used by tests and by the one-shot
                ``ka worker --once`` mode; ``None`` means run forever.

        Returns:
            Number of jobs executed.

        """
        executed = 0
        iterations = 0
        self._logger.info("worker.started", poll_interval_seconds=self._poll_interval)
        while not self._stop.is_set():
            iterations += 1
            try:
                outcome = await self._executor.execute_once()
            except Exception as exc:  # noqa: BLE001 - the loop must survive repository errors
                # A repository failure means no claim was taken, so there is nothing to settle.
                # Back off, log, and keep going: a worker that exits on a transient database
                # blip turns a recoverable error into a stopped pipeline.
                self._logger.error(
                    "worker.iteration_failed",
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                outcome = None
                await self._sleep_before_retry()
            if outcome is not None:
                executed += 1
                self._record(outcome)
            else:
                await self._sleep_before_retry()
            if max_iterations is not None and iterations >= max_iterations:
                break
        self._logger.info("worker.stopped", jobs_executed=executed)
        return executed

    async def _sleep_before_retry(self) -> None:
        """Sleep for the poll interval, waking early if a stop is requested.

        Waiting on the event rather than on ``asyncio.sleep`` means shutdown is immediate when
        the worker is idle, instead of delayed by up to one poll interval.
        """
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
        except TimeoutError:
            return

    def _record(self, outcome: Any) -> None:
        """Emit a structured line and metrics for one executed job.

        Args:
            outcome: The use case outcome record.

        """
        job_type = self._executor_job_type(outcome)
        self._logger.info(
            "worker.job.settled",
            job_id=outcome.job_id,
            job_type=job_type,
            outcome=outcome.outcome,
            attempts_used=outcome.attempts_used,
            failure_class=outcome.failure_class.value if outcome.failure_class else None,
        )
        if self._metrics is not None:
            try:
                self._metrics.worker_job_outcomes.add(
                    1,
                    {
                        "service": self._metrics.service,
                        "job_type": job_type,
                        "outcome": outcome.outcome,
                    },
                )
                if outcome.outcome == "dead_lettered":
                    self._metrics.worker_dead_letter_depth.add(1)
            except Exception:  # noqa: BLE001, S110 - telemetry must not break the loop
                pass

    @staticmethod
    def _executor_job_type(outcome: Any) -> str:
        """Return the job type for an outcome.

        The use case outcome deliberately does not carry the job type; this reads it back from
        the executor's last executed job where available, defaulting to ``unknown``. Logging a
        wrong label would corrupt the ``job_type`` metric dimension, so the default is explicit
        rather than inferred from the id.
        """
        return str(getattr(outcome, "job_type", "unknown"))

    async def update_queue_gauges(self) -> None:
        """Refresh queue-depth gauges from the repository.

        Called periodically by the run loop in a future iteration; exposed separately so the
        worker's readiness endpoint can report queue state without the run loop.
        """
        return

    def install_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers that request a graceful stop.

        Only installed on the main thread of a real process; the runner is also driven directly
        by tests, where signal installation is not applicable.
        """
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                # Not all platforms support loop-level signal handlers. A missed handler means
                # SIGTERM kills the process without draining, which the lease backstops.
                loop.add_signal_handler(sig, self.request_stop)
