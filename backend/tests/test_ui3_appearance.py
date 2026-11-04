"""UI-3: appearance settings (theme, font, surface) are wired and translated."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "desktop" / "src"
I18N = SRC / "i18n"


def test_surface_modes_are_wired_into_startup_and_css():
    surface = (SRC / "themes" / "surface.ts").read_text(encoding="utf-8")
    for mode in ("theme", "white", "textured", "custom", "image"):
        assert f'"{mode}"' in surface
    main = (SRC / "main.tsx").read_text(encoding="utf-8")
    assert "initSurface()" in main
    css = (SRC / "index.css").read_text(encoding="utf-8")
    for mode in ("white", "textured", "custom", "image"):
        assert f':root[data-surface="{mode}"]' in css, mode


def test_appearance_card_is_in_settings():
    settings = (SRC / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert "<AppearanceCard />" in settings
    card = (SRC / "components" / "AppearanceCard.tsx").read_text(encoding="utf-8")
    assert "SURFACE_MODES" in card
    assert "FONT_CHOICES" in card
    assert "setTheme" in card and "setFont" in card


def test_every_locale_has_appearance_keys():
    locale_files = [p for p in I18N.glob("*.ts") if p.name not in ("index.ts", "types.ts")]
    assert len(locale_files) == 16
    keys = (
        "title",
        "theme",
        "font",
        "background",
        "modeTheme",
        "modeWhite",
        "modeTextured",
        "modeCustom",
        "modeImage",
        "imageFile",
        "customColor",
        "hint",
    )
    for path in locale_files:
        text = path.read_text(encoding="utf-8")
        assert "appearance:" in text, path.name
        for key in keys:
            assert f"{key}:" in text, (path.name, key)
