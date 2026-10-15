import { Maximize2, Minimize2, Loader2 } from "lucide-react";
import type { ChatModels, RunProfile, ProfileSeat } from "@/lib/api";
import { cn } from "@/lib/utils";

/* Top bar of the chat: what you are talking to and how, at a glance, plus the
   focus-mode toggle (hides the chat list and the app sidebar to give the
   conversation the whole window). Purely presentational: all state lives in
   NativeChatPage. */

const TIER_SHORT: Record<string, string> = { premium: "Strong", standard: "Medium", free: "Free", fast: "Fast" };
const AUDIT_SHORT = { off: "no check", light: "quick check", strict: "strict check" } as const;
const THINK_SHORT = { off: "fast", medium: "balanced", high: "deep" } as const;

export function seatLabel(seat: ProfileSeat | undefined, models: ChatModels | null): string {
  if (!seat) return "auto";
  if (seat.model) {
    const found = models?.models.find((m) => m.id === seat.model || m.model === seat.model);
    return found?.model ?? seat.model;
  }
  if (seat.tier) return TIER_SHORT[seat.tier] ?? seat.tier;
  return "auto";
}

function Chip({ children, title, tone }: { children: React.ReactNode; title?: string; tone?: "warn" }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex max-w-[16rem] items-center gap-1 truncate border border-border px-2 py-0.5 text-xs",
        tone === "warn" ? "border-amber-500/50 text-amber-500" : "text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

export function ChatTopBar({
  title, mode, profile, models, busy, waiting, costToday, focus, onToggleFocus,
}: {
  title: string;
  mode: string;
  profile: RunProfile | null;
  models: ChatModels | null;
  busy: boolean;
  waiting: boolean;
  costToday: number | null;
  focus: boolean;
  onToggleFocus: () => void;
}) {
  return (
    <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
      <div className="min-w-0 flex-1 truncate text-sm font-medium" title={title}>
        {title || "New chat"}
      </div>
      {waiting && <Chip tone="warn" title="A step is waiting for your Approve / Reject">needs you</Chip>}
      {busy && !waiting && (
        <Chip title="A task is running">
          <Loader2 className="h-3 w-3 animate-spin" /> working
        </Chip>
      )}
      <Chip title="Mode">{mode}</Chip>
      {profile && (
        <>
          <Chip title="Main model (who plans / answers)">{seatLabel(profile.main, models)}</Chip>
          <Chip title="Thinking / check">
            {THINK_SHORT[profile.thinking]} · {AUDIT_SHORT[profile.audit]}
          </Chip>
        </>
      )}
      {costToday !== null && (
        <Chip title="Estimated spend today across all chats (upper bound, cache not counted)">
          ≈ ${costToday.toFixed(2)} today
        </Chip>
      )}
      <button
        type="button"
        onClick={onToggleFocus}
        title={focus ? "Show chat list and menu (Ctrl+\\)" : "Focus: hide chat list and menu (Ctrl+\\)"}
        aria-pressed={focus}
        className="border border-border p-1 text-muted-foreground hover:bg-foreground/5"
      >
        {focus ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
      </button>
    </div>
  );
}
