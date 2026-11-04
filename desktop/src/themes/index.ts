export { ThemeProvider, useTheme } from "./context";
export { BUILTIN_THEMES, defaultTheme } from "./presets";
export {
  FONT_CHOICES,
  THEME_DEFAULT_FONT_ID,
  getFontChoice,
  isOverrideFont,
} from "./fonts";
export type { FontChoice, FontCategory } from "./fonts";
export {
  DEFAULT_SURFACE_COLOR,
  SURFACE_MODES,
  applySurface,
  initSurface,
  readSurface,
  saveSurface,
} from "./surface";
export type { SurfaceMode, SurfaceState } from "./surface";
export type { DashboardTheme, ThemeLayer, ThemeListEntry, ThemeListResponse, ThemePalette } from "./types";
