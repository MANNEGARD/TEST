"""självgående körjournal (1.1.0)

Adresser (geokodning), uträknad starttid, automatiska godkännanden, månadsskiftet och
nedladdningslänkar. Alla nya kolumner kan vara tomma; befintliga rader rörs inte.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15 22:39:02.734738+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('geocode_cache',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('provider_host', sa.String(length=200), nullable=False),
    sa.Column('lat_key', sa.Integer(), nullable=False),
    sa.Column('lon_key', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('fetched_at', app.db.UTCDateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', app.db.UTCDateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_geocode_cache_owner_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_geocode_cache')),
    sa.UniqueConstraint('owner_user_id', 'provider_host', 'lat_key', 'lon_key', name='uq_geocode_cell')
    )
    with op.batch_alter_table('geocode_cache', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_geocode_cache_owner_user_id'), ['owner_user_id'], unique=False)

    op.create_table('month_closes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('vehicle_id', sa.Uuid(), nullable=False),
    sa.Column('month', sa.String(length=7), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('blockers', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('notes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('export_id', sa.Uuid(), nullable=True),
    sa.Column('rows_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('last_checked_at', app.db.UTCDateTime(timezone=True), nullable=True),
    sa.Column('notified_at', app.db.UTCDateTime(timezone=True), nullable=True),
    sa.Column('notice_count', sa.Integer(), nullable=False),
    sa.Column('created_at', app.db.UTCDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', app.db.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_month_closes_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], name=op.f('fk_month_closes_vehicle_id_vehicles'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_month_closes')),
    sa.UniqueConstraint('vehicle_id', 'month', name='uq_month_close_vehicle_month')
    )
    with op.batch_alter_table('month_closes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_month_closes_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('export_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origin', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('superseded_by_id', sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column('rows_fingerprint', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('link_downloads', sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('known_locations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('geocode_status', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('geocoded_at', app.db.UTCDateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('osm_class', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('place_kind', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('assumption', sa.String(length=20), nullable=True))

    with op.batch_alter_table('logical_trips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('start_time_override', app.db.UTCDateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('approval_source', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('approval_basis', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))
        batch_op.add_column(sa.Column('auto_approved_at', app.db.UTCDateTime(timezone=True), nullable=True))

    with op.batch_alter_table('raw_trips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('start_estimate_basis', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('link_epoch', sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('vehicle_observations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('avg_speed_auto', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('statistics_reported_at', app.db.UTCDateTime(timezone=True), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('vehicle_observations', schema=None) as batch_op:
        batch_op.drop_column('statistics_reported_at')
        batch_op.drop_column('avg_speed_auto')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('link_epoch')

    with op.batch_alter_table('raw_trips', schema=None) as batch_op:
        batch_op.drop_column('start_estimate_basis')

    with op.batch_alter_table('logical_trips', schema=None) as batch_op:
        batch_op.drop_column('auto_approved_at')
        batch_op.drop_column('approval_basis')
        batch_op.drop_column('approval_source')
        batch_op.drop_column('start_time_override')

    with op.batch_alter_table('known_locations', schema=None) as batch_op:
        batch_op.drop_column('assumption')
        batch_op.drop_column('place_kind')
        batch_op.drop_column('osm_class')
        batch_op.drop_column('geocoded_at')
        batch_op.drop_column('geocode_status')

    with op.batch_alter_table('export_jobs', schema=None) as batch_op:
        batch_op.drop_column('link_downloads')
        batch_op.drop_column('rows_fingerprint')
        batch_op.drop_column('superseded_by_id')
        batch_op.drop_column('origin')

    with op.batch_alter_table('month_closes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_month_closes_user_id'))

    op.drop_table('month_closes')
    with op.batch_alter_table('geocode_cache', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_geocode_cache_owner_user_id'))

    op.drop_table('geocode_cache')
