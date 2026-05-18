"""iv_snapshots

Revision ID: 679436966224
Revises: ca88b0042a6f
Create Date: 2026-05-18 10:51:31.535744+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "679436966224"
down_revision: str | Sequence[str] | None = "ca88b0042a6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "iv_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("underlying", sa.String(length=16), nullable=False),
        sa.Column("expiry_bucket", sa.String(length=8), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("atm_iv", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("iv_snapshots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_iv_snapshots_ts"), ["ts"], unique=False)
        batch_op.create_index(batch_op.f("ix_iv_snapshots_underlying"), ["underlying"], unique=False)
        batch_op.create_index(batch_op.f("ix_iv_snapshots_expiry_bucket"), ["expiry_bucket"], unique=False)
        batch_op.create_index(batch_op.f("ix_iv_snapshots_expiry_date"), ["expiry_date"], unique=False)
        batch_op.create_index("ix_iv_snapshots_lookup", ["underlying", "expiry_bucket", "ts"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("iv_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_iv_snapshots_lookup")
        batch_op.drop_index(batch_op.f("ix_iv_snapshots_expiry_date"))
        batch_op.drop_index(batch_op.f("ix_iv_snapshots_expiry_bucket"))
        batch_op.drop_index(batch_op.f("ix_iv_snapshots_underlying"))
        batch_op.drop_index(batch_op.f("ix_iv_snapshots_ts"))
    op.drop_table("iv_snapshots")
