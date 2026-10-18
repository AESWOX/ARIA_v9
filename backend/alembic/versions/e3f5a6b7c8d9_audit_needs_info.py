"""audit verdict needs_info (П.5.1 ТЗ)

Revision ID: e3f5a6b7c8d9
Revises: d9e3f4a5b6c7
Create Date: 2026-10-16 00:30:00.000000

Цепочка остаётся линейной: e2f5161fbeee -> b7c1d2e3f4a5 -> c8d2e3f4a5b6 ->
d9e3f4a5b6c7 -> e3f5a6b7c8d9.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f5a6b7c8d9'
down_revision: Union[str, Sequence[str], None] = 'd9e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Enum в Postgres — отдельный тип: добавляем значение, не пересоздавая таблицу.
        op.execute("ALTER TYPE auditverdict ADD VALUE IF NOT EXISTS 'needs_info'")
    # SQLite/MySQL хранят enum строкой: схему менять не нужно, значение уже в коде.


def downgrade() -> None:
    # Значение enum в Postgres не удаляется; понижение здесь — no-op.
    pass
