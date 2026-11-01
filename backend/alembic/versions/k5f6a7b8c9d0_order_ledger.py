"""order ledger (J6)

Revision ID: k5f6a7b8c9d0
Revises: j4e5f6a7b8c9
Create Date: 2026-11-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'k5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'j4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'order_ledger',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('lead_id', sa.Uuid(), nullable=False),
        sa.Column('revenue_amount', sa.Float(), nullable=False),
        sa.Column('currency', sa.String(), nullable=False),
        sa.Column('platform_fee', sa.Float(), nullable=False),
        sa.Column('fx_to_usd', sa.Float(), nullable=False),
        sa.Column('llm_cost_usd', sa.Float(), nullable=True),
        sa.Column('hours', sa.Float(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('order_type', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_order_ledger_lead_id'), 'order_ledger', ['lead_id'], unique=False)
    op.create_index(op.f('ix_order_ledger_created_at'), 'order_ledger', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_order_ledger_created_at'), table_name='order_ledger')
    op.drop_index(op.f('ix_order_ledger_lead_id'), table_name='order_ledger')
    op.drop_table('order_ledger')
