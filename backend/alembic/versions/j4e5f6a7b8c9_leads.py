"""leads and lead_events (J1)

Revision ID: j4e5f6a7b8c9
Revises: i3d4e5f6a7b8
Create Date: 2026-11-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'j4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'i3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'leads',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('url', sa.String(), nullable=True),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('budget_text', sa.String(), nullable=True),
        sa.Column('budget_usd_est', sa.Float(), nullable=True),
        sa.Column('currency', sa.String(), nullable=True),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('language', sa.String(), nullable=True),
        sa.Column('raw_hash', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('effort_hours_est', sa.Float(), nullable=True),
        sa.Column('token_cost_est_usd', sa.Float(), nullable=True),
        sa.Column('scam_flags', sa.JSON(), nullable=False),
        sa.Column('sensitive', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_leads_url'), 'leads', ['url'], unique=False)
    op.create_index(op.f('ix_leads_raw_hash'), 'leads', ['raw_hash'], unique=False)
    op.create_index(op.f('ix_leads_status'), 'leads', ['status'], unique=False)
    op.create_index(op.f('ix_leads_created_at'), 'leads', ['created_at'], unique=False)
    op.create_table(
        'lead_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('lead_id', sa.Uuid(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_lead_events_lead_id'), 'lead_events', ['lead_id'], unique=False)
    op.create_index(op.f('ix_lead_events_created_at'), 'lead_events', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_lead_events_created_at'), table_name='lead_events')
    op.drop_index(op.f('ix_lead_events_lead_id'), table_name='lead_events')
    op.drop_table('lead_events')
    op.drop_index(op.f('ix_leads_created_at'), table_name='leads')
    op.drop_index(op.f('ix_leads_status'), table_name='leads')
    op.drop_index(op.f('ix_leads_raw_hash'), table_name='leads')
    op.drop_index(op.f('ix_leads_url'), table_name='leads')
    op.drop_table('leads')
