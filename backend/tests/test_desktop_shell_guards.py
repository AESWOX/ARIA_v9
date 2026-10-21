"""Инварианты оболочки десктопа: окно без рамок, заставка загрузки, неблокирующий старт бэкенда.

Фронтенд-раннера в проекте нет (F8), поэтому проверяется содержимое файлов: то, на чём уже дважды ломался UI.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DESKTOP = REPO / "desktop"
SRC = DESKTOP / "src"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_index_html_has_script_free_splash_and_dark_background():
    html = _read(DESKTOP / "index.html")
    assert 'id="boot-splash"' in html
    # CSP: script-src 'self' — любой inline-скрипт заблокирован; заставка обязана быть чистым CSS
    inline_scripts = [m for m in re.findall(r"<script\b([^>]*)>", html) if "src=" not in m]
    assert inline_scripts == [], f"inline <script> breaks the CSP: {inline_scripts}"
    assert "#041c1c" in html.split("<body>")[0], "no dark background before first paint (white flash)"
    assert "prefers-reduced-motion" in html


def test_splash_is_not_mounted_over_the_shell_origin_and_is_dismissed():
    main = _read(SRC / "main.tsx")
    assert "tauri.localhost" in main and "onShellOrigin" in main
    assert "boot-splash" in main and "SPLASH_MIN_MS" in main
    # рендер приложения только вне origin оболочки (там нет бэкенда и токена)
    assert re.search(r"if\s*\(\s*!onShellOrigin\s*\)\s*\{[\s\S]*createRoot", main)
    assert "<WindowChrome />" in main


def test_window_is_frameless_and_csp_still_blocks_inline_scripts():
    conf = json.loads(_read(DESKTOP / "src-tauri" / "tauri.conf.json"))
    win = conf["app"]["windows"][0]
    assert win["decorations"] is False
    assert win["backgroundColor"].lower() == "#041c1c"
    assert win["resizable"] is True, "without resizable the frameless window cannot be stretched"
    script_src = re.search(r"script-src ([^;]*)", conf["app"]["security"]["csp"]).group(1)
    assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src


def test_remote_capability_is_loopback_only_and_window_apis_only():
    cap = json.loads(_read(DESKTOP / "src-tauri" / "capabilities" / "remote-window.json"))
    assert cap["windows"] == ["main"]
    assert cap["remote"]["urls"] == ["http://127.0.0.1:*", "http://localhost:*"]
    for perm in cap["permissions"]:
        assert perm == "core:event:default" or perm.startswith("core:window:allow-"), perm
    needed = {
        "core:window:allow-start-dragging",
        "core:window:allow-minimize",
        "core:window:allow-toggle-maximize",
        "core:window:allow-is-maximized",
        "core:window:allow-set-fullscreen",
        "core:window:allow-is-fullscreen",
        "core:window:allow-close",
    }
    assert needed <= set(cap["permissions"])
    # никаких updater/fs/shell для страницы, которую отдаёт локальный бэкенд
    assert not any(p.startswith(("updater:", "fs:", "shell:", "http:")) for p in cap["permissions"])


def test_window_chrome_component_exposes_all_four_controls():
    src = _read(SRC / "components" / "WindowChrome.tsx")
    for call in ("win.minimize()", "win.toggleMaximize()", "win.setFullscreen(", "win.close()"):
        assert call in src, call
    assert "data-tauri-drag-region" in src
    assert '"Escape"' in src and '"F11"' in src
    assert "isTauriShell()" in src and "if (!shell) return null" in src  # в браузере шапки нет


def test_full_height_layouts_account_for_the_title_bar():
    """h-dvh / 100vh внутри #root обрезают низ окна на высоту шапки: нужен --vh-avail."""
    offenders = []
    for path in SRC.rglob("*.tsx"):
        if "vendor" in path.parts:
            continue
        text = _read(path)
        if re.search(r"\bh-dvh\b|\bmax-h-dvh\b|100vh-", text):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], f"use h-app / var(--vh-avail): {offenders}"
    css = _read(SRC / "index.css")
    assert "--vh-avail" in css and ".h-app" in css and "--chrome-h" in css


def test_backend_launch_does_not_block_the_main_thread_in_setup():
    """setup() выполняется в главном потоке: ожидание здоровья бэкенда (до 180 с) там замораживает окно (белый экран)."""
    rs = _read(DESKTOP / "src-tauri" / "src" / "main.rs")
    setup = rs.split(".setup(|app| {", 1)[1].split(".invoke_handler", 1)[0]
    assert "launch_backend(" in setup
    assert setup.index("thread::spawn") < setup.index("launch_backend("), "launch_backend runs before the thread is spawned"
    # состояние регистрируется до потока: команды и обработчик выхода не должны упасть на отсутствующем AppState
    assert setup.index("app.manage(") < setup.index("thread::spawn")
