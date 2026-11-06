import { useState, type ReactNode } from "react";
import { ChevronDown, X } from "lucide-react";
import type { ChatUsage } from "@/lib/api";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { formatUsd } from "./ChatTopBar";

/* UI-2 / UI-3 feedback: the inspector is an accordion, not a tab strip.
   Overview shows cost, calls, tokens and the models used; Context shows how
   much of the model window this chat occupies. Read-only: all numbers come
   from GET /chat/sessions/{id}/usage. */

type Section = "overview" | "context";

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

function SectionHeader({
  label,
  open,
  onToggle,
}: {
  label: string;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="flex w-full items-center justify-between gap-2 border-t border-border/60 py-1.5 text-left text-muted-foreground hover:text-foreground"
    >
      <span>{label}</span>
      <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
    </button>
  );
}

export function SessionInspector({
  usage,
  title,
  onClose,
  embedded = false,
}: {
  usage: ChatUsage | null;
  title: string;
  onClose: () => void;
  embedded?: boolean;
}) {
  const { t, locale } = useI18n();
  const copy = t.chatInspector;
  const [open, setOpen] = useState<Section | null>("overview");
  const pct = usage?.context_pct ?? null;
  const bar = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const unpriced = usage?.unpriced_messages ?? 0;
  const toggle = (section: Section) => setOpen((current) => (current === section ? null : section));

  return (
    <div className={cn("bg-background/60 px-3 py-2 text-xs", !embedded && "border-b border-border")}>
      {!embedded && (
        <div className="mb-1 flex items-center gap-2">
          <span className="min-w-0 flex-1 truncate font-medium" title={title}>
            {title || "—"}
          </span>
          <button
            type="button"
            onClick={onClose}
            title={t.common.close}
            aria-label={t.common.close}
            className="border border-border p-1 text-muted-foreground hover:bg-foreground/5"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      <SectionHeader label={t.sessions.overview} open={open === "overview"} onToggle={() => toggle("overview")} />
      {open === "overview" && (
        <div className="grid gap-2 pb-1 md:grid-cols-2">
          <div className="space-y-1">
            <Row
              label={copy.cost}
              value={
                usage?.session_cost != null
                  ? `$${formatUsd(usage.session_cost)}${
                      unpriced > 0 ? ` ${copy.unpriced.replace("{count}", String(unpriced))}` : ""
                    }`
                  : "—"
              }
            />
            <Row label={t.analytics.apiCalls} value={usage?.calls ?? 0} />
            <Row
              label={t.analytics.totalTokens}
              value={
                usage
                  ? t.analytics.inOut
                      .replace("{input}", String(usage.total_prompt_tokens))
                      .replace("{output}", String(usage.total_completion_tokens))
                  : "—"
              }
            />
            <Row
              label={copy.cacheHit}
              value={usage?.cache_hit_ratio != null ? `${Math.round(usage.cache_hit_ratio * 100)}%` : "—"}
            />
          </div>
          <div>
            <div className="mb-1 text-muted-foreground">{t.analytics.perModelBreakdown}</div>
            {usage && usage.by_model.length > 0 ? (
              <ul className="space-y-0.5">
                {usage.by_model.map((model) => (
                  <li
                    key={`${model.provider_id}:${model.model ?? ""}`}
                    className="flex items-baseline justify-between gap-2"
                  >
                    <span
                      className="min-w-0 truncate"
                      title={`${model.provider_id}${model.model ? ` / ${model.model}` : ""}`}
                    >
                      {model.model || model.provider_id}
                    </span>
                    <span className="shrink-0 text-muted-foreground">
                      {model.calls} × · {model.prompt_tokens} / {model.completion_tokens}
                      {model.cost_usd != null ? ` · $${formatUsd(model.cost_usd)}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground">{copy.noCalls}</p>
            )}
          </div>
        </div>
      )}

      <SectionHeader label={copy.context} open={open === "context"} onToggle={() => toggle("context")} />
      {open === "context" && (
        <div className="space-y-1 pb-1">
          <Row
            label={copy.context}
            value={
              usage
                ? `${usage.context_tokens} / ${usage.context_window ?? "—"}${pct != null ? ` (${pct}%)` : ""}`
                : "—"
            }
          />
          <div className="h-1.5 w-full bg-foreground/10">
            <div
              className={cn("h-full", bar >= 90 ? "bg-amber-500" : "bg-foreground/40")}
              style={{ width: `${bar}%` }}
            />
          </div>
          <Row label={copy.messages} value={usage?.message_count ?? 0} />
          <Row label={copy.budget} value={usage?.context_budget ?? "—"} />
          {usage?.last_call_at && (
            <Row label={t.analytics.lastUsed} value={new Date(usage.last_call_at).toLocaleString(locale)} />
          )}
        </div>
      )}
    </div>
  );
}
