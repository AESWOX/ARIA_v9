import { useState } from "react";
import { Card, CardContent } from "@vendor/ui/ui/components/card";
import { useTheme } from "@/themes";
import { useI18n } from "@/i18n";
import { ThemeSettingsDialog } from "./ThemeSettingsDialog";

/* Settings → Appearance is a one-line summary plus one button. All the
   controls (theme, font, background, image dimmer, text/window/accent colors)
   live in ThemeSettingsDialog so the page does not become a wall of radios. */

export function AppearanceCard() {
  const { t } = useI18n();
  const copy = t.appearance;
  const { themeName, availableThemes, fontId } = useTheme();
  const [open, setOpen] = useState(false);
  const themeLabel = availableThemes.find((theme) => theme.name === themeName)?.label ?? themeName;

  return (
    <>
      <Card className="border-border">
        <CardContent className="flex flex-col gap-2 p-4">
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm font-medium">{copy.title}</span>
            <button
              type="button"
              className="border border-border px-3 py-1.5 text-sm hover:bg-foreground/5"
              onClick={() => setOpen(true)}
            >
              {copy.open}
            </button>
          </div>
          <span className="text-xs text-muted-foreground">
            {copy.theme}: {themeLabel} · {copy.font}: {fontId || "theme default"}
          </span>
        </CardContent>
      </Card>
      <ThemeSettingsDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
