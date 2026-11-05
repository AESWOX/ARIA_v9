import { Link } from "react-router-dom";
import type { StatusResponse } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

/** Gateway + session summary for the System sidebar block (no separate strip chrome). */
export function SidebarStatusStrip({ status }: SidebarStatusStripProps) {
  const { t } = useI18n();

  if (status === null) {
    return (
      <div className="px-5 py-1.5" aria-hidden>
        <div className="h-2 w-[80%] max-w-full animate-pulse rounded-sm bg-midground/10" />
      </div>
    );
  }

  const gw = gatewayLine(status, t);
  const { activeSessionsLabel, gatewayStatusLabel } = t.app;

  return (
    <div
      className={cn(
        "block text-left",
        "px-5 pb-2 pt-0.5",
        "text-text-secondary",
        "font-brand text-xs leading-snug tracking-[0.08em]",
      )}
    >
      <p className="break-words">
        <Link
          to="/channels"
          title={t.status.gateway}
          className="text-text-tertiary underline-offset-2 hover:text-midground hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground/40 focus-visible:ring-inset"
        >
          {gatewayStatusLabel}
        </Link>{" "}
        <span className={cn("font-medium", gw.tone)}>{gw.label}</span>
      </p>

      <p className="break-words">
        <Link
          to="/sessions"
          title={t.status.activeSessions}
          className="text-text-tertiary underline-offset-2 hover:text-midground hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground/40 focus-visible:ring-inset"
        >
          {activeSessionsLabel}
        </Link>{" "}
        <span className="tabular-nums text-text-secondary">{status.active_sessions}</span>
      </p>
    </div>
  );
}

export function gatewayLine(
  status: StatusResponse,
  t: ReturnType<typeof useI18n>["t"],
): { label: string; tone: string } {
  const g = t.app.gatewayStrip;
  const byState: Record<string, { label: string; tone: string }> = {
    running: { label: g.running, tone: "text-success" },
    starting: { label: g.starting, tone: "text-warning" },
    startup_failed: { label: g.failed, tone: "text-destructive" },
    stopped: { label: g.stopped, tone: "text-muted-foreground" },
  };
  if (status.gateway_state && byState[status.gateway_state]) {
    return byState[status.gateway_state];
  }
  return status.gateway_running
    ? { label: g.running, tone: "text-success" }
    : { label: g.off, tone: "text-muted-foreground" };
}

interface SidebarStatusStripProps {
  status: StatusResponse | null;
}
