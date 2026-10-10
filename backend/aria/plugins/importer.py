"""H8: импорт Agent Plugins (plugin.json + mcp.json + skills/*/SKILL.md).

Источник — локальная папка, .zip или GitHub-архив с ОБЯЗАТЕЛЬНЫМ ref (лучше SHA коммита).
Всё, что приходит из плагина, считается недоверенным:
  * MCP-серверы импортируются ОТКЛЮЧЁННЫМИ (stdio-команда — это запуск кода; включает владелец);
  * плейсхолдеры `${VAR}` в env/headers не подставляются — значения вводятся вручную;
  * архивы и папки проходят проверки: без symlink, без выхода за корень, лимиты размера/числа файлов;
  * серверы импортируются в обычный `mcp_servers.json`, поэтому default-deny политика тулов (H7) остаётся.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from aria import paths
from aria.mcp.manager import validate_server

MAX_FILES = 200
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 5_000_000
MAX_ARCHIVE_BYTES = 20_000_000

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")
_SKILL_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}|\$\([^)]*\)|\{\{[^}]*\}\}")

MANIFEST_CANDIDATES = ("plugin.json", ".claude-plugin/plugin.json", ".plugin/plugin.json")
MCP_CANDIDATES = ("mcp.json", ".mcp.json")


class PluginError(ValueError):
    """Ошибка пользователя / недоверенного входа (HTTP 400)."""


class PluginConflict(PluginError):
    """Имя занято (HTTP 409)."""


# ── источники ────────────────────────────────────────────────────────────

def _http_get(url: str) -> bytes:
    """Скачать архив с лимитом размера. Подменяется в тестах."""
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise PluginError(f"download failed: HTTP {resp.status_code}")
            buf = io.BytesIO()
            for chunk in resp.iter_bytes():
                buf.write(chunk)
                if buf.tell() > MAX_ARCHIVE_BYTES:
                    raise PluginError("archive is too large")
            return buf.getvalue()


def _extract_zip(data: bytes, dest: Path) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PluginError("not a valid zip archive") from exc
    infos = zf.infolist()
    if len(infos) > MAX_FILES * 5:
        raise PluginError("archive has too many entries")
    total = 0
    root = dest.resolve()
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name.split("/")[0]:
            raise PluginError(f"unsafe path in archive: {name}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise PluginError(f"symlink in archive: {name}")
        target = (dest / name).resolve()
        if root != target and root not in target.parents:
            raise PluginError(f"unsafe path in archive: {name}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        total += info.file_size
        if info.file_size > MAX_FILE_BYTES * 5 or total > MAX_ARCHIVE_BYTES * 2:
            raise PluginError("archive is too large when unpacked")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(info))


def _copy_tree_checked(src: Path, dest: Path) -> None:
    """Копия папки без symlink и с теми же лимитами."""
    base = src.resolve()
    files = 0
    total = 0
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            raise PluginError(f"symlink in plugin: {path.relative_to(src)}")
        resolved = path.resolve()
        if base != resolved and base not in resolved.parents:
            raise PluginError(f"path escapes plugin root: {path.relative_to(src)}")
        rel = path.relative_to(src)
        if path.is_dir():
            (dest / rel).mkdir(parents=True, exist_ok=True)
            continue
        files += 1
        size = path.stat().st_size
        total += size
        if files > MAX_FILES * 5 or total > MAX_ARCHIVE_BYTES * 2:
            raise PluginError("plugin folder is too large")
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest / rel)


def describe_source(source: dict) -> dict:
    """Нормализованное описание источника для записи об установке (без локальных путей наружу)."""
    if "github" in source:
        return {"type": "github", "repo": source["github"], "ref": source.get("ref")}
    p = str(source.get("path") or "")
    return {"type": "zip" if p.lower().endswith(".zip") else "folder", "path": p}


@contextmanager
def open_source(source: dict) -> Iterator[Path]:
    """Подготовить рабочую копию плагина во временной папке и вернуть её корень."""
    if not isinstance(source, dict):
        raise PluginError("source must be an object")
    with tempfile.TemporaryDirectory(prefix="aria-plugin-") as tmp:
        work = Path(tmp) / "src"
        work.mkdir()
        if source.get("github"):
            repo = str(source["github"]).strip()
            ref = str(source.get("ref") or "").strip()
            if not _REPO_RE.match(repo):
                raise PluginError("github must look like owner/repo")
            if not ref or not _REF_RE.match(ref) or ".." in ref:
                raise PluginError("ref (commit SHA or tag) is required for GitHub sources")
            _extract_zip(_http_get(f"https://codeload.github.com/{repo}/zip/{ref}"), work)
        elif source.get("path"):
            p = Path(str(source["path"])).expanduser()
            if not p.exists():
                raise PluginError("path does not exist")
            if p.is_file():
                if p.suffix.lower() != ".zip":
                    raise PluginError("only folders and .zip archives are supported")
                if p.stat().st_size > MAX_ARCHIVE_BYTES:
                    raise PluginError("archive is too large")
                _extract_zip(p.read_bytes(), work)
            else:
                _copy_tree_checked(p, work)
        else:
            raise PluginError("source needs 'path' or 'github' + 'ref'")
        yield _find_root(work)


def _find_root(base: Path) -> Path:
    """GitHub-архив кладёт всё в одну верхнюю папку — спускаемся в неё."""
    for _ in range(2):
        if _find_manifest(base) is not None or (base / "skills").is_dir():
            return base
        children = [c for c in base.iterdir() if c.is_dir()]
        if len(children) == 1:
            base = children[0]
        else:
            break
    return base


def _find_manifest(root: Path) -> Path | None:
    for rel in MANIFEST_CANDIDATES:
        p = root / rel
        if p.is_file():
            return p
    return None


# ── разбор ───────────────────────────────────────────────────────────────

def _slug(value: str, what: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-_")
    if not _SLUG_RE.match(s):
        raise PluginError(f"invalid {what}: {value!r}")
    return s


def _read_json(path: Path, what: str) -> Any:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise PluginError(f"{what} is too large")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise PluginError(f"{what} is not valid JSON") from exc


def _safe_subdir(root: Path, rel: str) -> Path | None:
    p = (root / rel).resolve()
    base = root.resolve()
    if base != p and base not in p.parents:
        raise PluginError(f"path escapes plugin root: {rel}")
    return p if p.is_dir() else None


def _clean_map(values: Any, notes: list[str], where: str) -> tuple[dict[str, str], list[str]]:
    """Литералы оставляем, плейсхолдеры — нет (значит, секрет надо ввести вручную)."""
    kept: dict[str, str] = {}
    need: list[str] = []
    if not isinstance(values, dict):
        return kept, need
    for k, v in values.items():
        sv = str(v)
        if _PLACEHOLDER_RE.search(sv):
            need.append(str(k))
        else:
            kept[str(k)] = sv
    if need:
        notes.append(f"{where}: values for {', '.join(need)} must be entered manually")
    return kept, need


def _convert_server(plugin: str, key: str, spec: Any, taken: set[str]) -> tuple[dict | None, dict]:
    info: dict[str, Any] = {"source_name": key, "notes": []}
    if not isinstance(spec, dict):
        info["skipped"] = "server entry must be an object"
        return None, info
    stype = str(spec.get("type") or "").lower()
    if stype == "sse":
        info["skipped"] = "legacy SSE transport is not supported yet"
        return None, info
    base = _slug(f"{plugin}-{key}", "server name")
    name, i = base, 2
    while name in taken:
        name = f"{base[:36]}-{i}"
        i += 1
    taken.add(name)
    env, need_env = _clean_map(spec.get("env"), info["notes"], "env")
    headers, need_headers = _clean_map(spec.get("headers"), info["notes"], "headers")
    payload: dict[str, Any] = {
        "name": name,
        "url": str(spec.get("url") or ""),
        "command": str(spec.get("command") or ""),
        "args": [str(a) for a in spec.get("args", [])] if isinstance(spec.get("args"), list) else [],
        "env": env,
        "headers": headers,
        "enabled": False,  # владелец включает сам, после просмотра
        "auth": "oauth" if (spec.get("auth") == "oauth" or spec.get("oauth")) else None,
    }
    try:
        server = validate_server(payload)
    except ValueError as exc:
        info["skipped"] = str(exc)
        return None, info
    info.update(
        name=name,
        transport=server["transport"],
        auth=server["auth"],
        needs_values=need_env + need_headers,
    )
    if server["transport"] == "stdio":
        info["notes"].append("stdio server runs a local command: review it before enabling")
    return server, info


def parse_plugin(root: Path, existing_server_names: set[str] | None = None) -> dict:
    manifest_path = _find_manifest(root)
    if manifest_path is None:
        raise PluginError("plugin.json not found (looked in root, .claude-plugin/, .plugin/)")
    manifest = _read_json(manifest_path, "plugin.json")
    if not isinstance(manifest, dict):
        raise PluginError("plugin.json must be an object")
    raw_name = manifest.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise PluginError("plugin.json: 'name' is required")
    name = _slug(raw_name, "plugin name")
    warnings: list[str] = []

    # MCP
    spec_map: Any = None
    inline = manifest.get("mcpServers")
    if isinstance(inline, dict):
        spec_map = inline
    else:
        rel = inline if isinstance(inline, str) else None
        candidates = ([rel] if rel else []) + list(MCP_CANDIDATES)
        for cand in candidates:
            p = (root / cand).resolve()
            if root.resolve() not in p.parents:
                raise PluginError(f"path escapes plugin root: {cand}")
            if p.is_file():
                spec_map = _read_json(p, cand)
                break
    if isinstance(spec_map, dict) and isinstance(spec_map.get("mcpServers"), dict):
        spec_map = spec_map["mcpServers"]
    servers: list[dict] = []
    server_info: list[dict] = []
    taken = set(existing_server_names or ())
    for key, spec in (spec_map or {}).items() if isinstance(spec_map, dict) else []:
        server, info = _convert_server(name, str(key), spec, taken)
        server_info.append(info)
        if server is not None:
            servers.append(server)

    # skills
    skills_rel = manifest.get("skills") if isinstance(manifest.get("skills"), str) else "skills"
    skills_dir = _safe_subdir(root, skills_rel)
    skills: list[dict] = []
    if skills_dir is not None:
        for d in sorted(skills_dir.iterdir()):
            if not d.is_dir() or not (d / "SKILL.md").is_file():
                continue
            if not _SKILL_DIR_RE.match(d.name):
                warnings.append(f"skill folder skipped (bad name): {d.name}")
                continue
            md = (d / "SKILL.md")
            if md.stat().st_size > MAX_FILE_BYTES:
                warnings.append(f"skill skipped (SKILL.md too large): {d.name}")
                continue
            text = md.read_text(encoding="utf-8", errors="replace")
            desc = re.search(r"(?im)^description:\s*(.+)$", text)
            others = [f for f in d.rglob("*") if f.is_file() and f.name != "SKILL.md"]
            scripts = [f.name for f in others if f.suffix.lower() in {".py", ".sh", ".ps1", ".js", ".bat", ".exe"}]
            try:
                skill_name = _slug(f"{name}-{d.name}", "skill name")
            except PluginError:
                warnings.append(f"skill skipped (name too long): {d.name}")
                continue
            skills.append({
                "source_name": d.name,
                "name": skill_name,
                "description": desc.group(1).strip() if desc else "",
                "files": len(others) + 1,
                "scripts": scripts,
                "_dir": str(d),
            })
            if scripts:
                warnings.append(f"skill {d.name} contains scripts: {', '.join(scripts[:5])}")
    if not skills and not servers:
        raise PluginError("plugin has no skills and no usable MCP servers")

    return {
        "name": name,
        "title": str(manifest.get("displayName") or manifest.get("title") or raw_name),
        "version": str(manifest.get("version") or ""),
        "license": str(manifest.get("license") or ""),
        "description": str(manifest.get("description") or ""),
        "skills": skills,
        "servers": server_info,
        "_servers": servers,
        "warnings": warnings,
    }


def public_view(parsed: dict) -> dict:
    out = {k: v for k, v in parsed.items() if not k.startswith("_")}
    out["skills"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in parsed["skills"]]
    return out


# ── реестр установленных ─────────────────────────────────────────────────

def registry_path() -> Path:
    return paths.data_dir() / "plugins.json"


def load_registry() -> list[dict]:
    p = registry_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("name")] if isinstance(data, list) else []


def save_registry(entries: list[dict]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stage_tree(src: Path, dest: Path) -> None:
    _copy_tree_checked(src, dest)


def copy_skill(src_dir: Path, skills_root: Path, skill_name: str) -> None:
    dest = skills_root / skill_name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _copy_tree_checked(src_dir, dest)
