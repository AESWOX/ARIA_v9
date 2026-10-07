"""real_run_via_proxy.py — реальный прогон полного цикла Stage 1-7 (run_task)
через LiteLLM-прокси (http://127.0.0.1:4000/v1, модель gemini-2.5-flash).

Отличие от дефолтного роутера: все provider_class (premium_reasoning,
standard_reasoning, subagent_execution, free_tier_reasoning) указывают на
локальную LiteLLM-прокси с единым ключом, а не на внешние API.

Запуск:
    cd backend
    .venv/Scripts/python scripts/real_run_via_proxy.py

Верификация после прогона: status=done, непустой note_path, пустые integrity_flags,
файл в sandbox, заметка в vault.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_BACKEND / ".env")

from aria.storage.obsidian_vault import vault_root  # noqa: E402

# ── Proxy connection (verified: GET /v1/models → 200) ──
PROXY_BASE_URL = os.getenv("ARIA_PROXY_BASE_URL", "http://127.0.0.1:4000/v1")
PROXY_MODEL = os.getenv("ARIA_PROXY_MODEL", "gemini-2.5-flash")
PROXY_KEY = os.getenv("ARIA_PROXY_API_KEY", "sk-litellm-gemini-pool-local")


def build_proxy_router():
    """ProviderRouter где все классы идут через LiteLLM-прокси."""
    from aria.llm.key_pool import KeyPool
    from aria.llm.providers.openai_compatible import OpenAICompatibleProvider
    from aria.llm.router import ProviderRouter

    router = ProviderRouter()
    pool = KeyPool([PROXY_KEY], name="litellm-proxy")

    for provider_id, provider_class in [
        ("proxy-premium", "premium_reasoning"),
        ("proxy-standard", "standard_reasoning"),
        ("proxy-subagent", "subagent_execution"),
        ("proxy-free", "free_tier_reasoning"),
    ]:
        router.register(
            OpenAICompatibleProvider(
                provider_id=provider_id,
                provider_class=provider_class,
                base_url=PROXY_BASE_URL,
                model=PROXY_MODEL,
                key_pool=pool,
            )
        )
    return router


async def main(objective: str) -> int:
    from aria.core.executor import run_task
    from aria.db.base import init_db, run_migrations, session_scope
    from aria.db import repository as repo

    init_db()
    run_migrations()
    router = build_proxy_router()

    # Повторяемость: если файл уже содержит целевой контент с прошлого прогона,
    # hash_before == hash_after → ложный НАЕБАЛ. Сбрасываем в placeholder.
    sandbox = str((_BACKEND / "data" / "sandbox").resolve())
    stub = Path(sandbox) / "generated" / "proxy_hello.txt"
    if stub.exists():
        stub.write_text("old placeholder content", encoding="utf-8")
        print("[reset] stub file reset to placeholder")

    with session_scope() as db:
        sess = repo.create_session(db, title="REAL RUN via LiteLLM proxy")
        task = repo.create_task(db, session=sess, role="coder", objective=objective)
        db.flush()

        print(f"[init] session={sess.id} task={task.id}")
        print(f"[init] router classes: {sorted(router.providers_by_class.keys())}")
        print(f"[init] proxy: {PROXY_BASE_URL} model={PROXY_MODEL}")

        result = await run_task(session=db, task=task, router=router)

        print("\n=== RESULT ===")
        for key, value in result.items():
            print(f"  {key}: {value}")

        note_path = result.get("note_path")
        vroot = vault_root()
        abs_note = (vroot / note_path) if note_path else None
        print(f"\n[check] status: {result.get('status')}")
        print(f"[check] integrity_flags: {result.get('integrity_flags')}")
        print(f"[check] note_path exists: {bool(abs_note and abs_note.is_file())}")

        if os.path.exists(stub):
            content = stub.read_text(encoding="utf-8", errors="replace")
            print(f"[check] sandbox file exists: {stub}")
            print(f"[check] sandbox content: {content!r}")
        else:
            print(f"[check] sandbox file MISSING: {stub}")

        if abs_note and abs_note.is_file():
            print(f"\n[check] vault note:\n{'-' * 40}")
            print(abs_note.read_text(encoding="utf-8", errors="replace")[:1500])
            print("-" * 40)

        return 0 if result.get("status") == "done" and result.get("integrity_flags") == [] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real run of Stage 1-7 (run_task) through the LiteLLM proxy."
    )
    parser.add_argument(
        "--objective",
        default=(
            "Rewrite the existing file generated/proxy_hello.txt so that its content "
            "is exactly `Hello from ARIA via LiteLLM proxy`. Use EXACTLY TWO plan steps: "
            "step 1 = file_write with path=generated/proxy_hello.txt, "
            "step 2 = file_read with path=generated/proxy_hello.txt. "
            "Do NOT use delegate_task, shell_execute, or any other tool."
        ),
        help="Objective to run. Defaults to the standard proxy_hello.txt task.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.objective)))
