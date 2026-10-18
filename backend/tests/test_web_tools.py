"""P0: web_search (парсер на сохранённых образцах) + web_fetch (чтение страниц, SSRF).

Сети нет: urlopen / _open_url подменены, DNS подменён там, где нужен «публичный» адрес.
Образцы выдачи DuckDuckGo лежат в tests/fixtures/ddg/. Файл ``ddg_lite_real.html`` (настоящая выдача
с машины владельца, команда — ARIA_PLAN §6.2 п.1) подхватывается автоматически и обязан давать >= 3 результатов.
"""
from __future__ import annotations

import email.message
import http.server
import io
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aria.core import guardrails
from aria.core.loop import _tool_schemas_for_role
from aria.core.roles import ROLE_REGISTRY
from aria.db.enums import IdempotencyClass
from aria.tools.handlers import web
from aria.tools.registry import TOOL_REGISTRY

FIX = Path(__file__).parent / "fixtures" / "ddg"
LITE = (FIX / "ddg_lite_sample.html").read_text(encoding="utf-8")
HTML_EP = (FIX / "ddg_html_sample.html").read_text(encoding="utf-8")
CAPTCHA = (FIX / "ddg_captcha.html").read_text(encoding="utf-8")
LITE_SAMPLES = sorted(FIX.glob("ddg_lite_*.html"))  # синтетический + настоящий, если владелец его положил


# --------------------------------------------------------------------------- parser


class TestParser:
    def test_lite_sample_organic_only_deduped_and_unwrapped(self):
        results = web.parse_ddg_results(LITE)
        assert [r["url"] for r in results] == [
            "https://docs.python.org/3/library/asyncio-sync.html",
            "https://stackoverflow.com/questions/40836800/python-asyncio-semaphore-in-async-await-function?noredirect=1",
            "https://realpython.com/async-io-python/",
            "https://superfastpython.com/asyncio-semaphore/",
        ]  # реклама (/y.js) убрана, дубль (5-я строка) убран, uddg раскрыт

    def test_titles_and_snippets_are_clean_text(self):
        results = web.parse_ddg_results(LITE)
        assert results[0]["title"] == "Synchronization Primitives — Python 3.13 documentation"
        assert results[0]["snippet"].startswith("An asyncio semaphore manages an internal counter")
        assert "<b>" not in results[0]["snippet"]
        assert results[1]["snippet"] == "I'm trying to limit the number of concurrent requests with a semaphore & asyncio.gather..."
        assert results[2]["title"].endswith("Real Python") and results[2]["snippet"].startswith("Learn how async IO")

    def test_attribute_order_and_quote_style_do_not_matter(self):
        variants = [
            """<a href="https://e.com/1" class="result-link">T</a>""",
            """<a class='result-link' href='https://e.com/1'>T</a>""",
            """<a rel="nofollow" class="x result-link y" href="https://e.com/1" data-z=1>T</a>""",
            """<A HREF=https://e.com/1 CLASS=result-link>T</A>""",
        ]
        for html in variants:
            assert web.parse_ddg_results(html) == [{"title": "T", "url": "https://e.com/1", "snippet": ""}], html

    def test_html_endpoint_sample(self):
        results = web.parse_ddg_results(HTML_EP)
        assert [r["url"] for r in results] == [
            "https://docs.python.org/3/library/asyncio-sync.html",
            "https://realpython.com/async-io-python/",
            "https://example.org/plain-link",
        ]
        assert results[1]["snippet"].startswith("Async IO is a concurrent programming design")

    def test_max_results_limits_output(self):
        assert len(web.parse_ddg_results(LITE, 2)) == 2

    def test_captcha_page_has_no_results_and_is_detected(self):
        assert web.parse_ddg_results(CAPTCHA) == []
        assert web.looks_blocked(CAPTCHA)
        assert not web.looks_blocked(LITE)

    def test_broken_markup_does_not_raise(self):
        assert web.parse_ddg_results("<a class='result-link' href='https://e.com'>unclosed <b><td class=result-snippet") == []

    @pytest.mark.parametrize("sample", LITE_SAMPLES, ids=lambda p: p.name)
    def test_every_lite_sample_gives_at_least_three_results(self, sample):
        results = web.parse_ddg_results(sample.read_text(encoding="utf-8", errors="replace"))
        assert len(results) >= 3, f"{sample.name}: разметка изменилась или парсер сломан"
        for r in results:
            assert r["title"] and r["url"].startswith(("http://", "https://"))
            assert "duckduckgo.com" not in r["url"]

    @pytest.mark.parametrize("href,expected", [
        ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fx%3Fy%3D1&rut=zz", "https://a.com/x?y=1"),
        ("/l/?uddg=https%3A%2F%2Fa.com%2F&rut=zz", "https://a.com/"),
        ("https://duckduckgo.com/l/?uddg=http%3A%2F%2Fa.com%2F", "http://a.com/"),
        ("https://plain.example/page", "https://plain.example/page"),
        ("//duckduckgo.com/l/?rut=only", ""),
        ("", ""),
    ])
    def test_unwrap_ddg_redirect(self, href, expected):
        assert web.unwrap_ddg_redirect(href) == expected


