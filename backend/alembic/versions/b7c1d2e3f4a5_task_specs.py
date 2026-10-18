"""task_specs (PR-3)

Revision ID: b7c1d2e3f4a5
Revises: e2f5161fbeee
Create Date: 2026-10-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c1d2e3f4a5'
down_revision: Union[str, Sequence[str], None] = 'e2f5161fbeee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'task_specs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('spec_json', sa.JSON(), nullable=False),
        sa.Column('valid', sa.Boolean(), nullable=False),
        sa.Column('clarify_rounds', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_task_specs_task_id'), 'task_specs', ['task_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_task_specs_task_id'), table_name='task_specs')
    op.drop_table('task_specs')
