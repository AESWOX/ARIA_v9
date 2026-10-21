import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, KeyRound, Loader2, Sparkles, X } from "lucide-react";
import { Button } from "@vendor/ui/ui/components/button";
import { H2 } from "@vendor/ui/ui/components/typography/h2";
import { fetchJSON } from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";

/**
 * First-run wizard: provider -> key (checked against the provider) -> model -> done.
 * The key is sent only to the local backend, which forwards it to the provider's
 * own API and saves it to the user's .env on "apply". Nothing is stored in the webview.
 */

type Step = "provider" | "key" | "model" | "done";
type Provider = { id: string; label: string; url: string };
type Validation = { ok: boolean; reason: string | null; models: string[] };

const RU = {
  title: "Настройка ARIA",
  intro: "Выберите провайдера, вставьте ключ и модель. Ключ проверяется и сохраняется только на этом компьютере.",
  pickProvider: "Провайдер",
  getKey: "Получить ключ",
  keyLabel: "API-ключ",
  check: "Проверить ключ",
  checking: "Проверяю…",
  pickModel: "Модель",
  save: "Сохранить и начать",
  saving: "Сохраняю…",
  next: "Далее",
  back: "Назад",
  skip: "Пропустить",
  doneTitle: "Готово",
  doneBody: "Модель подключена. Можно писать в чат.",
  start: "Открыть чат",
  errors: {
    invalid_key: "Провайдер не принял ключ. Проверьте, что скопировали его целиком.",
    rate_limited: "Провайдер временно ограничил запросы. Подождите минуту и повторите.",
    provider_error: "Провайдер ответил ошибкой. Повторите позже.",
    network: "Нет связи с провайдером. Проверьте интернет.",
    no_models: "Для этого ключа нет доступных моделей.",
    save: "Не удалось сохранить настройки.",
  } as Record<string, string>,
};

const EN: typeof RU = {
  title: "Set up ARIA",
  intro: "Pick a provider, paste a key and choose a model. The key is checked and stored on this computer only.",
  pickProvider: "Provider",
  getKey: "Get a key",
  keyLabel: "API key",
  check: "Check key",
  checking: "Checking…",
  pickModel: "Model",
  save: "Save and start",
  saving: "Saving…",
  next: "Next",
  back: "Back",
  skip: "Skip for now",
  doneTitle: "All set",
  doneBody: "The model is connected. You can start chatting.",
  start: "Open chat",
  errors: {
    invalid_key: "The provider rejected this key. Make sure you copied all of it.",
    rate_limited: "The provider is rate limiting right now. Wait a minute and retry.",
    provider_error: "The provider returned an error. Try again later.",
    network: "Cannot reach the provider. Check your internet connection.",
    no_models: "No usable models for this key.",
    save: "Could not save the settings.",
  },
};

const STEPS: Step[] = ["provider", "key", "model", "done"];

