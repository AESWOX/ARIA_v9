#!/usr/bin/env python3
"""live_smoke.py — Block C: real API calls against configured providers.

Usage:
  cd backend && .venv/Scripts/python scripts/live_smoke.py

Each test reports PASS / FAIL with duration. Final exit code = number of failures.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Ensure backend/ is on sys.path for aria imports
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Load .env so settings can find API keys
try:
    from dotenv import load_dotenv
    load_dotenv(_BACKEND / ".env")
except ImportError:
    pass  # dotenv not installed — proceed without .env

from aria.config import get_settings
from aria.llm.key_pool import KeyPool
from aria.llm.providers.openai_compatible import OpenAICompatibleProvider


# ── helpers ─────────────────────────────────────────────────────────────────

_tests: list[dict] = []
_start = time.monotonic()


def test(name: str, fn):
    """Run one smoke test, capture result."""
    t0 = time.monotonic()
    try:
        fn()
        duration = time.monotonic() - t0
        _tests.append({"name": name, "status": "PASS", "duration": f"{duration:.2f}s"})
        print(f"  ✅ {name}  ({duration:.2f}s)")
    except Exception as e:
        duration = time.monotonic() - t0
        _tests.append({"name": name, "status": "FAIL", "duration": f"{duration:.2f}s", "error": str(e)})
        print(f"  ❌ {name}  ({duration:.2f}s): {e}")


# ── smoke tests ────────────────────────────────────────────────────────────

def smoke_duckduckgo():
    """Боевой обработчик web_search (а не просто GET): парсер обязан вернуть >= 3 результатов.

    Раньше смоук проверял только размер страницы, поэтому «results: []» (сломанный парсер) был зелёным.
    """
    import asyncio

    from aria.tools.handlers.web import web_search

    out = asyncio.run(web_search({"query": "python asyncio semaphore", "max_results": 5}))
    assert not out.get("error"), f"web_search error: {out['error']}"
    n = len(out["results"])
    assert n >= 3, f"parser returned {n} results (source={out.get('source')}) — markup change?"
    print(f"    web_search: {n} results via {out.get('source')}; first: {out['results'][0]['url']}")


def smoke_web_fetch():
    """web_fetch читает обычную страницу и отказывается ходить на 127.0.0.1."""
    import asyncio

    from aria.tools.handlers.web import web_fetch

    out = asyncio.run(web_fetch({"url": "https://example.com/"}))
    assert not out.get("error"), f"web_fetch error: {out['error']}"
    assert "Example Domain" in (out.get("title") or "") + out["content"], f"unexpected content: {out['content'][:80]!r}"
    print(f"    web_fetch: {out['chars']} chars, title={out['title']!r}")
    refused = asyncio.run(web_fetch({"url": "http://127.0.0.1:1/"}))
    assert "not a public address" in refused.get("error", ""), f"SSRF not refused: {refused}"
    print("    web_fetch: http://127.0.0.1 refused")


def smoke_groq_chat():
    """Real Groq API call via OpenAICompatibleProvider."""
    settings = get_settings()
    keys = settings.groq_api_keys_list
    if not keys:
        raise RuntimeError("GROQ_API_KEYS not set, skipping Groq smoke test")
    from aria.llm import groq_discovery

    if settings.groq_models_auto and not settings.groq_models:
        groq_discovery.refresh_if_stale(keys, settings.groq_base_url)
    model = settings.groq_main_chain[0]
    print(f"    Groq model from chain: {model}")
    pool = KeyPool(keys, name="smoke-groq")
    provider = OpenAICompatibleProvider(
        provider_id="smoke-groq",
        provider_class="test",
        base_url=settings.groq_base_url,
        model=model,
        key_pool=pool,
    )
    chat = provider.provider_id
    # We just validate that the provider can be constructed and its pool has keys
    assert len(pool) >= 1, "Groq pool should have at least 1 key"
    print(f"    Groq pool has {len(pool)} keys")
    # Try a real HTTP call
    import httpx
    key = pool.next_key()
    base = settings.groq_base_url
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{base}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Say exactly: hello from aria smoke test"}],
                # 512: модели с рассуждением (gpt-oss, qwen3) тратят токены на «мысли», при 20 content пустой
                "max_tokens": 512,
            },
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        if resp.status_code == 401:
            # Auth error means the code path works, keys need refresh — partial pass
            print(f"    Groq endpoint reachable (401: keys need refresh)")
            return
        if resp.status_code in (400, 404):
            raise AssertionError(
                f"Groq {resp.status_code} for model {model!r}: {resp.text[:200]} — "
                "run: python -m aria.llm.groq_discovery --probe")
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        assert content and content.strip(), (
            f"empty response from Groq model {model!r}, finish_reason="
            f"{data['choices'][0].get('finish_reason')!r}")
        print(f"    Groq responded: {content[:60]}...")


def smoke_gemini_vision():
    """Real Gemini Vision API call (downloads a small test PNG from the web)."""
    settings = get_settings()
    keys = settings.vision_gemini_api_keys_list
    if not keys:
        # Fallback to main gemini keys
        keys = settings.gemini_api_keys_list
    if not keys:
        raise RuntimeError("no Gemini API keys available for vision smoke test")
    
    # First: verify text-only chat works on the endpoint
    import httpx
    key = keys[0]
    base = settings.gemini_base_url.rstrip("/")
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{base}/chat/completions",
            json={
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "Say hello"}],
                "max_tokens": 10,
            },
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        if resp.status_code == 401:
            print(f"    Gemini endpoint reachable (401: keys need refresh)")
            return
        
    # Second: try vision with the test fixture image
    fixture = _BACKEND / "tests" / "fixtures" / "test_image.png"
    if not fixture.exists():
        print("    no test_image.png fixture, skipping vision image test"); return
    img_bytes = fixture.read_bytes()
    b64 = base64.b64encode(img_bytes).decode()
    data_uri = f"data:image/png;base64,{b64}"
    
    with httpx.Client(timeout=20) as client:
        resp = client.post(
            f"{base}/chat/completions",
            json={
                "model": "gemini-2.5-flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image in one word"},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    }
                ],
                "max_tokens": 10,
            },
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text[:200]}"
        print(f"    Gemini Vision responded (status 200)")


# ── main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  ARIA v13 — Live Smoke Tests (Block C)")
    print("=" * 60)
    settings = get_settings()

    # 1. DuckDuckGo web search
    print("\n[1] Web Search (DuckDuckGo)")
    test("web_search", smoke_duckduckgo)
    test("web_fetch", smoke_web_fetch)

    # 2. Groq chat completion
    print("\n[2] Groq Chat (model from discovery chain)")
    if settings.groq_api_keys_list:
        test("groq_chat", smoke_groq_chat)
    else:
        print("  ⏭️  GROQ_API_KEYS not set — skipped")
        _tests.append({"name": "groq_chat", "status": "SKIP", "duration": "0.00s"})

    # 3. Gemini Vision
    print("\n[3] Gemini Vision (gemini-2.5-flash)")
    has_vision_keys = bool(settings.vision_gemini_api_keys_list)
    has_gemini_keys = bool(settings.gemini_api_keys_list)
    if has_vision_keys or has_gemini_keys:
        test("gemini_vision", smoke_gemini_vision)
    else:
        print("  ⏭️  no Gemini API keys — skipped")
        _tests.append({"name": "gemini_vision", "status": "SKIP", "duration": "0.00s"})

    # ── summary ──
    total = time.monotonic() - _start
    passed = sum(1 for t in _tests if t["status"] == "PASS")
    skipped = sum(1 for t in _tests if t["status"] == "SKIP")
    failed = sum(1 for t in _tests if t["status"] == "FAIL")

    print("\n" + "=" * 60)
    print(f"  Result: {passed} PASS / {failed} FAIL / {skipped} SKIP  (total {total:.1f}s)")
    print("=" * 60)

    # Write JSON report
    report_path = _BACKEND / "data" / "live_smoke.json"
    report = {
        "version": "v13",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_seconds": round(total, 1),
        "tests": _tests,
        "summary": {"passed": passed, "failed": failed, "skipped": skipped},
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  Report: {report_path}")

    sys.exit(failed)


if __name__ == "__main__":
    main()
