"""Phase 1 foundation: extension, jobs table, idempotency table.

Revision ID: 0001_phase1_foundation
Revises:
Create Date: 2026-10-04

Design notes that a reviewer should check, because each index exists for a specific query:

* ``ix_jobs_claimable (state, available_at)`` - the claim query's filter and sort. A claim
  without this index scans the whole table; with 22 M rows of Phase 0's stress corpus that is a
  multi-second scan on every poll.
* Partial index on ``idempotency_key`` - idempotent insert relies on a unique conflict target, so
  the index must exist and must be partial (many rows have NULL).
* Partial index on ``dead_lettered`` state - the alert query counts dead letters constantly; it
  must not touch the working set.
* ``CHECK`` on ``state`` and ``attempts_used`` - the state machine is enforced in the domain, but a
  constraint catches a direct SQL write from an operator or a migration script.

**No speculative tables.** Phase 0's conceptual model named documents, chunks, citations and more.
None are created here: creating them before the ingestion design is measured would freeze
assumptions and make a later change a migration rather than a first draft.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_phase1_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Valid job states, duplicated here as a literal rather than imported so that a change to the
#: domain enum cannot silently rewrite history in an applied migration.
JOB_STATES = ("pending", "running", "retry", "succeeded", "dead_lettered")


def upgrade() -> None:
    """Apply the foundation schema."""
    # pgvector is required by Phase 2+, but Phase 1 declares no vector columns. Creating the
    # extension now means the image and the database are validated against the same major
    # version before any vector column exists, and the version is recorded in pg_extension so a
    # mismatch is detectable rather than discovered during a later index build.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("result", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"state IN {JOB_STATES}",
            name="ck_jobs_state",
        ),
        sa.CheckConstraint("attempts_used >= 0", name="ck_jobs_attempts_non_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_positive"),
        sa.CheckConstraint("max_attempts <= 10", name="ck_jobs_max_attempts_ceiling"),
    )

    # Claim path: filter by state + order by available_at.
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["state", "available_at"],
        unique=False,
    )

    # Idempotent insert conflict target. Partial, because most rows have no key.
    op.create_index(
        "ix_jobs_idempotency_key",
        "jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # Dead-letter counting for the alert, isolated from the working set.
    op.create_index(
        "ix_jobs_dead_lettered",
        "jobs",
        ["id"],
        unique=False,
        postgresql_where=sa.text("state = 'dead_lettered'"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="in_progress"),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("response_payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('in_progress', 'completed')", name="ck_idempotency_status"),
        # The conflict target for the atomic claim.
        sa.PrimaryKeyConstraint("tenant_id", "scope", "key", name="pk_idempotency_keys"),
    )

    # Retention sweep: delete completed records past expiry.
    op.create_index(
        "ix_idempotency_expires_at",
        "idempotency_keys",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the foundation schema.

    Drops tables but deliberately leaves the ``vector`` extension in place: dropping a shared
    extension can fail if another schema depends on it, and a failed downgrade blocks recovery.
    Extension removal is an explicit, separate, reviewed operation.
    """
    op.drop_index("ix_idempotency_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_jobs_dead_lettered", table_name="jobs")
    op.drop_index("ix_jobs_idempotency_key", table_name="jobs")
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_table("jobs")