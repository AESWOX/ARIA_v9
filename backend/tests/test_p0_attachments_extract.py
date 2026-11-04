"""P0 attach: извлечение текста из Office/ODF/PDF и опциональная транскрипция аудио."""
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

from aria.config import get_settings
from aria.core.roles import ROLE_REGISTRY
from aria.tools.handlers import audio, documents
from aria.tools.registry import TOOL_REGISTRY


def _zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def test_docx_extracts_paragraph_text(tmp_path):
    path = tmp_path / "a.docx"
    _zip(path, {
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t> world</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body></w:document>"
        ),
    })
    out = asyncio.run(documents.file_extract({"path": "a.docx"}, str(tmp_path)))
    assert "Hello world" in out["text"] and "Second paragraph" in out["text"]
    assert out["kind"] == ".docx" and out["truncated"] is False


def test_xlsx_extracts_shared_strings(tmp_path):
    path = tmp_path / "a.xlsx"
    _zip(path, {
        "xl/sharedStrings.xml": (
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>Alpha</t></si><si><t>Beta</t></si></sst>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row></sheetData></worksheet>'
        ),
    })
    out = asyncio.run(documents.file_extract({"path": "a.xlsx"}, str(tmp_path)))
    assert "Alpha\tBeta" in out["text"]


def test_pptx_extracts_slide_text(tmp_path):
    path = tmp_path / "a.pptx"
    _zip(path, {
        "ppt/slides/slide1.xml": (
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<a:p><a:r><a:t>Slide text</a:t></a:r></a:p></p:sld>"
        ),
    })
    out = asyncio.run(documents.file_extract({"path": "a.pptx"}, str(tmp_path)))
    assert "Slide text" in out["text"]


def test_odf_extracts_text(tmp_path):
    path = tmp_path / "a.odt"
    _zip(path, {
        "content.xml": (
            '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            "<text:p>ODF text</text:p></office:document-content>"
        ),
    })
    out = asyncio.run(documents.file_extract({"path": "a.odt"}, str(tmp_path)))
    assert "ODF text" in out["text"]


def test_legacy_office_gets_clear_error(tmp_path):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0")
    out = asyncio.run(documents.file_extract({"path": "old.doc"}, str(tmp_path)))
    assert "Сохрани" in out["error"] and ".docx" in out["error"]


def test_extract_rejects_sandbox_escape(tmp_path):
    out = asyncio.run(documents.file_extract({"path": "../secret.txt"}, str(tmp_path)))
    assert "escape" in out["error"].lower()


def test_audio_without_groq_key_returns_clear_error(tmp_path, monkeypatch):
    path = tmp_path / "voice.mp3"
    path.write_bytes(b"ID3" + b"\x00" * 32)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(get_settings(), "groq_api_keys", "")
    out = asyncio.run(audio.audio_transcribe({"path": "voice.mp3"}, str(tmp_path)))
    assert "GROQ_API_KEY" in out["error"]


def test_general_role_sees_attachment_tools():
    whitelist = ROLE_REGISTRY["general"].tool_whitelist
    for name in ("file_extract", "vision_analyze", "audio_transcribe"):
        assert name in whitelist, name
    assert "general" in TOOL_REGISTRY["vision_analyze"].allowed_roles