# --------------------------------------------------------------------------- web_search handler


def make_urlopen(pages: dict):
    """Подмена urllib.request.urlopen: ключ — подстрока URL, значение — HTML (str) или исключение."""
    calls: list[str] = []

    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        for key, val in pages.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                cm = MagicMock()
                cm.__enter__.return_value.read.return_value = val.encode("utf-8")
                return cm
        raise AssertionError(f"unexpected url {url}")

    return fake, calls


@pytest.fixture()
def urlopen_pages(monkeypatch):
    def install(pages):
        fake, calls = make_urlopen(pages)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return calls

    return install


EMPTY_PAGE = "<html><body><p>No results.</p></body></html>"


class TestWebSearch:
    async def test_lite_sample_end_to_end(self, urlopen_pages):
        calls = urlopen_pages({"lite.duckduckgo.com": LITE})
        out = await web.web_search({"query": "python asyncio semaphore"})
        assert len(out["results"]) >= 3 and out["source"] == "lite"
        assert out["source_trust"] == "untrusted" and out["notice"]
        assert len(calls) == 1 and "q=python+asyncio+semaphore" in calls[0]

    async def test_falls_back_to_html_endpoint_when_lite_has_no_results(self, urlopen_pages):
        calls = urlopen_pages({"lite.duckduckgo.com": EMPTY_PAGE, "html.duckduckgo.com": HTML_EP})
        out = await web.web_search({"query": "x"})
        assert out["source"] == "html" and len(out["results"]) == 3 and len(calls) == 2

    async def test_http_403_on_lite_falls_back_to_html(self, urlopen_pages):
        err = urllib.error.HTTPError("https://lite.duckduckgo.com/lite/", 403, "Forbidden", email.message.Message(), None)
        urlopen_pages({"lite.duckduckgo.com": err, "html.duckduckgo.com": HTML_EP})
        out = await web.web_search({"query": "x"})
        assert out["source"] == "html" and out["results"]

    async def test_captcha_on_both_endpoints_gives_clear_error(self, urlopen_pages):
        urlopen_pages({"lite.duckduckgo.com": CAPTCHA, "html.duckduckgo.com": CAPTCHA})
        out = await web.web_search({"query": "x"})
        assert out["results"] == [] and "blocked" in out["error"]

    async def test_network_error_does_not_try_fallback(self, urlopen_pages):
        calls = urlopen_pages({"duckduckgo.com": urllib.error.URLError("no route to host")})
        out = await web.web_search({"query": "x"})
        assert out["results"] == [] and out["error"].startswith("request failed") and len(calls) == 1

    async def test_unrecognized_page_is_an_error_not_no_results(self, urlopen_pages, caplog):
        """Прод 14.10 15:02: страницы 8–9 КБ без ссылок и без маркеров блокировки возвращались как «no results found»,
        и модель делала вывод, что в сети ничего нет."""
        urlopen_pages({"duckduckgo.com": "<html><body>" + "<p>filler</p>" * 600 + "</body></html>"})
        with caplog.at_level("WARNING", logger="aria.web"):
            out = await web.web_search({"query": "x"})
        assert out["results"] == [] and "not recognized" in out["error"] and "NOT evidence" in out["error"]
        assert "note" not in out and "markup change" in caplog.text

    async def test_small_unrecognized_page_is_also_an_error(self, urlopen_pages):
        urlopen_pages({"duckduckgo.com": "<html><body><p>something odd</p></body></html>"})
        out = await web.web_search({"query": "x"})
        assert "not recognized" in out["error"]

    async def test_duckduckgo_saying_no_results_is_an_honest_empty_answer(self, urlopen_pages):
        urlopen_pages({"duckduckgo.com": "<html><body><div class='no-results'>No results.</div></body></html>"})
        out = await web.web_search({"query": "zzzqqq"})
        assert out["results"] == [] and out["note"] == "no results found" and not out.get("error")

    async def test_unrecognized_page_is_saved_for_debugging_and_capped(self, urlopen_pages):
        from aria import paths
        # Мелкая нестабильность набора (П.2 ТЗ): каталог debug общий на весь прогон и
        # туда пишут соседние тесты класса, поэтому saved[0] мог оказаться чужим файлом
        # и assertion падал в зависимости от порядка запуска. Чистим каталог заранее
        # и проверяем, что нужная страница сохранена хотя бы одним файлом.
        debug_dir = paths.data_dir() / "debug"
        if debug_dir.exists():
            for stale in debug_dir.glob("ddg_unrecognized_*.html"):
                stale.unlink()
        urlopen_pages({"duckduckgo.com": "<html><body><p>odd page</p></body></html>"})
        for _ in range(3):
            await web.web_search({"query": "x"})
        saved = list(debug_dir.glob("ddg_unrecognized_*.html"))
        assert 1 <= len(saved) <= 3, f"ожидали 1..3 отладочных файла, получили {len(saved)}"
        assert any("odd page" in f.read_text(encoding="utf-8") for f in saved), \
            "сохранённая страница не совпадает с той, что отдал urlopen"

    async def test_junk_arguments(self, urlopen_pages):
        urlopen_pages({"lite.duckduckgo.com": LITE})
        assert (await web.web_search({"query": None}))["error"] == "empty query"
        assert (await web.web_search({"query": "   "}))["error"] == "empty query"
        assert len((await web.web_search({"query": "x", "max_results": "abc"}))["results"]) == 4  # по умолчанию 5, в образце 4
        assert len((await web.web_search({"query": "x", "max_results": 0}))["results"]) == 1
        assert len((await web.web_search({"query": "x", "max_results": 999}))["results"]) == 4


