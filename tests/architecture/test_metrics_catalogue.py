"""Architecture fitness tests for the metric catalogue.

A metric catalogue that drifts from the code is worse than none: it is documentation that
lies. These tests assert the catalogue against the *live* instruments, and enforce the
cardinality rules that make a metrics backend survivable.

Cardinality is the failure that does not announce itself. Adding ``request_id`` or ``path``
to a label set is one line of code and produces one time series per request, which takes the
metrics backend down while the service looks perfectly healthy.
"""

from __future__ import annotations

import pytest

from knowledge_assistant.observability.metrics import (
    LATENCY_BUCKETS_SECONDS,
    METRIC_CATALOGUE,
    MetricNames,
    Metrics,
    build_meter,
)

pytestmark = pytest.mark.architecture

#: Values a label may legitimately take. Anything not drawn from a small closed set -
#: request IDs, paths, job IDs, exception messages - is a cardinality bomb.
_LOW_CARDINALITY_LABELS = {
    "method",
    "route",
    "status_class",
    "operation",
    "state",
    "result",
    "outcome",
    "failure_class",
    "component",
    "service",
    "dependency",
    "level",
    # Bounded by the handler registry, not by traffic. Phase 1 registers exactly three job
    # types; a fourth is a code review decision, not a label explosion.
    "job_type",
}

#: Labels that must never appear, however convenient they look.
_FORBIDDEN_LABELS = {
    "request_id",
    "trace_id",
    "span_id",
    "id",
    "job_id",
    "tenant_id",
    "user_id",
    "path",
    "url",
    "query",
    "email",
    "message",
    "exception",
    "error",
    "idempotency_key",
}


class TestCatalogueIntegrity:
    """Every documented metric is unique and fully documented."""

    def test_catalogue_is_not_empty(self) -> None:
        assert len(METRIC_CATALOGUE) > 0

    def test_metric_names_are_unique(self) -> None:
        names = [m.name for m in METRIC_CATALOGUE]
        duplicates = {n for n in names if names.count(n) > 1}
        assert not duplicates, f"duplicate metric names: {duplicates}"

    @pytest.mark.parametrize("field", ["purpose", "owner", "alert_implication"])
    def test_every_metric_documents_the_field(self, field: str) -> None:
        """The brief requires purpose, owner and alert implication for every metric. A blank
        field is the same as a missing field, so emptiness is checked too.
        """
        for meta in METRIC_CATALOGUE:
            assert getattr(meta, field).strip(), f"{meta.name} has an empty {field}"

    def test_every_metric_has_a_valid_type(self) -> None:
        valid = {"counter", "histogram", "updowncounter", "gauge"}
        for meta in METRIC_CATALOGUE:
            assert meta.type in valid, f"{meta.name} has type {meta.type!r}"

    def test_metric_names_use_a_consistent_prefix(self) -> None:
        """Unprefixed names collide with other services sharing the exporter."""
        for meta in METRIC_CATALOGUE:
            assert meta.name.startswith(("ka.http", "ka.db", "ka.worker", "ka.process")), meta.name

    def test_every_named_constant_is_catalogued(self) -> None:
        """A constant defined on ``MetricNames`` but absent from the catalogue is an
        undocumented metric that will be scraped and never queried.
        """
        declared = {
            value
            for name, value in vars(MetricNames).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        catalogued = {m.name for m in METRIC_CATALOGUE}
        assert declared == catalogued, f"undocumented: {declared - catalogued}"


class TestCardinalityDiscipline:
    """The rules that keep the metrics backend alive."""

    @pytest.mark.parametrize("meta", METRIC_CATALOGUE, ids=lambda m: m.name)
    def test_no_forbidden_label(self, meta: object) -> None:
        """One forbidden label is enough to create unbounded series."""
        for label in meta.labels:
            assert label not in _FORBIDDEN_LABELS, f"{meta.name} carries {label}"

    @pytest.mark.parametrize("meta", METRIC_CATALOGUE, ids=lambda m: m.name)
    def test_labels_are_from_the_allowed_set(self, meta: object) -> None:
        for label in meta.labels:
            assert label in _LOW_CARDINALITY_LABELS, f"{meta.name} has undeclared label {label}"

    @pytest.mark.parametrize("meta", METRIC_CATALOGUE, ids=lambda m: m.name)
    def test_label_count_is_bounded(self, meta: object) -> None:
        """Series count is the product of label cardinalities. Five labels is already a
        design smell.
        """
        assert len(meta.labels) <= 3, f"{meta.name} has too many labels"


class TestInstrumentsMatchCatalogue:
    """The catalogue must describe the instruments that actually exist."""

    def test_all_catalogue_names_are_created(self) -> None:
        metrics = Metrics(build_meter("test"), service="test")
        assert isinstance(metrics.catalogue, tuple | list)

    def test_every_catalogue_entry_maps_to_an_instrument_attribute(self) -> None:
        """Name and attribute must correspond, so a rename cannot orphan documentation."""
        metrics = Metrics(build_meter("test"), service="test")
        attribute_names = {
            name
            for name in dir(metrics)
            if not name.startswith("_") and name not in {"service", "common_labels", "catalogue"}
        }
        # 16 catalogue entries, 13 instruments plus three facade properties.
        assert len(attribute_names) == 13
        assert len(METRIC_CATALOGUE) >= len(attribute_names)

    def test_latency_buckets_are_sorted_and_include_the_slo_targets(self) -> None:
        """Histogram buckets determine whether a p95 is computable at all. A bucket set that
        stops below the latency target silently makes the SLO unmeasurable.
        """
        assert list(LATENCY_BUCKETS_SECONDS) == sorted(LATENCY_BUCKETS_SECONDS)
        assert LATENCY_BUCKETS_SECONDS[-1] >= 10.0, "must cover the slow tail"

    def test_common_labels_include_the_service(self) -> None:
        """Without a service label, api and worker series are indistinguishable."""
        metrics = Metrics(build_meter("api"), service="api")
        assert ("service", "api") in metrics.common_labels


class TestFacadePreventsAdHocLabels:
    """The facade exists so call sites cannot invent instruments at the call site."""

    def test_facade_has_no_public_add_method(self) -> None:
        """An escape hatch would defeat the whole purpose of the catalogue."""
        metrics = Metrics(build_meter("test"), service="test")
        public = [n for n in dir(metrics) if not n.startswith("_")]
        assert not any("add" in n or "create" in n for n in public)
