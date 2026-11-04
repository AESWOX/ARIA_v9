import { useState } from "react";
import { Card, CardContent } from "@vendor/ui/ui/components/card";
import {
  FONT_CHOICES,
  SURFACE_MODES,
  THEME_DEFAULT_FONT_ID,
  readSurface,
  readSurfaceImageFile,
  saveSurface,
  useTheme,
} from "@/themes";
import type { SurfaceMode } from "@/themes";
import { useI18n } from "@/i18n";

/* UI-3: Appearance in Settings. Theme and font are the same controls the header
   switcher uses, plus the surface layer (theme / white / textured / custom)
   that the header does not expose. */

export function AppearanceCard() {
  const { t } = useI18n();
  const copy = t.appearance;
  const { themeName, availableThemes, setTheme, fontId, setFont } = useTheme();
  const [surface, setSurface] = useState(() => readSurface());

  const modeLabel: Record<SurfaceMode, string> = {
    theme: copy.modeTheme,
    white: copy.modeWhite,
    textured: copy.modeTextured,
    custom: copy.modeCustom,
    image: copy.modeImage,
  };

  const changeMode = (mode: SurfaceMode) => {
    const next = { ...surface, mode };
    setSurface(next);
    saveSurface(next.mode, next.color, next.image);
  };

  const changeColor = (color: string) => {
    const next = { ...surface, color };
    setSurface(next);
    saveSurface(next.mode, next.color, next.image);
  };

  const changeImage = async (file: File | undefined) => {
    if (!file) return;
    try {
      const image = await readSurfaceImageFile(file);
      const next = { ...surface, mode: "image" as const, image };
      setSurface(next);
      saveSurface(next.mode, next.color, next.image);
    } catch {
      /* too large / unreadable: keep the previous background */
    }
  };

  const selectClass = "border border-border bg-background/40 px-2 py-1.5 text-sm";

  return (
    <Card className="border-border">
      <CardContent className="flex flex-col gap-3 p-4">
        <span className="text-sm font-medium">{copy.title}</span>

        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs text-muted-foreground">{copy.theme}</span>
          <select className={selectClass} value={themeName} onChange={(e) => setTheme(e.target.value)}>
            {availableThemes.map((th) => (
              <option key={th.name} value={th.name}>
                {th.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs text-muted-foreground">{copy.font}</span>
          <select className={selectClass} value={fontId} onChange={(e) => setFont(e.target.value)}>
            <option value={THEME_DEFAULT_FONT_ID}>{t.theme?.fontDefault ?? "Theme default"}</option>
            {FONT_CHOICES.map((font) => (
              <option key={font.id} value={font.id}>
                {font.label}
              </option>
            ))}
          </select>
        </label>

        <div className="flex flex-col gap-1 text-sm">
          <span className="text-xs text-muted-foreground">{copy.background}</span>
          <div className="flex flex-wrap gap-3">
            {SURFACE_MODES.map((mode) => (
              <label key={mode} className="flex items-center gap-1.5">
                <input
                  type="radio"
                  name="aria-surface"
                  checked={surface.mode === mode}
                  onChange={() => changeMode(mode)}
                />
                {modeLabel[mode]}
              </label>
            ))}
          </div>
          <span className="text-xs text-muted-foreground">{copy.hint}</span>
        </div>

        {surface.mode === "custom" && (
          <label className="flex items-center gap-2 text-sm">
            <span>{copy.customColor}</span>
            <input type="color" value={surface.color} onChange={(e) => changeColor(e.target.value)} />
          </label>
        )}

        {surface.mode === "image" && (
          <label className="flex items-center gap-2 text-sm">
            <span>{copy.imageFile}</span>
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              onChange={(e) => void changeImage(e.target.files?.[0])}
            />
          </label>
        )}
      </CardContent>
    </Card>
  );
}
