import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";
import type { Locale, Translations } from "./types";
// P1.2: only the default locale is statically imported. The other 16
// dictionaries are loaded on demand via dynamic import(), so the initial
// bundle stops carrying ~70 kB of translation tables it may never use.
// Each locale becomes its own tiny chunk fetched only when selected.
import { en } from "./en";

const FALLBACK: Translations = en;

// Display metadata for the language picker — endonym (native name) so users
// recognize their language even if they don't speak the current UI language.
// Exposed as a constant so the LanguageSwitcher and any future settings page
// can share the same list.
//
// We intentionally do NOT pair locales with country flags. Languages are not
// countries (English ≠ GB, Portuguese ≠ PT, Spanish ≠ ES, Chinese variants ≠
// any single jurisdiction). Endonyms are unambiguous and avoid the political
// mismapping that flag pairings inevitably create.
export const LOCALE_META: Record<Locale, { name: string }> = {
  en: { name: "English" },
  zh: { name: "简体中文" },
  "zh-hant": { name: "繁體中文" },
  ja: { name: "日本語" },
  de: { name: "Deutsch" },
  es: { name: "Español" },
  fr: { name: "Français" },
  tr: { name: "Türkçe" },
  uk: { name: "Українська" },
  af: { name: "Afrikaans" },
  ko: { name: "한국어" },
  it: { name: "Italiano" },
  ga: { name: "Gaeilge" },
  pt: { name: "Português" },
  ru: { name: "Русский" },
  hu: { name: "Magyar" },
};

export const SUPPORTED_LOCALES = Object.keys(LOCALE_META) as Locale[];

// P1.2: dynamic import map — each entry is code-split by Vite into its own
// chunk, keeping the locale dictionaries out of the initial bundle.
const LOADERS: Record<Locale, () => Promise<Translations>> = {
  en: async () => en,
  zh: () => import("./zh").then((m) => m.zh),
  "zh-hant": () => import("./zh-hant").then((m) => m.zhHant),
  ja: () => import("./ja").then((m) => m.ja),
  de: () => import("./de").then((m) => m.de),
  es: () => import("./es").then((m) => m.es),
  fr: () => import("./fr").then((m) => m.fr),
  tr: () => import("./tr").then((m) => m.tr),
  uk: () => import("./uk").then((m) => m.uk),
  af: () => import("./af").then((m) => m.af),
  ko: () => import("./ko").then((m) => m.ko),
  it: () => import("./it").then((m) => m.it),
  ga: () => import("./ga").then((m) => m.ga),
  pt: () => import("./pt").then((m) => m.pt),
  ru: () => import("./ru").then((m) => m.ru),
  hu: () => import("./hu").then((m) => m.hu),
};

const STORAGE_KEY = "aria-locale";

function isLocale(value: string): value is Locale {
  return (SUPPORTED_LOCALES as string[]).includes(value);
}

function getInitialLocale(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && isLocale(stored)) return stored;
  } catch {
    // SSR or privacy mode
  }
  return "en";
}

interface I18nContextValue {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: Translations;
  /** True while a non-default dictionary is being fetched. */
  loading: boolean;
}

const I18nContext = createContext<I18nContextValue>({
  locale: "en",
  setLocale: () => {},
  t: FALLBACK,
  loading: false,
});

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(getInitialLocale);
  const [t, setT] = useState<Translations>(FALLBACK);
  const [loading, setLoading] = useState(false);

  // P1.2: load the dictionary for the active locale on demand. en is
  // synchronous; every other locale resolves its dynamic chunk here.
  useEffect(() => {
    let cancelled = false;
    const loader = LOADERS[locale];
    setLoading(true);
    loader()
      .then((mod) => {
        if (cancelled) return;
        setT(mod);
      })
      .catch(() => {
        if (cancelled) return;
        setT(FALLBACK);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [locale]);

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    try {
      localStorage.setItem(STORAGE_KEY, l);
    } catch {
      // ignore
    }
  }, []);

  const value: I18nContextValue = {
    locale,
    setLocale,
    t,
    loading,
  };

  return (
    <I18nContext.Provider value={value}>
      {children}
    </I18nContext.Provider>
  );
}

export function useI18n() {
  return useContext(I18nContext);
}
