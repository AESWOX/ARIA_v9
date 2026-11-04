/**
 * UI-3: the background/surface layer that sits on top of the active theme.
 *
 * `theme` keeps the palette untouched. `white` flips the Backdrop's inversion
 * layer to get a light canvas without changing every color token. `textured`
 * adds a subtle dot grid to the backdrop. `custom` paints the canvas with a
 * user-picked dark color. The mode is persisted in localStorage and applied as
 * a `data-surface` attribute plus one CSS variable, so switching themes does
 * not reset it.
 */

export type SurfaceMode = "theme" | "white" | "textured" | "custom";

export const SURFACE_MODES: readonly SurfaceMode[] = ["theme", "white", "textured", "custom"];

const SURFACE_KEY = "aria-dashboard-surface";
const SURFACE_COLOR_KEY = "aria-dashboard-surface-color";

export const DEFAULT_SURFACE_COLOR = "#101418";
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

export interface SurfaceState {
  mode: SurfaceMode;
  color: string;
}

function isSurfaceMode(value: string | null): value is SurfaceMode {
  return value !== null && (SURFACE_MODES as readonly string[]).includes(value);
}

function normalizeColor(value: string | null): string {
  return value !== null && HEX_COLOR.test(value) ? value : DEFAULT_SURFACE_COLOR;
}

export function readSurface(): SurfaceState {
  if (typeof window === "undefined") {
    return { mode: "theme", color: DEFAULT_SURFACE_COLOR };
  }
  try {
    const mode = window.localStorage.getItem(SURFACE_KEY);
    return {
      mode: isSurfaceMode(mode) ? mode : "theme",
      color: normalizeColor(window.localStorage.getItem(SURFACE_COLOR_KEY)),
    };
  } catch {
    return { mode: "theme", color: DEFAULT_SURFACE_COLOR };
  }
}

/** Apply the surface immediately. Safe to call before React mounts. */
export function applySurface(mode: SurfaceMode, color: string): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.surface = mode;
  root.style.setProperty("--surface-custom-bg", normalizeColor(color));
}

export function saveSurface(mode: SurfaceMode, color: string): void {
  const nextColor = normalizeColor(color);
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(SURFACE_KEY, mode);
      window.localStorage.setItem(SURFACE_COLOR_KEY, nextColor);
    } catch {
      /* storage can be blocked; the look still applies for this session */
    }
  }
  applySurface(mode, nextColor);
}

/** Read and apply the saved surface; call once at startup. */
export function initSurface(): SurfaceState {
  const state = readSurface();
  applySurface(state.mode, state.color);
  return state;
}
