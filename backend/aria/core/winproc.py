"""Запуск дочерних процессов на Windows без видимого консольного окна.

Бэкенд стартует из десктопа без собственной консоли, поэтому каждый дочерний консольный процесс
(uvx, npx, команда агента, хук) иначе открывает своё чёрное окно поверх интерфейса.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

# На не-Windows пусто, поэтому ``**NO_WINDOW`` безопасно добавлять к любому вызову subprocess/asyncio.
NO_WINDOW: dict[str, Any] = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if os.name == "nt" else {}
)
