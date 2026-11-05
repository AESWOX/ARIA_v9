import { useEffect, useRef, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@vendor/ui/ui/components/dialog";
import {
  FONT_CHOICES,
  SURFACE_MODES,
  THEME_DEFAULT_FONT_ID,
  applyThemeOverrides,
  readSurface,
  readSurfaceImageFile,
  readThemeOverrides,
  saveSurface,
  saveThemeOverrides,
  useTheme,
} from "@/themes";
import type { SurfaceMode } from "@/themes";
import { useI18n } from "@/i18n";
import { api } from "@/lib/api";

/* One "Theme settings" dialog: theme, font, background mode, image dimmer and
   the user's text/window-text/accent colors. The old Settings card grew into a
   wall of controls; this keeps the card short and puts everything behind one
   button, with real sliders instead of hidden presets. */

export function ThemeSettingsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const copy = t.appearance;
  const { theme, themeName, availableThemes, setTheme, fontId, setFont } = useTheme();
  const [surface, setSurface] = useState(() => readSurface());
  const [colors, setColors] = useState(() => readThemeOverrides());
  const fileInputRef = useRef<HTMLInputElement>(null);

  // The dialog is opened after the async provider hydration; read the durable
  // copy so a large image is still shown even when localStorage rejected it.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api
      .getDashboardPrefs()
      .then((prefs) => {
        if (cancelled) return;
        setSurface({
          mode: prefs.surface_mode,
          color: prefs.surface_color,
          image: prefs.surface_image,
          imageDim: prefs.surface_dim,
        });
        setColors(prefs.overrides);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [open]);

  const modeLabel: Record<SurfaceMode, string> = {
    theme: copy.modeTheme,
    white: copy.modeWhite,
    textured: copy.modeTextured,
    custom: copy.modeCustom,
    image: copy.modeImage,
  };

  const applyColors = (next: typeof colors) => {
    setColors(next);
    saveThemeOverrides(next);
    applyThemeOverrides(next, theme.palette.midground.hex, theme.palette.warmGlow);
    void api.saveDashboardPrefs({ overrides: next }).catch(() => undefined);
  };

  const changeSurface = (patch: Partial<typeof surface>) => {
    const next = { ...surface, ...patch };
    setSurface(next);
    saveSurface(next.mode, next.color, next.image, next.imageDim);
    const payload = {
      surface_mode: next.mode,
      surface_color: next.color,
      surface_dim: next.imageDim,
    } as Parameters<typeof api.saveDashboardPrefs>[0];
    // Do not resend a multi-megabyte data URL while the owner drags the dim slider.
    if (Object.prototype.hasOwnProperty.call(patch, "image")) {
      payload.surface_image = next.image;
    }
    void api.saveDashboardPrefs(payload).catch(() => undefined);
  };

  const changeImage = async (file: File | undefined) => {
    if (!file) return;
    try {
      const image = await readSurfaceImageFile(file);
      changeSurface({ mode: "image" as const, image });
    } catch {
      /* too large / unreadable: keep the previous background */
    }
  };

  const selectClass = "border border-border bg-background/40 px-2 py-1.5 text-sm";
  const colorRow = "flex items-center justify-between gap-3 text-sm";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
        </DialogHeader>

        <div className="max-h-[70vh] space-y-4 overflow-y-auto pr-1 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">{copy.theme}</span>
            <select className={selectClass} value={themeName} onChange={(e) => setTheme(e.target.value)}>
              {availableThemes.map((th) => (
                <option key={th.name} value={th.name}>
                  {th.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1">
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

          <div className="flex flex-col gap-2">
            <span className="text-xs text-muted-foreground">{copy.background}</span>
            <div className="flex flex-wrap gap-3">
              {SURFACE_MODES.map((mode) => (
                <label key={mode} className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name="aria-surface"
                    checked={surface.mode === mode}
                    onChange={() => changeSurface({ mode })}
                  />
                  {modeLabel[mode]}
                </label>
              ))}
            </div>
            <span className="text-xs text-muted-foreground">{copy.hint}</span>
          </div>

          {surface.mode === "custom" && (
            <label className={colorRow}>
              <span>{copy.customColor}</span>
              <input
                type="color"
                value={surface.color}
                onChange={(e) => changeSurface({ color: e.target.value })}
              />
            </label>
          )}

          {surface.mode === "image" && (
            <>
              <div className="space-y-2">
                <span className="text-xs text-muted-foreground">{copy.imageFile}</span>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/png,image/jpeg,image/webp,image/gif"
                  className="sr-only"
                  onChange={(e) => {
                    void changeImage(e.target.files?.[0]);
                    e.currentTarget.value = "";
                  }}
                />
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    className="border border-border bg-background/40 px-3 py-1.5 text-sm hover:bg-foreground/5"
                    onClick={() => fileInputRef.current?.click()}
                  >
                    {copy.chooseImage ?? "Choose image"}
                  </button>
                  {surface.image ? (
                    <>
                      <span className="text-xs text-muted-foreground">
                        {copy.imageSelected ?? "Image selected"}
                      </span>
                      <button
                        type="button"
                        className="text-xs text-muted-foreground underline"
                        onClick={() => changeSurface({ image: "", mode: "theme" })}
                      >
                        {copy.removeImage ?? "Remove"}
                      </button>
                    </>
                  ) : (
                    <span className="text-xs text-muted-foreground">
                      {copy.noImage ?? "No image selected"}
                    </span>
                  )}
                </div>
                {surface.image && (
                  <img
                    src={surface.image}
                    alt=""
                    className="max-h-28 w-full border border-border object-cover"
                  />
                )}
              </div>
              <label className="flex flex-col gap-1">
                <span className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>{copy.imageDim}</span>
                  <span>{surface.imageDim}%</span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={surface.imageDim}
                  onChange={(e) => changeSurface({ imageDim: Number(e.target.value) })}
                />
              </label>
            </>
          )}

          <div className="space-y-2 border-t border-border pt-3">
            <label className={colorRow}>
              <span>{copy.textColor}</span>
              <span className="flex items-center gap-2">
                <input
                  type="color"
                  value={colors.textColor || theme.palette.midground.hex}
                  onChange={(e) => applyColors({ ...colors, textColor: e.target.value })}
                />
                <button
                  type="button"
                  className="text-xs text-muted-foreground underline"
                  onClick={() => applyColors({ ...colors, textColor: "" })}
                >
                  {copy.auto}
                </button>
              </span>
            </label>

            <label className={colorRow}>
              <span>{copy.windowTextColor}</span>
              <span className="flex items-center gap-2">
                <input
                  type="color"
                  value={
                    colors.windowTextColor ||
                    colors.textColor ||
                    theme.palette.midground.hex
                  }
                  onChange={(e) => applyColors({ ...colors, windowTextColor: e.target.value })}
                />
                <button
                  type="button"
                  className="text-xs text-muted-foreground underline"
                  onClick={() => applyColors({ ...colors, windowTextColor: "" })}
                >
                  {copy.auto}
                </button>
              </span>
            </label>

            <label className={colorRow}>
              <span>{copy.accentColor}</span>
              <span className="flex items-center gap-2">
                <input
                  type="color"
                  value={colors.accentColor || "#ffbd38"}
                  onChange={(e) => applyColors({ ...colors, accentColor: e.target.value })}
                />
                <button
                  type="button"
                  className="text-xs text-muted-foreground underline"
                  onClick={() => applyColors({ ...colors, accentColor: "" })}
                >
                  {copy.auto}
                </button>
              </span>
            </label>
          </div>
        </div>

        <DialogFooter className="justify-between">
          <button
            type="button"
            className="text-xs text-muted-foreground underline"
            onClick={() => applyColors({ textColor: "", windowTextColor: "", accentColor: "" })}
          >
            {copy.reset}
          </button>
          <button
            type="button"
            className="border border-border px-3 py-1.5 text-sm"
            onClick={() => onOpenChange(false)}
          >
            {t.common.close}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
