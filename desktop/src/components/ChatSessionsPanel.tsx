import { MessageCircle, Plus } from "lucide-react";
import type { ChatSessionRow } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

type SeenMap = Record<string, string>;

export function ChatSessionsPanel({
  sessions,
  activeId,
  seen,
  busy,
  focus,
  onOpen,
  onNew,
}: {
  sessions: ChatSessionRow[];
  activeId: string | null;
  seen: SeenMap;
  busy: boolean;
  focus: boolean;
  onOpen: (id: string) => void;
  onNew: () => void;
}) {
  const { t } = useI18n();
  if (focus) return null;

  return (
    <aside className="hidden w-[210px] shrink-0 flex-col gap-2 border border-border bg-background/40 p-2 md:flex">
      <button
        type="button"
        onClick={onNew}
        disabled={busy}
        className="flex items-center gap-1 border border-border px-2 py-1 text-left text-xs hover:bg-foreground/5 disabled:opacity-50"
      >
        <Plus className="h-3 w-3" /> {t.sessions.newChat}
      </button>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {sessions.length === 0 && <p className="p-2 text-xs text-muted-foreground">—</p>}
        {sessions.map((session) => {
          const indicator =
            (session.pending_approvals ?? 0) > 0
              ? "approve"
              : session.busy
                ? "busy"
                : session.id !== activeId && session.last_reply_at && seen[session.id] !== session.last_reply_at
                  ? "unread"
                  : null;
          return (
            <button
              key={session.id}
              type="button"
              onClick={() => onOpen(session.id)}
              title={session.title}
              className={cn(
                "flex w-full items-center gap-1 px-2 py-1.5 text-left text-xs hover:bg-foreground/5",
                session.id === activeId && "bg-foreground/10",
              )}
            >
              <MessageCircle className="h-3 w-3 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{session.title || t.sessions.untitledSession}</span>
              {indicator === "approve" && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" />}
              {indicator === "unread" && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />}
              {indicator === "busy" && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-muted-foreground" />}
            </button>
          );
        })}
      </div>
    </aside>
  );
}
