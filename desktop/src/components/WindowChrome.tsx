import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { Copy, Maximize2, Minimize2, Minus, Square, X } from "lucide-react";
import { getCurrentWindow } from "@tauri-apps/api/window";

/** Height of the custom title bar, px. Mirrored into the --chrome-h CSS variable. */
export const CHROME_HEIGHT = 36;

/** True only inside the Tauri shell (the injected IPC bridge exists). */
export function isTauriShell(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/**
 * Title bar for the frameless window (decorations: false): drag area plus
 * minimize / maximize-restore / fullscreen / close. Renders nothing in a plain browser.
 * Fullscreen: F11 toggles, Esc leaves it.
 */
export function WindowChrome() {
  const shell = isTauriShell();
  const [maximized, setMaximized] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  useLayoutEffect(() => {
    if (!shell) return;
    document.documentElement.style.setProperty("--chrome-h", `${CHROME_HEIGHT}px`);
    return () => {
      document.documentElement.style.removeProperty("--chrome-h");
    };
  }, [shell]);

  const sync = useCallback(async () => {
    try {
      const win = getCurrentWindow();
      const [max, full] = await Promise.all([win.isMaximized(), win.isFullscreen()]);
      setMaximized(max);
      setFullscreen(full);
    } catch (err) {
      console.warn("[window-chrome] state sync failed", err);
    }
  }, []);

  useEffect(() => {
    if (!shell) return;
    void sync();
    let unlisten: (() => void) | undefined;
    let disposed = false;
    getCurrentWindow()
      .onResized(() => void sync())
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch((err) => console.warn("[window-chrome] resize listener failed", err));
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [shell, sync]);

  const run = useCallback(
    async (action: (win: ReturnType<typeof getCurrentWindow>) => Promise<unknown>) => {
      try {
        await action(getCurrentWindow());
      } catch (err) {
        console.warn("[window-chrome] action failed", err);
      } finally {
        void sync();
      }
    },
    [sync],
  );

  const toggleFullscreen = useCallback(
    () => run((win) => win.setFullscreen(!fullscreen)),
    [run, fullscreen],
  );

  useEffect(() => {
    if (!shell) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "F11") {
        event.preventDefault();
        void toggleFullscreen();
      } else if (event.key === "Escape" && fullscreen) {
        void run((win) => win.setFullscreen(false));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [shell, fullscreen, toggleFullscreen, run]);

  if (!shell) return null;

  return (
    <div className="window-chrome" data-tauri-drag-region role="toolbar" aria-label="Window">
      <div className="window-chrome__title" data-tauri-drag-region>
        <span className="window-chrome__dot" data-tauri-drag-region />
        <span data-tauri-drag-region>ARIA</span>
      </div>
      <div className="window-chrome__buttons">
        <button
          type="button"
          className="window-chrome__btn"
          aria-label="Minimize"
          title="Minimize"
          onClick={() => void run((win) => win.minimize())}
        >
          <Minus size={15} />
        </button>
        <button
          type="button"
          className="window-chrome__btn"
          aria-label={maximized ? "Restore" : "Maximize"}
          title={maximized ? "Restore" : "Maximize"}
          onClick={() => void run((win) => win.toggleMaximize())}
        >
          {maximized ? <Copy size={13} /> : <Square size={12} />}
        </button>
        <button
          type="button"
          className="window-chrome__btn"
          aria-label={fullscreen ? "Exit fullscreen" : "Fullscreen"}
          title={fullscreen ? "Exit fullscreen (Esc)" : "Fullscreen (F11)"}
          onClick={() => void toggleFullscreen()}
        >
          {fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        </button>
        <button
          type="button"
          className="window-chrome__btn window-chrome__btn--close"
          aria-label="Close"
          title="Close"
          onClick={() => void run((win) => win.close())}
        >
          <X size={16} />
        </button>
      </div>
    </div>
  );
}
