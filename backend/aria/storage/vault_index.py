"""Obsidian integration layer on top of aria.storage.obsidian_vault.

* discover()    - find vaults the way Obsidian does (its own obsidian.json registry
                  + folders that contain ``.obsidian``)
* status()/structure() - read the vault's OWN config (.obsidian/*.json): daily-notes,
                  templates, attachments folders; detect conventions from folder names
* tags          - frontmatter ``tags:`` (inline list / block list / csv) and inline #tags,
                  nested tags (``project/aria`` also matches ``project``)
* decisions     - ``type: decision`` notes, #decision/#решение, ``> [!decision]`` callouts,
                  ``Decision:`` / ``Решение:`` lines, "Decisions"/"Решения" sections
* branches      - a branch = folder + ``_index.md`` (MOC); decisions are logged as notes
                  under ``<branch>/decisions`` and linked from the branch index

Pure stdlib. Everything goes through obsidian_vault.vault_root() so path-escape
guards and the live OBSIDIAN_VAULT_PATH setting apply.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from aria.storage import obsidian_vault as ov

_MAX_NOTES_COUNTED = 20000
_DECISION_TAGS = {"decision", "решение", "adr"}
_DECISION_TYPES = {"decision", "adr", "решение"}
_RESERVED_WIN = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARS = set('<>:"|?*\\\0')
_SKIP_SCAN_DIRS = {
    "node_modules", ".git", "appdata", "windows", "program files", "program files (x86)",
    "__pycache__", "venv", ".venv", "target", "dist", "build", "$recycle.bin", "library",
}

# ── frontmatter / tags ────────────────────────────────────────────────────

_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_TAG_RE = re.compile(r"(?<![\w#&/\[(])#(?=[\w/\-]*[^\W\d])([\w/\-]+)", re.UNICODE)
_WIKI_RE = re.compile(r"\[\[([^\]|#]+)")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_MARKER_RE = re.compile(
    r"^\s*(?:[-*]\s+|\d+\.\s+)?(?:\*\*)?(decision|decided|решение|решили|решено|принято решение|вердикт|итог)"
    r"(?:\*\*)?\s*[:：—-]\s*(.+?)\s*$",
    re.I,
)
_CALLOUT_RE = re.compile(r"^>\s*\[!(decision|решение)\][+-]?\s*(.*)$", re.I)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_DECISION_HEADINGS = {"decision", "decisions", "key decisions", "решение", "решения", "ключевые решения"}


def _clean_tag(t: str) -> str:
    return t.strip().lstrip("#").strip().strip("/").lower()


def _split_inline_list(val: str) -> list[str]:
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        val = val[1:-1]
    return [p.strip().strip("'\"") for p in val.split(",") if p.strip().strip("'\"")]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Minimal YAML subset: ``k: v``, ``k: [a, b]``, ``k:`` + ``- item`` lines."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, Any] = {}
    key: str | None = None
    for raw in m.group(1).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", raw) or re.match(r"^-\s+(.*)$", raw)
        if item and key is not None:
            cur = fm.get(key)
            if not isinstance(cur, list):
                cur = [] if cur in (None, "") else [cur]
            cur.append(item.group(1).strip().strip("'\""))
            fm[key] = cur
            continue
        if ":" in raw and not raw.startswith((" ", "\t")):
            k, _, v = raw.partition(":")
            key = k.strip()
            v = v.strip()
            if v.startswith("["):
                fm[key] = _split_inline_list(v)
            else:
                fm[key] = v.strip("'\"") if v else ""
    return fm, text[m.end():]


def _fm_tags(fm: dict[str, Any]) -> list[str]:
    raw = fm.get("tags", fm.get("tag", []))
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    return [t for t in (_clean_tag(x) for x in raw) if t]


def _body_lines(body: str):
    """Yield (lineno_in_body, line) outside fenced code blocks."""
    fence = False
    for i, line in enumerate(body.splitlines(), 1):
        if _FENCE_RE.match(line):
            fence = not fence
            continue
        if not fence:
            yield i, line


def _title_of(path: Path) -> str:
    return path.stem


def _extract(text: str, path: Path) -> dict[str, Any]:
    fm, body = parse_frontmatter(text)
    fm_offset = len(text.splitlines()) - len(body.splitlines())
    tags = set(_fm_tags(fm))
    links: list[str] = []
    decisions: list[dict[str, Any]] = []
    summary = ""

    lines = list(_body_lines(body))
    in_decision_section = False
    section_level = 0
    for idx, (ln, line) in enumerate(lines):
        stripped = _INLINE_CODE_RE.sub("", line)
        for t in _TAG_RE.findall(stripped):
            ct = _clean_tag(t)
            if ct:
                tags.add(ct)
        links.extend(w.strip() for w in _WIKI_RE.findall(stripped))
        if not summary and stripped.strip() and not stripped.lstrip().startswith(("#", ">", "---")):
            summary = stripped.strip()[:160]

        h = _HEADING_RE.match(line)
        if h:
            level = len(h.group(1))
            if in_decision_section and level <= section_level:
                in_decision_section = False
            if h.group(2).strip().lower().rstrip(":") in _DECISION_HEADINGS:
                in_decision_section, section_level = True, level
            continue
        if in_decision_section:
            txt = re.sub(r"^\s*(?:[-*+]\s+|\d+\.\s+)", "", line).strip()
            if txt:
                decisions.append({"line": ln + fm_offset, "kind": "section", "text": txt[:400]})
            continue
        c = _CALLOUT_RE.match(line)
        if c:
            parts = [c.group(2).strip()] if c.group(2).strip() else []
            for _, nxt in lines[idx + 1: idx + 6]:
                if not nxt.startswith(">"):
                    break
                parts.append(nxt.lstrip("> ").strip())
            if parts:
                decisions.append({"line": ln + fm_offset, "kind": "callout", "text": " ".join(parts)[:400]})
            continue
        mk = _MARKER_RE.match(line)
        if mk:
            decisions.append({"line": ln + fm_offset, "kind": "marker", "text": mk.group(2)[:400]})
            continue
        inline = {_clean_tag(t) for t in _TAG_RE.findall(stripped)}
        if inline & _DECISION_TAGS:
            txt = _TAG_RE.sub("", stripped).strip(" -*\t")
            if txt:
                decisions.append({"line": ln + fm_offset, "kind": "tag", "text": txt[:400]})

    ftype = str(fm.get("type", "")).strip().lower()
    if ftype in _DECISION_TYPES or (set(_fm_tags(fm)) & _DECISION_TAGS):
        # a decision note's "## Decision" section IS the note-level decision: don't list it twice
        sec = [d for d in decisions if d["kind"] == "section"]
        decisions = [d for d in decisions if d["kind"] != "section"]
        text_ = (str(fm.get("decision") or "").strip() or (sec[0]["text"] if sec else "")
                 or str(fm.get("title") or "").strip() or summary or _title_of(path))
        decisions.insert(0, {"line": sec[0]["line"] if sec else 1, "kind": "note", "text": text_[:400]})

    return {
        "tags": sorted(tags),
        "frontmatter": fm,
        "links": sorted(set(links)),
        "decisions": decisions,
        "summary": summary,
    }


# ── index (mtime-cached) ──────────────────────────────────────────────────

_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


def _walk_notes(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.lower().endswith(".md") and not fn.startswith("."):
                yield Path(dirpath) / fn


def _note_date(rel: str, fm: dict[str, Any], mtime: float) -> str:
    for key in ("date", "created"):
        v = str(fm.get(key, "")).strip()
        m = _DATE_RE.search(v)
        if m:
            return m.group(1)
    m = _DATE_RE.search(Path(rel).name)
    if m:
        return m.group(1)
    return _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def build_index(folder: str = "") -> list[dict[str, Any]]:
    root = ov.vault_root().resolve()
    base = ov._safe_folder(root, folder) if folder else root
    if not base.exists():
        return []
    entries: list[dict[str, Any]] = []
    for p in _walk_notes(base):
        try:
            st = p.stat()
        except OSError:
            continue
        key = str(p)
        cached = _CACHE.get(key)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            data = cached[2]
        else:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            data = _extract(text, p)
            _CACHE[key] = (st.st_mtime_ns, st.st_size, data)
        rel = str(p.relative_to(root)).replace("\\", "/")
        entries.append({
            "path": rel,
            "name": p.stem,
            "mtime": st.st_mtime,
            "date": _note_date(rel, data["frontmatter"], st.st_mtime),
            **data,
        })
    return entries


# ── tags ──────────────────────────────────────────────────────────────────

def _norm_tags(tags: list[str] | str | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    return [t for t in (_clean_tag(x) for x in tags) if t]


def _matches(note_tag: str, query: str) -> bool:
    return note_tag == query or note_tag.startswith(query + "/")


def list_tags(folder: str = "") -> list[dict[str, Any]]:
    exact: dict[str, int] = {}
    total: dict[str, set[str]] = {}
    for e in build_index(folder):
        for t in e["tags"]:
            exact[t] = exact.get(t, 0) + 1
            parts = t.split("/")
            for i in range(1, len(parts) + 1):
                total.setdefault("/".join(parts[:i]), set()).add(e["path"])
    out = [{"tag": t, "count": exact.get(t, 0), "total": len(paths)} for t, paths in total.items()]
    out.sort(key=lambda r: (-r["total"], r["tag"]))
    return out


def search_by_tags(tags: list[str] | str, mode: str = "any", folder: str = "", limit: int = 100) -> dict[str, Any]:
    want = _norm_tags(tags)
    if not want:
        raise ValueError("at least one tag is required")
    agg = all if mode == "all" else any
    hits = [
        e for e in build_index(folder)
        if agg(any(_matches(nt, q) for nt in e["tags"]) for q in want)
    ]
    hits.sort(key=lambda e: e["mtime"], reverse=True)
    return {
        "tags": want,
        "mode": "all" if mode == "all" else "any",
        "total": len(hits),
        "notes": [
            {"path": e["path"], "name": e["name"], "tags": e["tags"], "date": e["date"], "summary": e["summary"]}
            for e in hits[:limit]
        ],
    }


def paths_for_tags(tags: list[str] | str, folder: str = "") -> set[str]:
    res = search_by_tags(tags, mode="all", folder=folder, limit=10**9)
    return {n["path"] for n in res["notes"]}


# ── decisions ─────────────────────────────────────────────────────────────

def find_decisions(q: str = "", folder: str = "", tags: list[str] | str | None = None, limit: int = 50) -> dict[str, Any]:
    want = _norm_tags(tags)
    ql = q.strip().lower()
    rows: list[dict[str, Any]] = []
    for e in build_index(folder):
        if want and not all(any(_matches(nt, w) for nt in e["tags"]) for w in want):
            continue
        for d in e["decisions"]:
            if ql and ql not in d["text"].lower() and ql not in e["name"].lower():
                continue
            rows.append({
                "path": e["path"], "note": e["name"], "date": e["date"], "line": d["line"],
                "kind": d["kind"], "text": d["text"], "status": str(e["frontmatter"].get("status", "")) or None,
                "tags": e["tags"],
            })
    rows.sort(key=lambda r: (r["date"], r["path"], -r["line"]), reverse=True)
    return {"total": len(rows), "decisions": rows[:limit]}


# ── discovery / status / structure ────────────────────────────────────────

def _read_json(p: Path) -> dict[str, Any]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _obsidian_registry_files() -> list[Path]:
    home = Path.home()
    files = []
    if os.environ.get("APPDATA"):
        files.append(Path(os.environ["APPDATA"]) / "obsidian" / "obsidian.json")
    files += [
        home / "Library" / "Application Support" / "obsidian" / "obsidian.json",
        home / ".config" / "obsidian" / "obsidian.json",
        home / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json",
    ]
    extra = os.environ.get("ARIA_OBSIDIAN_CONFIG")
    if extra:
        files.insert(0, Path(extra))
    return files


def _scan_roots() -> list[Path]:
    env = os.environ.get("ARIA_VAULT_SCAN_ROOTS")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p]
    home = Path.home()
    roots = [home, home / "Documents", home / "Desktop", home / "OneDrive", home / "OneDrive" / "Documents",
             home / "Dropbox", home / "iCloud Drive", home / "Google Drive"]
    return [r for r in roots if r.exists()]


def _count_notes(path: Path) -> tuple[int, float]:
    n, newest = 0, 0.0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                n += 1
                try:
                    newest = max(newest, os.stat(os.path.join(dirpath, fn)).st_mtime)
                except OSError:
                    pass
                if n >= _MAX_NOTES_COUNTED:
                    return n, newest
    return n, newest


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def discover(max_depth: int = 3, time_budget_sec: float = 4.0) -> dict[str, Any]:
    current = _norm(ov.vault_root())
    found: dict[str, dict[str, Any]] = {}

    def add(path: Path, source: str, opened: bool = False) -> None:
        key = _norm(path)
        if key in found:
            found[key]["sources"].append(source)
            found[key]["open_in_obsidian"] |= opened
            return
        if not path.is_dir():
            return
        n, newest = _count_notes(path)
        found[key] = {
            "path": str(path), "name": path.name or str(path), "sources": [source],
            "is_obsidian_vault": (path / ".obsidian").is_dir(), "note_count": n,
            "last_modified": _dt.datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else None,
            "open_in_obsidian": opened, "current": key == current,
        }

    for cfg in _obsidian_registry_files():
        for v in (_read_json(cfg).get("vaults") or {}).values():
            if isinstance(v, dict) and v.get("path"):
                add(Path(v["path"]), "obsidian", bool(v.get("open")))

    deadline = time.monotonic() + time_budget_sec
    visited = 0
    for root in _scan_roots():
        stack = [(root, 0)]
        while stack and time.monotonic() < deadline and visited < 6000:
            d, depth = stack.pop()
            visited += 1
            try:
                with os.scandir(d) as it:
                    subs = [e for e in it if e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            if any(e.name == ".obsidian" for e in subs):
                add(d, "scan")
                continue  # do not descend into a vault
            if depth < max_depth:
                for e in subs:
                    if not e.name.startswith(".") and e.name.lower() not in _SKIP_SCAN_DIRS:
                        stack.append((Path(e.path), depth + 1))

    cur = Path(ov.vault_root())
    if _norm(cur) not in found:
        add(cur, "current")
    vaults = sorted(found.values(), key=lambda v: (not v["current"], not v["open_in_obsidian"], -(v["note_count"])))
    return {"current": str(cur), "vaults": vaults}


def _config(root: Path) -> dict[str, Any]:
    cfg_dir = root / ".obsidian"
    app = _read_json(cfg_dir / "app.json")
    daily = _read_json(cfg_dir / "daily-notes.json")
    tpl = _read_json(cfg_dir / "templates.json")
    return {
        "attachments_folder": app.get("attachmentFolderPath") or None,
        "new_note_location": app.get("newFileLocation") or None,
        "new_note_folder": app.get("newFileFolderPath") or None,
        "daily_notes_folder": daily.get("folder") or None,
        "daily_notes_format": daily.get("format") or None,
        "templates_folder": tpl.get("folder") or None,
    }


_ROLE_HINTS = {
    "daily": {"daily", "daily notes", "journal", "дневник", "ежедневные"},
    "templates": {"templates", "template", "шаблоны"},
    "attachments": {"attachments", "assets", "files", "вложения"},
    "inbox": {"inbox", "входящие"},
}


def structure() -> dict[str, Any]:
    root = ov.vault_root().resolve()
    cfg = _config(root)
    cfg_folders = {
        "daily": cfg["daily_notes_folder"], "templates": cfg["templates_folder"],
        "attachments": cfg["attachments_folder"],
    }
    folders = []
    root_notes = sum(1 for f in root.glob("*.md"))
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        n, _ = _count_notes(d)
        role = next((r for r, names in _ROLE_HINTS.items() if d.name.lower() in names), None)
        for r, cf in cfg_folders.items():
            if cf and cf.strip("/\\").lower() == d.name.lower():
                role = r
        has_index = (d / "_index.md").is_file()
        folders.append({"name": d.name, "path": d.name, "notes": n, "role": role, "branch": has_index})
    return {"root_notes": root_notes, "folders": folders, "config": cfg}


def status() -> dict[str, Any]:
    root = ov.vault_root().resolve()
    entries = build_index()
    tag_set = {t for e in entries for t in e["tags"]}
    folder_count = sum(1 for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    return {
        "path": str(root),
        "exists": root.is_dir(),
        "is_obsidian_vault": (root / ".obsidian").is_dir(),
        "note_count": len(entries),
        "folder_count": folder_count,
        "tag_count": len(tag_set),
        "decision_count": sum(len(e["decisions"]) for e in entries),
        "config": _config(root),
    }


def validate_vault_path(raw: str) -> Path:
    if not raw or not raw.strip():
        raise ValueError("path is required")
    p = Path(os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))).resolve()
    if p.parent == p:
        raise ValueError("a drive/filesystem root cannot be a vault; pick a folder")
    if _norm(p) == _norm(Path.home()):
        raise ValueError("the home folder itself cannot be a vault; pick a subfolder")
    return p


def init_obsidian_dir(path: Path) -> bool:
    """Make a plain folder recognisable as an Obsidian vault (what Obsidian does on 'open folder as vault')."""
    cfg = path / ".obsidian"
    if cfg.is_dir():
        return False
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "app.json").write_text("{}\n", encoding="utf-8")
    return True


# ── branches & decisions (write side) ─────────────────────────────────────

def _safe_rel_folder(name: str) -> str:
    segs = [s.strip() for s in (name or "").replace("\\", "/").split("/") if s.strip()]
    if not segs:
        raise ValueError("branch name is required")
    for s in segs:
        if s in (".", "..") or s.startswith(".") or s.endswith((".", " ")):
            raise ValueError(f"invalid folder name: {s!r}")
        if any(c in _BAD_CHARS for c in s) or s.lower() in _RESERVED_WIN or len(s) > 80:
            raise ValueError(f"invalid folder name: {s!r}")
    return "/".join(segs)


def _slug(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:60] or "decision"


def _fm_list(tags: list[str]) -> str:
    return "[" + ", ".join(tags) + "]"


def _clean_user_tags(tags: list[str] | str | None) -> list[str]:
    out = []
    for t in _norm_tags(tags):
        t = re.sub(r"[^\w/\-]", "", t, flags=re.UNICODE)
        if t and t not in out:
            out.append(t)
    return out


def create_branch(name: str, description: str = "", tags: list[str] | str | None = None) -> dict[str, Any]:
    rel = _safe_rel_folder(name)
    root = ov.vault_root().resolve()
    target = ov._safe_folder(root, rel)
    existed = target.exists()
    target.mkdir(parents=True, exist_ok=True)
    index = target / "_index.md"
    index_created = not index.exists()
    if index_created:
        all_tags = ["moc"] + [t for t in _clean_user_tags(tags) if t != "moc"]
        title = rel.split("/")[-1]
        desc = description.strip()
        content = (
            f"---\ntype: moc\ncreated: {_dt.date.today().isoformat()}\ntags: {_fm_list(all_tags)}\n---\n"
            f"# {title}\n\n{desc + chr(10) + chr(10) if desc else ''}## Decisions\n\n## Notes\n"
        )
        ov.write_note_atomic("_index", content, folder=rel)
    return {"path": rel, "index": f"{rel}/_index.md", "created": not existed, "index_created": index_created}


def list_branches() -> list[dict[str, Any]]:
    out = []
    for e in build_index():
        if e["name"] == "_index":
            folder = e["path"].rsplit("/", 1)[0] if "/" in e["path"] else ""
            if folder:
                out.append({"path": folder, "name": folder.split("/")[-1],
                            "notes": sum(1 for n in build_index(folder) if n["name"] != "_index")})
    out.sort(key=lambda b: b["path"])
    return out


def _link_in_index(index_rel: str, line: str) -> bool:
    root = ov.vault_root().resolve()
    p = ov.resolve_note_path(index_rel)
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    head = next((i for i, l in enumerate(lines) if l.strip().lower() == "## decisions"), None)
    if head is None:
        lines += ["", "## Decisions", line]
    else:
        end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        ins = end
        while ins > head + 1 and not lines[ins - 1].strip():
            ins -= 1
        lines.insert(ins, line)
    folder, _, name = index_rel.rpartition("/")
    ov.write_note_atomic(name[:-3] if name.endswith(".md") else name, "\n".join(lines) + "\n", folder=folder)
    _ = root
    return True


def log_decision(title: str, decision: str, context: str = "", branch: str = "",
                 tags: list[str] | str | None = None, status: str = "accepted") -> dict[str, Any]:
    title = " ".join((title or "").split())
    if not title or not (decision or "").strip():
        raise ValueError("title and decision are required")
    status = re.sub(r"[^\w-]", "", status or "accepted").lower() or "accepted"
    rel_branch = _safe_rel_folder(branch) if branch.strip() else ""
    folder = f"{rel_branch}/decisions" if rel_branch else "decisions"
    root = ov.vault_root().resolve()
    target = ov._safe_folder(root, folder)
    target.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    base = f"{today}-{_slug(title)}"
    name, n = base, 2
    while (target / f"{name}.md").exists():
        name = f"{base}-{n}"
        n += 1
    all_tags = ["decision"] + [t for t in _clean_user_tags(tags) if t != "decision"]
    body = f"---\ntype: decision\nstatus: {status}\ndate: {today}\ntags: {_fm_list(all_tags)}\n---\n# {title}\n\n## Decision\n{decision.strip()}\n"
    if context.strip():
        body += f"\n## Context\n{context.strip()}\n"
    res = ov.write_note_atomic(name, body, folder=folder)
    linked = False
    if rel_branch:
        linked = _link_in_index(f"{rel_branch}/_index.md", f"- [[{name}]] — {title}")
    _CACHE.clear()
    return {"path": res["path"].replace("\\", "/"), "name": name, "branch": rel_branch or None, "index_updated": linked}
