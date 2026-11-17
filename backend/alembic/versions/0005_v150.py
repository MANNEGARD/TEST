"""1.5.0: förbrukningens grund och batteriets användbara kapacitet

Schema:

* ``raw_trips.fuel_basis`` / ``raw_trips.energy_basis``: hur en uppskattad förbrukning
  räknades fram – metod, värdena före och efter, felgränsen och eventuella anteckningar
  (``volvo.consumption``). Tom för äldre resor och för värden som inte är uträknade.
* ``vehicles.usable_battery_kwh``: batteriets användbara kapacitet i kWh, angiven av ägaren.
  Tom = DriveLog föreslår ett värde ur modell och årsmodell när ett belagt finns, annars är
  elen okänd.

Inga data ändras. Ner tar bort kolumnerna (grunden och kapaciteten försvinner, värdena på
resorna står kvar).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17 12:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('raw_trips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fuel_basis', _JSON, nullable=True))
        batch_op.add_column(sa.Column('energy_basis', _JSON, nullable=True))
    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('usable_battery_kwh', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.drop_column('usable_battery_kwh')
    with op.batch_alter_table('raw_trips', schema=None) as batch_op:
        batch_op.drop_column('energy_basis')
        batch_op.drop_column('fuel_basis')
