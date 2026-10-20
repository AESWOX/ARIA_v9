"""Security hardening: C1 (shell code runners), C2 (secrets in child env), H3 (/ws auth), M4 (file_search glob).

Tests were written first and fail on the code they were written against.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aria.api.auth import ticket_store, token_store
from aria.main import app
from aria.tools.handlers import files as files_handler
from aria.tools.handlers import shell as shell_handler
from aria.tools.validators import classify_shell_command

# ── C2: the child process must not inherit secrets ───────────────────────

SECRET_ENV = {
    "DEEPSEEK_API_KEY": "sk-deepseek-secret",
    "GEMINI_API_KEY": "gemini-secret",
    "GOOGLE_API_KEY": "google-secret",
    "ARIA_RUNTIME_TOKEN": "runtime-secret",
    "LOCAL_AGENT_UI_PIN": "123456",
    "GITHUB_TOKEN": "ghp_secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "OPENAI_API_KEY": "openai-secret",
    "MY_CUSTOM_PASSWORD": "pw-secret",
}


def test_build_shell_env_drops_secrets_and_keeps_path():
    base = {**SECRET_ENV, "PATH": "/usr/bin", "LANG": "C.UTF-8", "SystemRoot": r"C:\Windows"}
    env = shell_handler.build_shell_env(base)
    for name in SECRET_ENV:
        assert name not in env, f"{name} leaked into the child environment"
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["SystemRoot"] == r"C:\Windows"


def test_shell_execute_passes_scrubbed_env(monkeypatch):
    seen: dict = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _spawn(command, **kwargs):
        seen.update(kwargs)
        return _Proc()

    for k, v in SECRET_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _spawn)
    asyncio.run(shell_handler.shell_execute({"command": "echo hi"}, timeout_sec=5))
    assert "env" in seen, "create_subprocess_shell is called without env= (inherits every secret)"
    assert not (set(SECRET_ENV) & set(seen["env"]))


# ── C1: code runners are not auto-allowed ────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "python script.py",
        "python3 -m pytest -q",
        "pytest",
        "pytest tests/",
        "node app.js",
        "npm test",
        "npm run build",
    ],
)
def test_code_runners_need_approval_by_default(command, monkeypatch):
    monkeypatch.delenv("ARIA_SHELL_TRUST_CODE_RUNNERS", raising=False)
    assert classify_shell_command(command) == "approval", command


@pytest.mark.parametrize("command", ["python --version", "python3 -V", "node --version"])
def test_version_probes_stay_allowed(command, monkeypatch):
    monkeypatch.delenv("ARIA_SHELL_TRUST_CODE_RUNNERS", raising=False)
    assert classify_shell_command(command) == "allow", command


def test_owner_can_opt_in_to_trusting_code_runners(monkeypatch):
    monkeypatch.setenv("ARIA_SHELL_TRUST_CODE_RUNNERS", "1")
    assert classify_shell_command("python3 -m pytest -q") == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "echo '#!/bin/sh' > .git/hooks/pre-commit",
        "cp evil .git/hooks/pre-commit",
        "mv evil .git\\hooks\\pre-commit",
        "echo x >> .git/config",
        "cat .git/config",
    ],
)
def test_shell_cannot_touch_dot_git_without_approval(command, monkeypatch):
    monkeypatch.setenv("ARIA_SHELL_TRUST_CODE_RUNNERS", "1")
    assert classify_shell_command(command) == "approval", command


def test_dot_gitignore_is_not_dot_git(monkeypatch):
    monkeypatch.setenv("ARIA_SHELL_TRUST_CODE_RUNNERS", "1")
    assert classify_shell_command("cat .gitignore") == "allow"


@pytest.mark.parametrize("path", [".git/hooks/pre-commit", ".git/config", "sub/.git/hooks/post-commit"])
def test_file_write_refuses_dot_git(tmp_path, path):
    with pytest.raises(files_handler.PathEscapeError):
        asyncio.run(files_handler.file_write({"path": path, "content": "x"}, str(tmp_path)))
    assert not (tmp_path / path).exists()


def test_file_write_still_allows_dot_gitignore(tmp_path):
    out = asyncio.run(files_handler.file_write({"path": ".gitignore", "content": "x"}, str(tmp_path)))
    assert out["bytes_written"] == 1


# ── M4: file_search must not leave the sandbox ───────────────────────────

@pytest.mark.parametrize("pattern", ["../*", "../../*", "sub/../../*", "/etc/*", "C:/Windows/*", "..\\*"])
def test_file_search_rejects_escaping_patterns(tmp_path, pattern):
    (tmp_path / "inside.txt").write_text("x")
    with pytest.raises(files_handler.PathEscapeError):
        asyncio.run(files_handler.file_search({"glob": pattern}, str(tmp_path / "box")))


def test_file_search_drops_matches_that_resolve_outside_the_sandbox(tmp_path, monkeypatch):
    """Совпадение, которое после resolve() лежит вне песочницы (так выглядит симлинк наружу), отбрасывается.

    Без реального симлинка: на Windows их создание требует прав администратора, а тест не должен
    пропускаться (сборщик считает любой skipped нечистым результатом). Реальный симлинк эквивалентен
    пути, чей resolve() указывает за пределы корня."""
    box = (tmp_path / "box").resolve()
    outside = (tmp_path / "outside").resolve()
    box.mkdir()
    outside.mkdir()
    (box / "ok.txt").write_text("ok")
    (outside / "secret.txt").write_text("secret")

    real_glob = type(box).glob

    def fake_glob(self, pattern):
        if self == box:
            return iter([box / "ok.txt", outside / "secret.txt"])
        return real_glob(self, pattern)

    monkeypatch.setattr(type(box), "glob", fake_glob)
    out = asyncio.run(files_handler.file_search({"glob": "**/*"}, str(box)))
    assert out["matches"] == ["ok.txt"]


def test_file_search_normal_pattern_still_works(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "b.py").write_text("x")
    out = asyncio.run(files_handler.file_search({"glob": "**/*.py"}, str(tmp_path)))
    assert sorted(m.replace("\\", "/") for m in out["matches"]) == ["a.py", "d/b.py"]


# ── H3: /ws needs a credential in loopback mode too ──────────────────────

@pytest.fixture()
def tok():
    token, _pin = token_store.issue()
    return token


def test_ws_without_credentials_is_rejected_in_loopback(tok):
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008


def test_ws_with_wrong_token_is_rejected(tok):
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws?token=nope"):
            pass
    assert exc.value.code == 1008


def test_ws_with_valid_token_connects(tok):
    c = TestClient(app)
    with c.websocket_connect(f"/ws?token={tok}"):
        pass


def test_ws_with_ticket_connects_once(tok):
    c = TestClient(app)
    ticket, _ttl = ticket_store.issue()
    with c.websocket_connect(f"/ws?ticket={ticket}"):
        pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"/ws?ticket={ticket}"):
            pass


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1.evil.example", "null"])
def test_ws_rejects_foreign_origin_even_with_valid_token(tok, origin):
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(f"/ws?token={tok}", headers={"origin": origin}):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("origin", ["http://127.0.0.1:5173", "http://localhost:8765", "tauri://localhost", "http://tauri.localhost"])
def test_ws_accepts_local_origins_with_valid_token(tok, origin):
    c = TestClient(app)
    with c.websocket_connect(f"/ws?token={tok}", headers={"origin": origin}):
        pass
