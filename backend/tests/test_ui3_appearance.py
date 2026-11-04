"""UI-3: appearance settings (theme, font, surface) are wired and translated."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "desktop" / "src"
I18N = SRC / "i18n"


def test_surface_modes_are_wired_into_startup_and_css():
    surface = (SRC / "themes" / "surface.ts").read_text(encoding="utf-8")
    for mode in ("theme", "white", "textured", "custom", "image"):
        assert f'"{mode}"' in surface
    assert "--surface-image-dim" in surface
    assert "imageDim" in surface
    main = (SRC / "main.tsx").read_text(encoding="utf-8")
    assert "initSurface()" in main
    css = (SRC / "index.css").read_text(encoding="utf-8")
    for mode in ("white", "textured", "custom", "image"):
        assert f':root[data-surface="{mode}"]' in css, mode
    overrides = (SRC / "themes" / "overrides.ts").read_text(encoding="utf-8")
    assert "applyThemeOverrides" in overrides
    assert "textColor" in overrides and "windowTextColor" in overrides


def test_appearance_card_is_in_settings_and_opens_the_theme_dialog():
    settings = (SRC / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert "<AppearanceCard />" in settings
    card = (SRC / "components" / "AppearanceCard.tsx").read_text(encoding="utf-8")
    assert "ThemeSettingsDialog" in card
    dialog = (SRC / "components" / "ThemeSettingsDialog.tsx").read_text(encoding="utf-8")
    assert "SURFACE_MODES" in dialog
    assert "FONT_CHOICES" in dialog
    assert "setTheme" in dialog and "setFont" in dialog
    assert "imageDim" in dialog and "textColor" in dialog and "windowTextColor" in dialog


def test_agent_shell_card_exposes_a_partial_timeout_setting():
    from aria.config import get_settings

    settings_page = (SRC / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert "agent_shell_timeout_sec" in settings_page
    assert "Timeout, seconds" in settings_page
    assert 5 <= get_settings().agent_shell_timeout_sec <= 600


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
        "imageDim",
        "open",
        "textColor",
        "windowTextColor",
        "accentColor",
        "auto",
        "reset",
        "customColor",
        "hint",
    )
    for path in locale_files:
        text = path.read_text(encoding="utf-8")
        assert "appearance:" in text, path.name
        for key in keys:
            assert f"{key}:" in text, (path.name, key)
