"""aria/tools/handlers/documents.py — текст из офисных документов и PDF.

Без тяжёлых зависимостей: .docx/.xlsx/.pptx/.odt/.ods/.odp — это ZIP + XML,
поэтому разбираем stdlib'ом. PDF — через опциональный ``pypdf`` (если установлен).
Старые бинарные .doc/.xls/.ppt не поддерживаются: просим сохранить в новом формате.
"""
from __future__ import annotations

import asyncio
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from aria.textio import read_text_auto

MAX_ARCHIVE_ENTRY = 80 * 1024 * 1024  # защита от zip-бомб на выбранных XML
DEFAULT_MAX_CHARS = 20_000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _read_entry(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_ARCHIVE_ENTRY:
        raise ValueError(f"{name}: слишком большой XML ({info.file_size} bytes)")
    return zf.read(name)


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(_read_entry(zf, "word/document.xml"))
    paragraphs = []
    for para in root.iter(_W + "p"):
        paragraphs.append("".join(t.text or "" for t in para.iter(_W + "t")))
    return "\n".join(paragraphs)


def _xlsx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(_read_entry(zf, "xl/sharedStrings.xml"))
            for si in root.iter(_S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
        sheets = sorted(
            n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        out: list[str] = []
        for idx, sheet in enumerate(sheets, 1):
            out.append(f"# Sheet {idx}")
            root = ET.fromstring(_read_entry(zf, sheet))
            for row in root.iter(_S + "row"):
                values: list[str] = []
                for cell in row.iter(_S + "c"):
                    ctype = cell.get("t")
                    v = cell.find(_S + "v")
                    if ctype == "s" and v is not None and v.text is not None:
                        try:
                            value = shared[int(v.text)]
                        except (ValueError, IndexError):
                            value = ""
                    elif ctype == "inlineStr":
                        value = "".join(t.text or "" for t in cell.iter(_S + "t"))
                    else:
                        value = v.text if v is not None and v.text is not None else ""
                    if value:
                        values.append(value)
                if values:
                    out.append("\t".join(values))
        return "\n".join(out)


def _pptx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        slides = sorted(
            n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        )
        out: list[str] = []
        for idx, slide in enumerate(slides, 1):
            root = ET.fromstring(_read_entry(zf, slide))
            text = "\n".join(t.text or "" for t in root.iter(_A + "t")).strip()
            if text:
                out.append(f"# Slide {idx}\n{text}")
        return "\n\n".join(out)


def _odf_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(_read_entry(zf, "content.xml"))
    paragraphs: list[str] = []
    for el in root.iter():
        if el.tag.endswith("}p") or el.tag.endswith("}h"):
            text = "".join(el.itertext()).strip()
            if text:
                paragraphs.append(text)
    return "\n".join(paragraphs)


def _pdf_text(path: Path) -> tuple[str, str | None]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return "", (
            "PDF-парсер не установлен: добавь пакет pypdf в backend (requirements.txt) "
            "и пересобери приложение."
        )
    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — один битый page не должен ронять весь PDF
            text = ""
        if text.strip():
            pages.append(f"# Page {i}\n{text}")
    return "\n\n".join(pages), None


_PLAIN = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".xml", ".html", ".htm"}
_LEGACY = {".doc", ".xls", ".ppt"}


async def file_extract(input_json: dict, sandbox_root: str, **_ctx) -> dict:
    """Извлечь текст из файла внутри sandbox. Возвращает {path, kind, text, truncated}."""
    from aria.tools.handlers.files import PathEscapeError, _resolve_within

    raw = str(input_json.get("path") or "").strip()
    if not raw:
        return {"error": "path is required"}
    try:
        path = _resolve_within(Path(sandbox_root), raw)
    except PathEscapeError as exc:
        return {"error": str(exc)}
    if not path.exists() or not path.is_file():
        return {"error": f"file not found: {raw}"}

    suffix = path.suffix.lower()
    note: str | None = None
    try:
        if suffix == ".docx":
            text = await asyncio.to_thread(_docx_text, path)
        elif suffix == ".xlsx":
            text = await asyncio.to_thread(_xlsx_text, path)
        elif suffix == ".pptx":
            text = await asyncio.to_thread(_pptx_text, path)
        elif suffix in (".odt", ".ods", ".odp"):
            text = await asyncio.to_thread(_odf_text, path)
        elif suffix == ".pdf":
            text, note = await asyncio.to_thread(_pdf_text, path)
        elif suffix in _PLAIN:
            text = await asyncio.to_thread(read_text_auto, path)
        elif suffix in _LEGACY:
            return {
                "error": (
                    f"Старый формат {suffix} не читается без Office/LibreOffice. "
                    "Сохрани файл как .docx/.xlsx/.pptx и приложи снова."
                )
            }
        else:
            return {"error": f"unsupported file type: {suffix or '(no extension)'}"}
    except Exception as exc:  # noqa: BLE001 — битый документ не должен ронять задачу
        return {"error": f"extract failed: {exc}"}

    max_chars = int(input_json.get("max_chars") or DEFAULT_MAX_CHARS)
    max_chars = max(1_000, min(max_chars, 200_000))
    truncated = len(text) > max_chars
    return {
        "path": raw,
        "kind": suffix,
        "text": text[:max_chars],
        "truncated": truncated,
        "chars": len(text),
        **({"note": note} if note else {}),
    }
