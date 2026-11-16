"""omräkning efter beslut (1.2.2)

Ägarens beslut i Granskning räknar om förslag och ärenden efter svaret i stället för i
anropet. Fordonet får en räknare för beställda och utförda omräkningar och ett lås med
utgångstid. Alla befintliga fordon får en beställd omräkning, så att ärenden som inte
längre gäller (t.ex. överlapp i mätarställningen som bara var avrundning) stängs direkt
efter uppgraderingen.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16 14:30:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('recompute_requested', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('recompute_done', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('recompute_started_at', app.db.UTCDateTime(timezone=True), nullable=True))
    op.execute("UPDATE vehicles SET recompute_requested = 1")


def downgrade() -> None:
    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.drop_column('recompute_started_at')
        batch_op.drop_column('recompute_done')
        batch_op.drop_column('recompute_requested')
