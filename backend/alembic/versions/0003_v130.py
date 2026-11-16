"""uppehåll i hämtningen och sammanslagning per kategori (1.3.0)

* ``raw_trips.collection_gap``: när hämtningen från Volvo låg nere före resan och hur
  många km mätaren ökade under tiden. Tom för äldre resor.
* ``settings`` (``merge``): varje befintlig användare får ``by_category = false`` så att
  sammanslagningen fungerar exakt som i 1.2.0 tills ägaren sparar sektionen. Övriga
  nycklar rörs inte.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16 10:00:00.000000+00:00
"""
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

import app.db
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

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

    conn = op.get_bind()
    now = datetime.now(UTC)
    existing = {
        user_id: (row_id, value)
        for row_id, user_id, value in conn.execute(
            sa.select(_settings.c.id, _settings.c.user_id, _settings.c.value).where(_settings.c.key == 'merge')
        ).all()
    }
    for (user_id,) in conn.execute(sa.select(_users.c.id)).all():
        if user_id in existing:
            row_id, value = existing[user_id]
            new_value = dict(value) if isinstance(value, dict) else {}
            new_value['by_category'] = False
            conn.execute(
                _settings.update().where(_settings.c.id == row_id).values(value=new_value, updated_at=now)
            )
        else:
            conn.execute(
                _settings.insert().values(
                    id=uuid.uuid4(), user_id=user_id, key='merge', value={'by_category': False}, updated_at=now
                )
            )


def downgrade() -> None:
    conn = op.get_bind()
    for row_id, value in conn.execute(
        sa.select(_settings.c.id, _settings.c.value).where(_settings.c.key == 'merge')
    ).all():
        new_value = dict(value) if isinstance(value, dict) else {}
        new_value.pop('by_category', None)
        new_value.pop('category_rules_since', None)
        if new_value:
            conn.execute(_settings.update().where(_settings.c.id == row_id).values(value=new_value))
        else:
            conn.execute(_settings.delete().where(_settings.c.id == row_id))

    with op.batch_alter_table('raw_trips', schema=None) as batch_op:
        batch_op.drop_column('collection_gap')
