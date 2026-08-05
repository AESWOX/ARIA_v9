from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from aria.config import get_settings


def vault_root() -> Path:
    settings = get_settings()
    root = Path(settings.OBSIDIAN_VAULT_PATH)
    root.mkdir(parents=True, exist_ok=True)
    (root / "00-TASKS").mkdir(parents=True, exist_ok=True)
    return root


def _parse_frontmatter(text: str) -> dict:
    """Ported from local-agent-max toolbox/obsidian_tool.py: minimal YAML
    frontmatter parser (key: value lines between --- markers)."""
    fm: dict = {}
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            raw = text[3:end].strip()
            for line in raw.splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    fm[key.strip()] = val.strip()
    return fm


def _extract_wiki_links(text: str) -> list[str]:
    """Ported from local-agent-max toolbox/obsidian_tool.py."""
    return re.findall(r"\[\[([^\]]+)\]\]", text)


def read_note(note_name: str) -> dict:
    root = vault_root()
    candidates = list(root.rglob(f"{note_name}.md"))
    if not candidates:
        return {"found": False, "content": None, "path": None}
    path = candidates[0]
    text = path.read_text(errors="replace")
    return {
        "found": True,
        "content": text,
        "path": str(path.relative_to(root)),
        "frontmatter": _parse_frontmatter(text),
        "wiki_links": _extract_wiki_links(text),
    }


def write_note(note_name: str, content: str, folder: str = "00-TASKS") -> dict:
    root = vault_root()
    target_dir = root / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{note_name}.md"
    path.write_text(content, encoding="utf-8")
    return {"path": str(path.relative_to(root)), "bytes_written": len(content)}


def write_note_atomic(note_name: str, content: str, folder: str = "00-TASKS") -> dict:
    """Атомарная запись vault-заметки через temp + rename.

    Предотвращает half-written заметки при сбое процесса.
    Используется Stage 7 (Delivery) с file-lock из core/locking.py.
    """
    root = vault_root()
    target_dir = root / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{note_name}.md"
    tmp = path.with_suffix(f".md.tmp.{uuid4().hex[:8]}")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)  # атомарно на NTFS/Ext4
    return {"path": str(path.relative_to(root)), "bytes_written": len(content)}


def _normalize_note_path(note_path: str) -> Path:
    normalized = Path(note_path.lstrip("/"))
    if normalized.suffix == "":
        normalized = normalized.with_suffix(".md")
    return normalized


def resolve_note_path(note_path: str, create_parent: bool = False) -> Path:
    root = vault_root().resolve()
    normalized = _normalize_note_path(note_path)
    target = (root / normalized).resolve()
    if not str(target).startswith(str(root)):
        raise ValueError("path escapes vault root")
    if create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def read_note_by_path(note_path: str) -> dict:
    root = vault_root().resolve()
    path = resolve_note_path(note_path)
    if not path.exists() or not path.is_file():
        return {"found": False, "content": None, "path": str(_normalize_note_path(note_path))}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "found": True,
        "content": text,
        "path": str(path.relative_to(root)),
        "frontmatter": _parse_frontmatter(text),
        "wiki_links": _extract_wiki_links(text),
    }


def write_note_by_path(note_path: str, content: str) -> dict:
    root = vault_root().resolve()
    path = resolve_note_path(note_path, create_parent=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path.relative_to(root)), "bytes_written": len(content)}


def save_draft_tz(session_id: str, task_id: str, draft_tz_md: str) -> dict:
    """§13.3: каждый intake-драфт сохраняется в Obsidian 00-TASKS/ и связывается с session_id."""
    note_name = f"task-{task_id}"
    body = f"---\nsession_id: {session_id}\ntask_id: {task_id}\n---\n\n{draft_tz_md}\n"
    return write_note(note_name, body, folder="00-TASKS")


def list_notes(folder: str | None = None) -> list[dict]:
    root = vault_root()
    base = root / folder if folder else root
    if not base.exists():
        return []
    return [
        {"name": p.stem, "path": str(p.relative_to(root)), "size": p.stat().st_size}
        for p in sorted(base.rglob("*.md"))
    ]


def _sanitize_asset_name(file_name: str) -> str:
    candidate = Path(file_name).name.strip() or "asset"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(candidate).stem).strip("-.") or "asset"
    suffix = Path(candidate).suffix
    return f"{stem}{suffix}"


def save_binary_asset(file_name: str, content: bytes, subdir: str = ".assets") -> dict:
    root = vault_root().resolve()
    assets_dir = (root / Path(subdir.lstrip("/"))).resolve()
    if not str(assets_dir).startswith(str(root)):
        raise ValueError("path escapes vault root")
    assets_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _sanitize_asset_name(file_name)
    target = assets_dir / safe_name
    if target.exists():
        target = assets_dir / f"{Path(safe_name).stem}-{uuid4().hex[:8]}{Path(safe_name).suffix}"

    target.write_bytes(content)
    relative_path = str(target.relative_to(root)).replace("\\", "/")
    return {
        "file_name": target.name,
        "path": relative_path,
        "relative_url": relative_path,
        "size": len(content),
    }


def search_vault(pattern: str, max_results: int = 20) -> dict:
    """Case-insensitive substring search across all notes with positional metadata.

    Each match includes file_path, line, column, snippet, and textual context so
    the frontend can jump precisely to the result location.
    """
    root = vault_root()
    pattern_lower = pattern.lower()
    matches: list[dict] = []
    files_scanned = 0

    for fpath in sorted(root.rglob("*.md")):
        parts = fpath.relative_to(root).parts
        if any(p.startswith(".") for p in parts):
            continue
        files_scanned += 1
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            lowered = line.lower()
            search_from = 0
            while True:
                found_at = lowered.find(pattern_lower, search_from)
                if found_at == -1:
                    break
                start = max(0, i - 3)
                end = min(len(lines), i + 2)
                context_lines = lines[start:end]
                file_path = str(fpath.relative_to(root)).replace("\\", "/")
                snippet = line.strip() or line
                context = "\n".join(context_lines)
                matches.append(
                    {
                        "file_path": file_path,
                        "path": file_path,
                        "line": i,
                        "column": found_at + 1,
                        "snippet": snippet,
                        "context": context,
                    }
                )
                if len(matches) >= max_results:
                    return {"matches": matches, "total": len(matches), "files_scanned": files_scanned}
                search_from = found_at + max(1, len(pattern_lower))

    return {"matches": matches, "total": len(matches), "files_scanned": files_scanned}


def list_vault_tree(subdir: str = "") -> dict:
    """Ported from local-agent-max toolbox/obsidian_tool.py::list_vault.
    Lists immediate subdirs + notes of a vault directory (non-recursive),
    unlike list_notes() which recurses. Path-escape guarded like the rest
    of this module's sandbox model."""
    root = vault_root()
    target_dir = (root / subdir) if subdir else root
    target_dir = target_dir.resolve()

    if not str(target_dir).startswith(str(root.resolve())):
        return {"error": "path escapes vault root"}
    if not target_dir.exists() or not target_dir.is_dir():
        return {"error": f"directory not found: {subdir}"}

    dirs = sorted(d.name for d in target_dir.iterdir() if d.is_dir() and not d.name.startswith("."))
    notes = sorted(f.name for f in target_dir.glob("*.md"))
    return {
        "path": str(target_dir.relative_to(root)).replace("\\", "/") if subdir else "",
        "dirs": dirs,
        "notes": notes,
        "total_notes": len(notes),
        "total_dirs": len(dirs),
    }
