"""1.3.0: uppehåll i hämtningen, positioner ur adresser, kalenderdeltagare, regelns läge

Schema:

* ``raw_trips.collection_gap``: när hämtningen från Volvo låg nere före resan och hur
  många km mätaren ökade under tiden. Tom för äldre resor.
* ``known_locations.forward_*``: adressuppslag (adress → position) för platser som har
  adress men saknar position – utfall, tidpunkt, antal försök och nästa försök.
* ``calendar_events.attendees``: bokningens deltagare (läses när hela bokningen läses).
* ``location_patterns.manual_purpose_mode``: om en egen regels fasta text gäller alltid
  eller bara när kalendern inte säger något annat (tom = det senare).

Data, för varje befintlig användare (övriga nycklar rörs inte):

* ``merge.by_category = false`` – sammanslagningen fungerar exakt som före 1.3.0 tills
  ägaren sparar sektionen.
* ``calendar.store_body_preview = true`` – hela bokningen läses (ägaren har bett om det).
* ``automation.home_office_is_work = true`` – resor mellan hemmet och arbetsgivarens kontor
  godkänns som tjänsteresor (ägarens val).
* ``system.last_version`` – versionen som körde före uppgraderingen, så att DriveLog kan
  säga till när den nya versionen har startat.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16 19:00:00.000000+00:00
"""
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

import app.db
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

# Senaste versionen med databasversion 0003 (1.2.2 och 1.2.3 har samma schema).
PREVIOUS_VERSION = '1.2.3'

# Sektion → nycklar som sätts för befintliga användare.
_SEEDS: dict[str, dict[str, Any]] = {
    'merge': {'by_category': False},
    'calendar': {'store_body_preview': True},
    'automation': {'home_office_is_work': True},
    'system': {'last_version': PREVIOUS_VERSION},
}
# Nycklar som downgrade tar bort (store_body_preview lämnas: ofarligt).
_REMOVED_ON_DOWNGRADE: dict[str, tuple[str, ...]] = {
    'merge': ('by_category', 'category_rules_since'),
    'automation': ('home_office_is_work',),
    'system': ('last_version',),
}

_users = sa.table('users', sa.column('id', sa.Uuid()))
_settings = sa.table(
    'settings',
    sa.column('id', sa.Uuid()),
    sa.column('user_id', sa.Uuid()),
    sa.column('key', sa.String(length=120)),
    sa.column('value', _JSON),
    sa.column('updated_at', app.db.UTCDateTime(timezone=True)),
)


def upgrade() -> None:
    with op.batch_alter_table('raw_trips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('collection_gap', _JSON, nullable=True))
    with op.batch_alter_table('known_locations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('forward_status', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('forward_at', app.db.UTCDateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('forward_attempts', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('forward_next_at', app.db.UTCDateTime(timezone=True), nullable=True))
    with op.batch_alter_table('calendar_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('attendees', _JSON, nullable=True))
    with op.batch_alter_table('location_patterns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('manual_purpose_mode', sa.String(length=20), nullable=True))

    conn = op.get_bind()
    now = datetime.now(UTC)
    rows = conn.execute(
        sa.select(_settings.c.id, _settings.c.user_id, _settings.c.key, _settings.c.value)
        .where(_settings.c.key.in_(list(_SEEDS)))
    ).all()
    existing = {(user_id, key): (row_id, value) for row_id, user_id, key, value in rows}
    for (user_id,) in conn.execute(sa.select(_users.c.id)).all():
        for key, seed in _SEEDS.items():
            if (user_id, key) in existing:
                row_id, value = existing[(user_id, key)]
                new_value = dict(value) if isinstance(value, dict) else {}
                new_value.update(seed)
                conn.execute(
                    _settings.update().where(_settings.c.id == row_id).values(value=new_value, updated_at=now)
                )
            else:
                conn.execute(
                    _settings.insert().values(
                        id=uuid.uuid4(), user_id=user_id, key=key, value=dict(seed), updated_at=now
                    )
                )


def downgrade() -> None:
    conn = op.get_bind()
    for row_id, key, value in conn.execute(
        sa.select(_settings.c.id, _settings.c.key, _settings.c.value)
        .where(_settings.c.key.in_(list(_REMOVED_ON_DOWNGRADE)))
    ).all():
        new_value = dict(value) if isinstance(value, dict) else {}
        for name in _REMOVED_ON_DOWNGRADE[key]:
            new_value.pop(name, None)
        if new_value:
            conn.execute(_settings.update().where(_settings.c.id == row_id).values(value=new_value))
        else:
            conn.execute(_settings.delete().where(_settings.c.id == row_id))

    with op.batch_alter_table('location_patterns', schema=None) as batch_op:
        batch_op.drop_column('manual_purpose_mode')
    with op.batch_alter_table('calendar_events', schema=None) as batch_op:
        batch_op.drop_column('attendees')
    with op.batch_alter_table('known_locations', schema=None) as batch_op:
        batch_op.drop_column('forward_next_at')
        batch_op.drop_column('forward_attempts')
        batch_op.drop_column('forward_at')
        batch_op.drop_column('forward_status')
    with op.batch_alter_table('raw_trips', schema=None) as batch_op:
        batch_op.drop_column('collection_gap')
