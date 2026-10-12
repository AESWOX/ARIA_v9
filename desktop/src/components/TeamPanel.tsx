import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import type { ChatModels, ProfileSeat, RunProfile } from "@/lib/api";
import { cn } from "@/lib/utils";

/* Team selector: who plans (boss), who executes (workers), who checks (auditor).
   Default is a model CLASS (the router picks the concrete model with fallback);
   a concrete model is under "Advanced". Only models the owner's keys really give. */

const TIER_LABEL: Record<string, string> = { premium: "Strong (smartest, costs more)", standard: "Medium", free: "Free tier", fast: "Fast and cheap" };
const AUDIT_LABEL = { off: "no check", light: "quick check", strict: "independent check" } as const;
const THINK_LABEL = { off: "fast", medium: "balanced", high: "deep" } as const;

function seatValue(seat: ProfileSeat): string {
  if (seat.model) return `m:${seat.model}`;
  if (seat.tier) return `t:${seat.tier}`;
  return "";
}

function parseSeat(v: string): ProfileSeat {
  if (v.startsWith("m:")) return { model: v.slice(2) };
  if (v.startsWith("t:")) return { tier: v.slice(2) as ProfileSeat["tier"] };
  return {};
}

function SeatSelect({
  label, seat, models, advanced, disabled, onChange,
}: {
  label: string; seat: ProfileSeat; models: ChatModels; advanced: boolean; disabled: boolean; onChange: (s: ProfileSeat) => void;
}) {
  return (
    <label className="flex items-center gap-1 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <select
        className="border border-border bg-background px-1 py-0.5 text-xs"
        value={seatValue(seat)}
        disabled={disabled}
        onChange={(e) => onChange(parseSeat(e.target.value))}
        aria-label={label}
      >
        <option value="">Auto (best available)</option>
        {Object.entries(TIER_LABEL).map(([tier, text]) => (
          <option key={tier} value={`t:${tier}`} disabled={!models.tiers[tier]}>
            {text}{models.tiers[tier] ? "" : " (no key)"}
          </option>
        ))}
        {advanced &&
          models.models.map((mdl) => (
            <option key={mdl.id} value={`m:${mdl.id}`}>
              {mdl.model} ({mdl.id})
            </option>
          ))}
      </select>
    </label>
  );
}

export function TeamPanel({
  profile, models, disabled, notes, onChange,
}: {
  profile: RunProfile; models: ChatModels | null; disabled: boolean; notes: string[]; onChange: (p: RunProfile) => void;
}) {
  const [open, setOpen] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  if (!models) return null;
  const set = <K extends keyof RunProfile>(key: K, value: RunProfile[K]) => onChange({ ...profile, [key]: value });
  const boss = profile.style === "boss";
  return (
    <div className="border-t border-border px-2 pt-1 text-xs">
      <button
        type="button"
        className="flex items-center gap-1 text-muted-foreground hover:text-foreground"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        Run setup: {boss ? "boss + helpers" : "one model"} · result: {AUDIT_LABEL[profile.audit]} · reasoning: {THINK_LABEL[profile.thinking]}
      </button>
      {open && (
        <div className="space-y-1 py-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted-foreground">Who works</span>
            {(["solo", "boss"] as const).map((st) => (
              <button
                type="button" key={st} disabled={disabled} onClick={() => set("style", st)}
                className={cn("border border-border px-2 py-0.5", profile.style === st ? "bg-foreground/10 font-medium" : "text-muted-foreground")}
                title={st === "solo" ? "One model does the whole task" : "A boss model plans and hands steps to helper models. Works in Agent / Plan modes only"}
              >
                {st === "solo" ? "One model" : "Boss + helpers"}
              </button>
            ))}
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} /> Pick exact models
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <SeatSelect label={boss ? "Boss (plans)" : "Model"} seat={profile.main} models={models} advanced={advanced} disabled={disabled} onChange={(s) => set("main", s)} />
            {boss && (
              <SeatSelect label="Helpers (do the steps)" seat={profile.workers} models={models} advanced={advanced} disabled={disabled} onChange={(s) => set("workers", s)} />
            )}
            {profile.audit === "strict" && (
              <SeatSelect label="Reviewer (checks the result)" seat={profile.auditor} models={models} advanced={advanced} disabled={disabled} onChange={(s) => set("auditor", s)} />
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1">
              <span className="text-muted-foreground">Check result</span>
              <select className="border border-border bg-background px-1 py-0.5" value={profile.audit} disabled={disabled}
                onChange={(e) => set("audit", e.target.value as RunProfile["audit"])}>
                <option value="off">No check</option>
                <option value="light">Quick (answer is complete and well-formed)</option>
                <option value="strict">Independent (a separate model re-checks it)</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              <span className="text-muted-foreground">Reasoning</span>
              <select className="border border-border bg-background px-1 py-0.5" value={profile.thinking} disabled={disabled}
                onChange={(e) => set("thinking", e.target.value as RunProfile["thinking"])}>
                <option value="off">Fast (no extra thinking)</option>
                <option value="medium">Balanced</option>
                <option value="high">Deep (slower, costs more)</option>
              </select>
            </label>
          </div>
          <div className="text-muted-foreground">
            {boss ? "Boss plans and splits the task, helpers run the steps. " : "One model plans and does everything. "}
            {profile.audit === "strict" ? "A separate reviewer model re-checks the result." : profile.audit === "light" ? "The result is checked for completeness only." : "The result is not checked."}
          </div>
          {notes.map((n) => (
            <div key={n} className="text-amber-600">{n}</div>
          ))}
        </div>
      )}
    </div>
  );
}