# --------------------------------------------------------------------------- web_fetch: helpers


class FakeResp:
    def __init__(self, body: bytes, ctype: str = "text/html; charset=utf-8", status: int = 200,
                 url: str = "https://example.com/page", extra: dict | None = None):
        self._buf = io.BytesIO(body)
        self.status = status
        self._url = url
        self.headers = email.message.Message()
        if ctype:
            self.headers["Content-Type"] = ctype
        for k, v in (extra or {}).items():
            self.headers[k] = v

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_REAL_GETADDRINFO = socket.getaddrinfo  # до любых подмен


def _public_dns(host, port, *a, **k):
    """Числовые адреса резолвятся по-настоящему (127.0.0.1 остаётся 127.0.0.1), любые ИМЕНА -> публичный адрес."""
    try:
        return _REAL_GETADDRINFO(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    except (socket.gaierror, UnicodeError):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@pytest.fixture()
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)


@pytest.fixture()
def opener(monkeypatch, public_dns):
    """Подмена сетевого вызова web_fetch; возвращает (install, calls)."""
    calls: list[str] = []

    def install(resp=None, exc=None):
        def fake(req, timeout):
            calls.append(req.full_url)
            if exc is not None:
                raise exc
            return resp

        monkeypatch.setattr(web, "_open_url", fake)

    return install, calls


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>  Test   Page </title>
<style>body{color:red}</style><script>var secret = 'alert(1)';</script></head>
<body><nav>Menu</nav><h1>Hello world</h1><p>First&nbsp;paragraph with <b>bold</b> &amp; entity.</p>
<script>document.write('injected')</script><noscript>enable js</noscript>
<ul><li>item one</li><li>item two</li></ul><p>zero​width</p></body></html>"""


# --------------------------------------------------------------------------- web_fetch: extraction


class TestWebFetchExtraction:
    async def test_extracts_readable_text(self, opener):
        install, calls = opener
        install(FakeResp(PAGE.encode("utf-8")))
        out = await web.web_fetch({"url": "https://example.com/page"})
        assert not out.get("error"), out
        assert out["title"] == "Test Page" and out["status"] == 200
        assert out["final_url"] == "https://example.com/page" and out["content_type"] == "text/html"
        c = out["content"]
        assert "Hello world" in c and "First paragraph with bold & entity." in c
        assert "- item one" in c and "- item two" in c
        assert "alert(1)" not in c and "color:red" not in c and "injected" not in c and "enable js" not in c
        assert "zerowidth" in c and "​" not in c
        assert out["source_trust"] == "untrusted" and out["notice"] and out["truncated"] is False
        assert calls == ["https://example.com/page"]

    async def test_truncates_to_max_chars(self, opener):
        install, _ = opener
        install(FakeResp(("<p>" + "word " * 500 + "</p>").encode()))
        out = await web.web_fetch({"url": "https://example.com/", "max_chars": 300})
        assert len(out["content"]) == 300 and out["truncated"] is True

    async def test_byte_limit_marks_truncated(self, opener, monkeypatch):
        install, _ = opener
        monkeypatch.setattr(web, "MAX_FETCH_BYTES", 1000)
        install(FakeResp(b"<p>" + b"x" * 5000 + b"</p>"))
        out = await web.web_fetch({"url": "https://example.com/", "max_chars": 50000})
        assert out["truncated"] is True and len(out["content"]) <= 1000

    async def test_plain_text_and_json_pass_through(self, opener):
        install, _ = opener
        install(FakeResp(b'{"a": 1,\n "b": "<not html>"}', ctype="application/json"))
        out = await web.web_fetch({"url": "https://example.com/api"})
        assert out["content"] == '{"a": 1,\n "b": "<not html>"}' and out["title"] == ""

    async def test_charset_from_header_and_from_meta(self, opener):
        install, _ = opener
        text = "Привет мир"
        install(FakeResp(text.encode("cp1251"), ctype="text/plain; charset=windows-1251"))
        assert (await web.web_fetch({"url": "https://example.com/"}))["content"] == text
        html = f'<html><head><meta charset="windows-1251"></head><body><p>{text}</p></body></html>'
        install(FakeResp(html.encode("cp1251"), ctype="text/html"))
        assert text in (await web.web_fetch({"url": "https://example.com/"}))["content"]

    @pytest.mark.parametrize("ctype", ["image/png", "application/pdf", "application/octet-stream", "video/mp4"])
    async def test_rejects_non_text_content_types(self, opener, ctype):
        install, _ = opener
        install(FakeResp(b"\x89PNG....", ctype=ctype))
        out = await web.web_fetch({"url": "https://example.com/file"})
        assert "unsupported content type" in out["error"] and "content" not in out

    async def test_rejects_compressed_responses(self, opener):
        install, _ = opener
        install(FakeResp(b"\x1f\x8b....", extra={"Content-Encoding": "gzip"}))
        assert "content-encoding" in (await web.web_fetch({"url": "https://example.com/"}))["error"]

    async def test_http_and_network_errors_are_clean_answers(self, opener):
        install, _ = opener
        install(exc=urllib.error.HTTPError("https://example.com/x", 404, "Not Found", email.message.Message(), None))
        out = await web.web_fetch({"url": "https://example.com/x"})
        assert out["error"] == "http 404" and out["status"] == 404
        install(exc=urllib.error.URLError("connection reset"))
        assert (await web.web_fetch({"url": "https://example.com/x"}))["error"].startswith("request failed")
        install(exc=RuntimeError("boom"))
        assert (await web.web_fetch({"url": "https://example.com/x"}))["error"] == "fetch failed: RuntimeError"

    async def test_slow_response_hits_deadline(self, opener, monkeypatch):
        install, _ = opener
        monkeypatch.setattr(web, "FETCH_DEADLINE_SEC", -1)
        install(FakeResp(b"<p>hi</p>"))
        assert "timed out" in (await web.web_fetch({"url": "https://example.com/"}))["error"]

    async def test_junk_arguments(self, opener):
        install, calls = opener
        install(FakeResp(b"<p>hi</p>"))
        assert (await web.web_fetch({}))["error"] == "empty url"
        assert (await web.web_fetch({"url": 123}))["error"] == "empty url"
        assert (await web.web_fetch({"url": "   "}))["error"] == "empty url"
        assert not calls
        out = await web.web_fetch({"url": "https://example.com/", "max_chars": "many"})
        assert out["content"] == "hi"

    async def test_dns_failure(self, monkeypatch):
        def boom(*a, **k):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        out = await web.web_fetch({"url": "https://no-such-host.invalid/"})
        assert out["error"] == "dns resolution failed for no-such-host.invalid"


# --------------------------------------------------------------------------- web_fetch: SSRF

BLOCKED_URLS = [
    "http://127.0.0.1/", "http://127.0.0.1:8000/admin", "http://localhost/", "http://LOCALHOST./",
    "http://foo.localhost/", "http://[::1]/", "http://10.0.0.5/", "http://192.168.1.1/admin",
    "http://172.16.5.4/", "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/",
    "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://[fe80::1]/",
    "http://[fc00::1]/", "http://224.0.0.1/", "http://printer.local/", "http://db.internal/",
    "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/",
]
# экзотические записи IPv4: итог зависит от getaddrinfo платформы, но наружу запрос не уходит в любом случае
EXOTIC_URLS = ["http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/"]
BAD_SCHEMES = [
    "file:///etc/passwd", "ftp://example.com/x", "gopher://127.0.0.1:70/", "javascript:alert(1)",
    "data:text/html,hi", "//example.com/x", "example.com/x",
]


class TestWebFetchSSRF:
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_private_and_local_addresses_are_refused(self, opener, url):
        install, calls = opener
        install(FakeResp(b"secret"))
        out = await web.web_fetch({"url": url})
        assert out.get("error") and "not a public address" in out["error"] or "not allowed" in out["error"], (url, out)
        assert "content" not in out and not calls

    @pytest.mark.parametrize("url", EXOTIC_URLS)
    async def test_exotic_ipv4_forms_never_reach_the_network(self, opener, url):
        install, calls = opener
        install(FakeResp(b"secret"))
        out = await web.web_fetch({"url": url})
        assert out.get("error") and "content" not in out and not calls, (url, out)

    @pytest.mark.parametrize("url", BAD_SCHEMES)
    async def test_non_http_schemes_are_refused(self, opener, url):
        install, calls = opener
        install(FakeResp(b"secret"))
        out = await web.web_fetch({"url": url})
        assert "scheme not allowed" in out["error"] and not calls, (url, out)

    async def test_credentials_in_url_are_refused(self, opener):
        install, calls = opener
        install(FakeResp(b"x"))
        out = await web.web_fetch({"url": "http://user:pw@example.com/"})
        assert "credentials" in out["error"] and not calls

    async def test_hostless_and_malformed_urls(self, opener):
        install, calls = opener
        install(FakeResp(b"x"))
        for url in ("http://", "http://[::1", "http://example.com:99999/"):
            out = await web.web_fetch({"url": url})
            assert out.get("error") and not calls, (url, out)

    def test_null_byte_in_host_is_a_clean_refusal(self):
        with pytest.raises(web.FetchError):
            web.validate_public_url("http://exa\x00mple.com/")  # настоящий резолвер: ValueError -> FetchError

    async def test_public_name_resolving_to_private_address_is_refused(self, opener, monkeypatch):
        install, calls = opener
        install(FakeResp(b"secret"))
        monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", p))])
        out = await web.web_fetch({"url": "https://innocent.example.com/"})
        assert "not a public address" in out["error"] and not calls

    async def test_one_private_address_among_public_ones_is_enough_to_refuse(self, opener, monkeypatch):
        install, calls = opener
        install(FakeResp(b"secret"))
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                 (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
        out = await web.web_fetch({"url": "https://mixed.example.com/"})
        assert "not a public address" in out["error"] and not calls

    def test_redirect_to_private_address_is_refused_at_every_hop(self, public_dns):
        handler = web._SafeRedirectHandler()
        req = urllib.request.Request("https://example.com/a")
        ok = handler.redirect_request(req, None, 302, "Found", email.message.Message(), "https://example.com/b")
        assert ok is not None and ok.full_url == "https://example.com/b"
        for target in ("http://127.0.0.1/admin", "http://169.254.169.254/", "file:///etc/passwd", "http://[::1]:8080/"):
            with pytest.raises(web.FetchError):
                handler.redirect_request(req, None, 302, "Found", email.message.Message(), target)
        assert handler.max_redirections == web.MAX_REDIRECTS

    async def test_refused_redirect_surfaces_as_error_not_content(self, opener):
        install, _ = opener
        install(exc=web.UnsafeURLError("address 127.0.0.1 is not a public address"))
        out = await web.web_fetch({"url": "https://example.com/redir"})
        assert "not a public address" in out["error"] and "content" not in out

    async def test_final_url_after_redirect_is_checked_too(self, opener):
        install, _ = opener
        install(FakeResp(b"secret", url="http://127.0.0.1/inner"))
        out = await web.web_fetch({"url": "https://example.com/"})
        assert "not a public address" in out["error"] and "content" not in out

    async def test_real_local_server_never_receives_a_request(self):
        """Настоящий сокет: сервер на 127.0.0.1 не должен получить ни одного запроса."""
        hits: list[str] = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"internal secret")

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            out = await web.web_fetch({"url": f"http://127.0.0.1:{server.server_address[1]}/secret"})
        finally:
            server.shutdown()
            server.server_close()
        assert "not a public address" in out["error"] and "content" not in out
        assert hits == []


# --------------------------------------------------------------------------- registry / roles


class TestRegistryAndRoles:
    def test_web_fetch_is_registered_as_a_safe_read_tool(self):
        spec = TOOL_REGISTRY["web_fetch"]
        assert spec.handler is web.web_fetch and not spec.requires_approval
        assert spec.idempotency_class == IdempotencyClass.safe_read
        assert spec.input_schema["required"] == ["url"]
        assert "web_fetch" in guardrails.IDEMPOTENT_TOOL_NAMES

    @pytest.mark.parametrize("role,tools", [
        ("general", {"web_search", "web_fetch"}),
        ("orchestrator", {"web_search", "web_fetch"}),
        ("research", {"web_search", "web_fetch"}),
    ])
    def test_role_really_sees_web_tools(self, role, tools):
        """Доступность = пересечение whitelist роли и allowed_roles тула."""
        for tool in tools:
            assert tool in ROLE_REGISTRY[role].tool_whitelist, (role, tool)
            assert role in TOOL_REGISTRY[tool].allowed_roles, (role, tool)

    def test_general_stays_read_only(self):
        mutating = set(guardrails.MUTATING_TOOL_NAMES)
        assert not (set(ROLE_REGISTRY["general"].tool_whitelist) & mutating)

    def test_general_schema_exposes_web_fetch_to_the_model(self):
        names = {s["function"]["name"]: s for s in _tool_schemas_for_role(ROLE_REGISTRY["general"].tool_whitelist)}
        assert {"web_search", "web_fetch"} <= set(names)
        assert names["web_fetch"]["function"]["parameters"]["required"] == ["url"]
