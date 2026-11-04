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
  MAX_SURFACE_IMAGE_BYTES,
  SURFACE_MODES,
  applySurface,
  initSurface,
  readSurface,
  readSurfaceImageFile,
  saveSurface,
} from "./surface";
export type { SurfaceMode, SurfaceState } from "./surface";
export {
  EMPTY_OVERRIDES,
  applyThemeOverrides,
  readThemeOverrides,
  saveThemeOverrides,
} from "./overrides";
export type { ThemeOverrides } from "./overrides";
export type { DashboardTheme, ThemeLayer, ThemeListEntry, ThemeListResponse, ThemePalette } from "./types";
