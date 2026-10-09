import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import type { ChatModels, ProfileSeat, RunProfile } from "@/lib/api";
import { cn } from "@/lib/utils";

/* Team selector: who plans (boss), who executes (workers), who checks (auditor).
   Default is a model CLASS (the router picks the concrete model with fallback);
   a concrete model is under "Advanced". Only models the owner's keys really give. */

const TIER_LABEL: Record<string, string> = { premium: "Strong", standard: "Medium", free: "Free", fast: "Fast / cheap" };

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
        <option value="">Auto</option>
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
        Team: {boss ? "boss + subs" : "solo"}, audit {profile.audit}, thinking {profile.thinking}
      </button>
      {open && (
        <div className="space-y-1 py-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted-foreground">Style</span>
            {(["solo", "boss"] as const).map((st) => (
              <button
                type="button" key={st} disabled={disabled} onClick={() => set("style", st)}
                className={cn("border border-border px-2 py-0.5", profile.style === st ? "bg-foreground/10 font-medium" : "text-muted-foreground")}
                title={st === "solo" ? "One model does everything" : "Boss plans and delegates to subs (agent / plan modes)"}
              >
                {st === "solo" ? "Solo" : "Boss + subs"}
              </button>
            ))}
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} /> Advanced (pick a model)
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <SeatSelect label={boss ? "Boss" : "Model"} seat={profile.main} models={models} advanced={advanced} disabled={disabled} onChange={(s) => set("main", s)} />
            {boss && (
              <SeatSelect label="Subs" seat={profile.workers} models={models} advanced={advanced} disabled={disabled} onChange={(s) => set("workers", s)} />
            )}
            {profile.audit === "strict" && (
              <SeatSelect label="Auditor" seat={profile.auditor} models={models} advanced={advanced} disabled={disabled} onChange={(s) => set("auditor", s)} />
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1">
              <span className="text-muted-foreground">Audit</span>
              <select className="border border-border bg-background px-1 py-0.5" value={profile.audit} disabled={disabled}
                onChange={(e) => set("audit", e.target.value as RunProfile["audit"])}>
                <option value="off">Off</option>
                <option value="light">Light (structure only)</option>
                <option value="strict">Strict (separate model)</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              <span className="text-muted-foreground">Thinking</span>
              <select className="border border-border bg-background px-1 py-0.5" value={profile.thinking} disabled={disabled}
                onChange={(e) => set("thinking", e.target.value as RunProfile["thinking"])}>
                <option value="off">Off</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
              </select>
            </label>
          </div>
          {notes.map((n) => (
            <div key={n} className="text-amber-600">{n}</div>
          ))}
        </div>
      )}
    </div>
  );
}
