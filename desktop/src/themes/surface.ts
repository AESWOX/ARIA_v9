/**
 * UI-3: the background/surface layer that sits on top of the active theme.
 *
 * `theme` keeps the palette untouched. `white` flips the Backdrop's inversion
 * layer to get a light canvas without changing every color token. `textured`
 * adds a subtle dot grid to the backdrop. `custom` paints the canvas with a
 * user-picked dark color. `image` uses a user-picked local image (data URL),
 * so no third-party asset or licence is involved. The mode is persisted in
 * localStorage and applied as a `data-surface` attribute plus CSS variables,
 * so switching themes does not reset it.
 */

export type SurfaceMode = "theme" | "white" | "textured" | "custom" | "image";

export const SURFACE_MODES: readonly SurfaceMode[] = [
  "theme",
  "white",
  "textured",
  "custom",
  "image",
];

const SURFACE_KEY = "aria-dashboard-surface";
const SURFACE_COLOR_KEY = "aria-dashboard-surface-color";
const SURFACE_IMAGE_KEY = "aria-dashboard-surface-image";

export const DEFAULT_SURFACE_COLOR = "#101418";
/** Keep the data URL under the common 5 MB localStorage budget. */
export const MAX_SURFACE_IMAGE_BYTES = 3_000_000;
const HEX_COLOR = /^#[0-9a-f]{6}$/i;
const DATA_IMAGE = /^data:image\/(png|jpeg|webp|gif);base64,/i;

export interface SurfaceState {
  mode: SurfaceMode;
  color: string;
  image: string;
}

function isSurfaceMode(value: string | null): value is SurfaceMode {
  return value !== null && (SURFACE_MODES as readonly string[]).includes(value);
}

function normalizeColor(value: string | null): string {
  return value !== null && HEX_COLOR.test(value) ? value : DEFAULT_SURFACE_COLOR;
}

function normalizeImage(value: string | null): string {
  return value !== null && DATA_IMAGE.test(value) ? value : "";
}

export function readSurface(): SurfaceState {
  if (typeof window === "undefined") {
    return { mode: "theme", color: DEFAULT_SURFACE_COLOR, image: "" };
  }
  try {
    const mode = window.localStorage.getItem(SURFACE_KEY);
    return {
      mode: isSurfaceMode(mode) ? mode : "theme",
      color: normalizeColor(window.localStorage.getItem(SURFACE_COLOR_KEY)),
      image: normalizeImage(window.localStorage.getItem(SURFACE_IMAGE_KEY)),
    };
  } catch {
    return { mode: "theme", color: DEFAULT_SURFACE_COLOR, image: "" };
  }
}

/** Apply the surface immediately. Safe to call before React mounts. */
export function applySurface(mode: SurfaceMode, color: string, image = ""): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.surface = mode;
  root.style.setProperty("--surface-custom-bg", normalizeColor(color));
  const picture = mode === "image" ? normalizeImage(image) : "";
  root.style.setProperty("--surface-image", picture ? `url("${picture}")` : "none");
}

export function saveSurface(mode: SurfaceMode, color: string, image = ""): void {
  const nextColor = normalizeColor(color);
  const nextImage = mode === "image" ? normalizeImage(image) : image;
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(SURFACE_KEY, mode);
      window.localStorage.setItem(SURFACE_COLOR_KEY, nextColor);
      window.localStorage.setItem(SURFACE_IMAGE_KEY, nextImage);
    } catch {
      /* storage can be blocked or full; the look still applies for this session */
    }
  }
  applySurface(mode, nextColor, nextImage);
}

export function readSurfaceImageFile(file: File): Promise<string> {
  if (file.size > MAX_SURFACE_IMAGE_BYTES) {
    return Promise.reject(new Error("image is too large"));
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(normalizeImage(String(reader.result || "")));
    reader.onerror = () => reject(reader.error || new Error("could not read image"));
    reader.readAsDataURL(file);
  });
}

/** Read and apply the saved surface; call once at startup. */
export function initSurface(): SurfaceState {
  const state = readSurface();
  applySurface(state.mode, state.color, state.image);
  return state;
}
