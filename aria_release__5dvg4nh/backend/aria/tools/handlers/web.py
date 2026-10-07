"""aria/tools/handlers/web.py — web_search + duckduckgo handler."""
from __future__ import annotations

import logging
from urllib.parse import quote_plus

logger = logging.getLogger("aria.web")


async def web_search(input_json: dict, sandbox_root: str = "", **_ctx) -> dict:
    """DuckDuckGo (lite) — поиск в интернете, zero-dependency."""
    query = input_json.get("query", "")
    if not query:
        return {"results": [], "error": "empty query"}
    max_results = min(input_json.get("max_results", 5), 15)

    # DuckDuckGo lite — чисто HTML, без JS, без API-ключа
    url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return {"results": [], "error": f"request failed: {exc.reason}"}
    except Exception as exc:
        return {"results": [], "error": str(exc)}

    # Парсим DuckDuckGo Lite HTML — ищем ссылки в результатах
    # Формат: <a href="..." class="result-link">text</a>
    import re
    results = []
    for match in re.finditer(
        r'<a\s+href="(https?://[^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        url = match.group(1)
        title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if title and url not in [r["url"] for r in results]:
            results.append({"title": title, "url": url})
            if len(results) >= max_results:
                break

    return {"results": results, "query": query}
