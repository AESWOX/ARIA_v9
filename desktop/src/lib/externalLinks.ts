// В десктопе (Tauri/WebView2) window.open и <a target="_blank"> молча ничего не делают, а обычная ссылка
// увела бы окно приложения с интерфейса. Внешние http(s)-ссылки открываем через бэкенд в браузере по умолчанию.
import { fetchJSON } from "@/lib/api";

function externalWebUrl(raw: string | URL | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(String(raw), window.location.href);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (u.origin === window.location.origin) return null; // свои страницы — как раньше
    return u.href;
  } catch {
    return null;
  }
}

async function openViaBackend(url: string): Promise<void> {
  await fetchJSON("/system/open-external", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
}

const nativeOpen = window.open.bind(window);

window.open = ((url?: string | URL, target?: string, features?: string) => {
  const href = externalWebUrl(url);
  if (!href) return nativeOpen(url, target, features);
  // Бэкенд не смог (сервер без браузера, нет токена) — обычный браузер откроет вкладку сам.
  openViaBackend(href).catch(() => nativeOpen(href, target ?? "_blank", features ?? "noopener,noreferrer"));
  return null;
}) as typeof window.open;

document.addEventListener(
  "click",
  (ev) => {
    if (ev.defaultPrevented || ev.button !== 0) return;
    const anchor = (ev.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
    const href = externalWebUrl(anchor?.getAttribute("href"));
    if (!anchor || !href) return;
    ev.preventDefault();
    openViaBackend(href).catch(() => nativeOpen(href, "_blank", "noopener,noreferrer"));
  },
  true,
);
