"""Knowledge base: documents, chunks, pgvector embeddings and full-text search.

Revision ID: 0002_knowledge_base
Revises: 0001_phase1_foundation

Why these columns exist, since "documents and chunks" is not a schema design:

* ``documents`` holds the full text once, so a citation can quote the source and a re-ingest
  can detect that nothing changed. ``content_hash`` is what makes ingestion idempotent.
* ``chunks`` holds one row per retrievable span. Title and source are **denormalised** onto
  the chunk: a citation needs them on every retrieval, and a join per retrieved chunk to
  render a number the user is waiting for is a latency cost with no offsetting benefit.
* ``embedding`` is a ``vector(768)`` value stored in a text column, guarded by a
  ``vector_dims`` CHECK constraint. Declared as text rather than through a driver type so the
  project keeps **zero** new Python dependencies: the ``pgvector`` helper package would add a
  library to express one column type, and the constraint below buys the same protection - a
  vector wider than the column is rejected at insert time, loudly, at ingestion, rather than
  silently producing a wrong similarity score. Shorter vectors are legal, because pgvector
  pads them on comparison; a different embedding model's width is a deployment decision, not
  a schema migration.
* ``provider`` records which embedding model produced the vector. A corpus embedded by one
  model must never be silently queried by another.
* ``search_vector`` is a **generated** column over the chunk text with a GIN index. Generated
  rather than maintained by the application so it cannot drift from the text it indexes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

#: Identifiers for Alembic's version table.
revision = "0002_knowledge_base"
down_revision = "0001_phase1_foundation"
branch_labels = None
depends_on = None

#: Vector width declared on the column. See the module docstring for why this is a cap.
EMBEDDING_DIMENSIONS = 768

#: Provider identifier recorded on rows written by the deterministic local encoder.
LOCAL_PROVIDER = "local-hashing-ngram-v1"


def upgrade() -> None:
    """Create the knowledge-base tables and their indexes."""
    op.create_table(
        "documents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "embedding",
            sa.Text(),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False, server_default=LOCAL_PROVIDER),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE", name="fk_chunks_document"
        ),
        sa.CheckConstraint("position >= 0", name="ck_chunks_position_non_negative"),
        sa.UniqueConstraint("document_id", "position", name="uq_chunks_document_position"),
    )

    # The column is text; these casts give pgvector its operators and index access. Declared
    # together so the storage type and the operators cannot drift apart.
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(768) USING embedding::vector")
    op.execute(
        "ALTER TABLE chunks ADD CONSTRAINT ck_chunks_embedding_width "
        "CHECK (vector_dims(embedding) <= 768)"
    )

    # Generated, so the index can never describe text the chunk no longer contains.
    op.execute(
        "ALTER TABLE chunks ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
    )

    # HNSW is the right structure for approximate cosine search at corpus sizes that grow,
    # and it is what pgvector recommends over IVFFlat now that enough data has been loaded.
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX ix_chunks_search_vector ON chunks USING gin (search_vector)")
    op.execute("CREATE INDEX ix_chunks_document_id ON chunks (document_id)")


def downgrade() -> None:
    """Remove the knowledge-base tables."""
    op.execute("DROP INDEX IF EXISTS ix_chunks_document_id")
    op.execute("DROP INDEX IF EXISTS ix_chunks_search_vector")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding")
    op.drop_table("chunks")
    op.drop_table("documents")
