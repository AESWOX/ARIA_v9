/**
 * Per-user color overrides on top of the active theme.
 *
 * ThemeProvider writes the palette as inline styles; these overrides use
 * `!important` so they survive a theme switch. Empty values mean "use the
 * theme color". `applyThemeOverrides` is called by ThemeProvider after every
 * applyTheme, which also restores the correct fallback when an override is
 * cleared (no stale inline value).
 */

const OVERRIDES_KEY = "aria-dashboard-theme-overrides";

export interface ThemeOverrides {
  /** Main text / midground color. Empty = active theme. */
  textColor: string;
  /** Window chrome (title bar) text color. Empty = follow textColor. */
  windowTextColor: string;
  /** Warm accent glow. Empty = active theme. */
  accentColor: string;
}

export const EMPTY_OVERRIDES: ThemeOverrides = {
  textColor: "",
  windowTextColor: "",
  accentColor: "",
};

const HEX_COLOR = /^#[0-9a-f]{6}$/i;

function normalize(value: unknown): string {
  return typeof value === "string" && HEX_COLOR.test(value) ? value : "";
}

export function readThemeOverrides(): ThemeOverrides {
  if (typeof window === "undefined") return { ...EMPTY_OVERRIDES };
  try {
    const raw = window.localStorage.getItem(OVERRIDES_KEY);
    if (!raw) return { ...EMPTY_OVERRIDES };
    const parsed = JSON.parse(raw) as Partial<ThemeOverrides>;
    return {
      textColor: normalize(parsed.textColor),
      windowTextColor: normalize(parsed.windowTextColor),
      accentColor: normalize(parsed.accentColor),
    };
  } catch {
    return { ...EMPTY_OVERRIDES };
  }
}

export function applyThemeOverrides(
  overrides: ThemeOverrides,
  fallbackTextColor: string,
  fallbackAccent: string,
): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  const text = overrides.textColor || fallbackTextColor;
  const windowText = overrides.windowTextColor || text;
  root.style.setProperty("--midground-base", text, "important");
  root.style.setProperty(
    "--midground",
    `color-mix(in srgb, ${text} 100%, transparent)`,
    "important",
  );
  root.style.setProperty("--window-text-color", windowText, "important");
  root.style.setProperty(
    "--warm-glow",
    overrides.accentColor || fallbackAccent,
    "important",
  );
}

export function saveThemeOverrides(overrides: ThemeOverrides): void {
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(OVERRIDES_KEY, JSON.stringify(overrides));
    } catch {
      /* storage can be blocked; the look still applies for this session */
    }
  }
}
