"""1.5.0: "bilen används" – Volvos tillgänglighetsstatus på varje avläsning

Schema:

* ``vehicle_observations.accessibility_status`` / ``accessibility_reason``: bilens
  availabilityStatus och unavailableReason (command-accessibility) som de kom.
* ``vehicle_observations.accessibility_reported_at``: bilens egen tidsstämpel för statusen.
* ``vehicle_observations.car_in_use``: härlett – sant bara för UNAVAILABLE med orsaken
  CAR_IN_USE, falskt för andra lästa statusar, tomt när ingen status lästes.

Tomma för äldre avläsningar. Inga data ändras. Ner tar bort kolumnerna.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17 13:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('vehicle_observations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('accessibility_status', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('accessibility_reason', sa.String(length=40), nullable=True))
        batch_op.add_column(
            sa.Column('accessibility_reported_at', app.db.UTCDateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column('car_in_use', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('vehicle_observations', schema=None) as batch_op:
        batch_op.drop_column('car_in_use')
        batch_op.drop_column('accessibility_reported_at')
        batch_op.drop_column('accessibility_reason')
        batch_op.drop_column('accessibility_status')
