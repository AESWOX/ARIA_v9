from __future__ import annotations

import re
from pathlib import Path


class PathEscapeError(Exception):
    pass


def _resolve_within(base: Path, relative: str) -> Path:
    base = base.resolve()
    target = (base / relative).resolve()
    # is_relative_to, а не startswith: «/tmp/sandbox_evil» не внутри «/tmp/sandbox» (D8)
    if not target.is_relative_to(base):
        raise PathEscapeError(f"path {relative} escapes sandbox root {base}")
    return target


def _has_git_component(rel: Path) -> bool:
    return any(part.lower() == ".git" for part in rel.parts)


async def file_read(input_json: dict, sandbox_root: str) -> dict:
    path = _resolve_within(Path(sandbox_root), input_json["path"])
    if not path.exists():
        return {"exists": False, "content": None}
    if path.is_dir():
        return {"exists": True, "is_dir": True, "entries": sorted(p.name for p in path.iterdir())}
    content = path.read_text(errors="replace")
    return {"exists": True, "is_dir": False, "content": content[:200_000]}


async def file_write(input_json: dict, sandbox_root: str) -> dict:
    path = _resolve_within(Path(sandbox_root), input_json["path"])
    # .git/hooks и .git/config — это исполнение кода при ближайшем `git commit`: запись туда только вручную
    if _has_git_component(path.relative_to(Path(sandbox_root).resolve())):
        raise PathEscapeError("запись внутрь .git запрещена (хуки и конфиг git исполняют код)")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = input_json.get("mode", "overwrite")
    content = input_json.get("content", "")

    # hash_before: только если файл уже существует
    import hashlib
    hash_before = None
    if path.exists():
        hash_before = hashlib.sha256(path.read_bytes()).hexdigest()

    if mode == "append":
        with path.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        path.write_text(content, encoding="utf-8")

    # hash_after: всегда
    hash_after = hashlib.sha256(path.read_bytes()).hexdigest()

    return {
        "path": str(path.relative_to(Path(sandbox_root).resolve())),
        "bytes_written": len(content),
        "hash_before": hash_before,
        "hash_after": hash_after,
    }


async def file_search(input_json: dict, sandbox_root: str) -> dict:
    root = Path(sandbox_root).resolve()
    pattern = input_json.get("glob", "**/*")
    norm = str(pattern).replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in norm.split("/"):
        raise PathEscapeError(f"glob {pattern!r} выходит за корень песочницы {root}")
    matches = []
    for p in root.glob(pattern):
        # симлинк внутри песочницы не должен вести наружу
        if p.is_file() and p.resolve().is_relative_to(root):
            matches.append(str(p.relative_to(root)))
    return {"matches": matches[:500], "truncated": len(matches) > 500}
