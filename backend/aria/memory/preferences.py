"""``vault/preferences.md`` — owner preferences in the stable prompt prefix (L6).

The file is the single source of truth for three owner sections (Формат / Язык /
Не спрашивать). Its rendered block is injected into the *static* system prefix
(PR-1 prompt cache), so it must stay byte-identical between calls while the file
is unchanged: the rendered string is cached by ``(path, mtime_ns, size)``.

Writes go only through ``memory_save`` with an owner Approve (see tools/registry
and core/loop). Nothing here calls a model or the network.
"""
from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path

PREFERENCE_FILE = "preferences.md"
PREFERENCE_SECTIONS = ("Формат", "Язык", "Не спрашивать")
MAX_PREFERENCE_TOKENS = 600
CHARS_PER_TOKEN = 4.2  # эвристика ТЗ L6
MAX_PREFERENCE_CHARS = int(MAX_PREFERENCE_TOKENS * CHARS_PER_TOKEN)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_ALIASES = {name.casefold(): name for name in PREFERENCE_SECTIONS}

_CACHE: dict[str, tuple[tuple, str]] = {}
_lock = threading.Lock()


def reset_cache() -> None:
    with _lock:
        _CACHE.clear()


def preferences_path() -> Path:
    from aria.storage.obsidian_vault import vault_root

    return vault_root() / PREFERENCE_FILE


def section_name(value: object) -> str | None:
    text = str(value or "").strip().rstrip(":").strip()
    return _ALIASES.get(text.casefold())


def parse_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {name: [] for name in PREFERENCE_SECTIONS}
    current: str | None = None
    for raw in (text or "").splitlines():
        heading = _HEADING.match(raw)
        if heading:
            current = _ALIASES.get(heading.group(1).strip().casefold())
            continue
        if current is None:
            continue
        line = raw.strip()
        if line.startswith(("- ", "* ", "+ ")):
            line = line[2:].strip()
        if line:
            sections[current].append(line)
    return sections


def _cap(block: str) -> str:
    if len(block) <= MAX_PREFERENCE_CHARS:
        return block
    cut = block[:MAX_PREFERENCE_CHARS]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    return cut.rstrip()


def build_block(text: str) -> str:
    sections = parse_sections(text)
    parts = ["Owner preferences (vault/preferences.md; apply them in this conversation):"]
    for name in PREFERENCE_SECTIONS:
        lines = sections[name]
        if lines:
            parts.append(f"## {name}")
            parts.extend(f"- {line}" for line in lines)
    if len(parts) == 1:
        return ""
    return _cap("\n".join(parts))


def stable_block() -> str:
    """Rendered preferences for the static prefix; cached until the file changes."""
    path = preferences_path()
    path_key = str(path)
    try:
        stat = path.stat()
        key: tuple = (path_key, stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (path_key, None, None)
    with _lock:
        cached = _CACHE.get(path_key)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    block = build_block(text)
    with _lock:
        _CACHE[path_key] = (key, block)
    return block


def save(section: str, content: str) -> dict:
    """Append one bullet to a section (owner-approved write, temp + rename)."""
    name = section_name(section)
    body = " ".join(str(content or "").split())
    if name is None:
        raise ValueError(f"section must be one of {list(PREFERENCE_SECTIONS)}")
    if not body:
        raise ValueError("content required")

    path = preferences_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    lines = text.splitlines()
    bullet = f"- {body}"

    heading_index: int | None = None
    for index, raw in enumerate(lines):
        heading = _HEADING.match(raw)
        if heading and _ALIASES.get(heading.group(1).strip().casefold()) == name:
            heading_index = index
            break

    if heading_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"## {name}", bullet])
    else:
        end = len(lines)
        for index in range(heading_index + 1, len(lines)):
            if _HEADING.match(lines[index]):
                end = index
                break
        insert_at = end
        while insert_at > heading_index + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        existing = {line.strip() for line in lines[heading_index + 1:end] if line.strip()}
        if bullet not in existing:
            lines.insert(insert_at, bullet)

    new_text = "\n".join(lines).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".md.tmp.{uuid.uuid4().hex[:8]}")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)  # replace() (не rename) перезаписывает на Windows
    reset_cache()
    return {"path": PREFERENCE_FILE, "section": name, "bytes_written": len(new_text)}