export function SetupWizard({ onDone }: { onDone: () => void }) {
  const lang = (typeof navigator !== "undefined" ? navigator.language : "en").toLowerCase();
  const tx = lang.startsWith("ru") || lang.startsWith("uk") ? RU : EN;

  const [step, setStep] = useState<Step>("provider");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [provider, setProvider] = useState<string>("");
  const [key, setKey] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchJSON<Provider[]>("/api/setup/providers")
      .then((list) => {
        setProviders(list);
        if (list.length) setProvider(list[0].id);
      })
      .catch(() => setError(tx.errors.network));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const current = useMemo(() => providers.find((p) => p.id === provider), [providers, provider]);

  const validate = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetchJSON<Validation>("/api/setup/validate-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, key: key.trim() }),
      });
      if (!res.ok) {
        setError(tx.errors[res.reason ?? "provider_error"] ?? tx.errors.provider_error);
        return;
      }
      setModels(res.models);
      setModel(res.models[0] ?? "");
      setStep("model");
    } catch {
      setError(tx.errors.network);
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    setBusy(true);
    setError(null);
    try {
      await fetchJSON("/api/setup/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, key: key.trim(), model }),
      });
      setKey(""); // do not keep the secret in component state after saving
      setStep("done");
    } catch {
      setError(tx.errors.save);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="setup-title"
    >
      <div className={cn(themedBody, "relative w-full max-w-md border border-border bg-card shadow-2xl")}>
        {step !== "done" && (
          <Button ghost size="icon" onClick={onDone} aria-label={tx.skip} className="absolute right-2 top-2 text-muted-foreground">
            <X />
          </Button>
        )}
        <div className="p-6 flex flex-col gap-4">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-current/20 bg-secondary/40 text-midground">
            {step === "done" ? <CheckCircle2 className="h-5 w-5" /> : step === "provider" ? <Sparkles className="h-5 w-5" /> : <KeyRound className="h-5 w-5" />}
          </span>
          <H2 id="setup-title" variant="sm" mondwest className="tracking-wider uppercase">
            {step === "done" ? tx.doneTitle : tx.title}
          </H2>

          {step === "provider" && (
            <>
              <p className="text-sm text-muted-foreground">{tx.intro}</p>
              <fieldset className="flex flex-col gap-2">
                <legend className="text-xs uppercase text-muted-foreground mb-1">{tx.pickProvider}</legend>
                {providers.map((p) => (
                  <label key={p.id} className={cn("flex items-center gap-2 border border-border p-2 cursor-pointer", p.id === provider && "bg-secondary/40")}>
                    <input type="radio" name="provider" checked={p.id === provider} onChange={() => setProvider(p.id)} />
                    <span className="text-sm">{p.label}</span>
                  </label>
                ))}
              </fieldset>
            </>
          )}

          {step === "key" && (
            <>
              <label className="text-xs uppercase text-muted-foreground" htmlFor="setup-key">{tx.keyLabel}</label>
              <input
                id="setup-key"
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={key}
                onChange={(e) => setKey(e.target.value)}
                className="w-full border border-border bg-background px-3 py-2 text-sm font-mono"
              />
              {current && (
                <a href={current.url} target="_blank" rel="noreferrer" className="text-xs underline text-muted-foreground">
                  {tx.getKey}
                </a>
              )}
            </>
          )}

          {step === "model" && (
            <>
              <label className="text-xs uppercase text-muted-foreground" htmlFor="setup-model">{tx.pickModel}</label>
              <select
                id="setup-model"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="w-full border border-border bg-background px-3 py-2 text-sm"
              >
                {models.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </>
          )}

          {step === "done" && <p className="text-sm text-muted-foreground">{tx.doneBody}</p>}

          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}

          <div className="flex items-center justify-between gap-2 pt-1">
            {step !== "done" ? (
              <Button ghost onClick={onDone} className="text-muted-foreground">{tx.skip}</Button>
            ) : (
              <span />
            )}
            <div className="flex items-center gap-2">
              {(step === "key" || step === "model") && (
                <Button outlined disabled={busy} onClick={() => { setError(null); setStep(step === "key" ? "provider" : "key"); }}>
                  {tx.back}
                </Button>
              )}
              {step === "provider" && <Button disabled={!provider} onClick={() => setStep("key")}>{tx.next}</Button>}
              {step === "key" && (
                <Button disabled={busy || key.trim().length < 8} onClick={validate}>
                  {busy ? <><Loader2 className="h-4 w-4 animate-spin" /> {tx.checking}</> : tx.check}
                </Button>
              )}
              {step === "model" && (
                <Button disabled={busy || !model} onClick={apply}>{busy ? tx.saving : tx.save}</Button>
              )}
              {step === "done" && <Button onClick={onDone}>{tx.start}</Button>}
            </div>
          </div>

          <div className="flex justify-center gap-1.5" aria-hidden>
            {STEPS.map((s) => (
              <span key={s} className={cn("h-1 rounded-full transition-all", s === step ? "w-5 bg-midground" : "w-1.5 bg-muted-foreground/30")} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
