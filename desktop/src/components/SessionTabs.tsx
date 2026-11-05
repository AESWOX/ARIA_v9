import { useState } from "react";
import { Plus, X } from "lucide-react";
import type { ChatSessionRow } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

export type TabIndicator = "approve" | "unread" | "busy" | null;

export function SessionTabs({
  sessions,
  openIds,
  activeId,
  draftActive,
  indicator,
  onOpen,
  onClose,
  onNew,
  onRename,
}: {
  sessions: ChatSessionRow[];
  openIds: string[];
  activeId: string | null;
  draftActive: boolean;
  indicator: (id: string) => TabIndicator;
  onOpen: (id: string) => void;
  onClose: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
}) {
  const { t } = useI18n();
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");

  const commitRename = (id: string) => {
    const title = draftTitle.trim();
    setRenamingId(null);
    if (title) onRename(id, title);
  };

  return (
    <div className="flex min-w-0 items-center gap-1 overflow-x-auto border-b border-border pb-1">
      {openIds.map((id) => {
        const session = sessions.find((row) => row.id === id);
        if (!session) return null;
        const status = indicator(id);
        const isActive = draftActive ? false : id === activeId;
        return (
          <div
            key={id}
            className={cn(
              "group flex shrink-0 items-center gap-1 border border-border px-2 py-1 text-xs",
              isActive ? "bg-foreground/10 text-foreground" : "text-muted-foreground hover:bg-foreground/5",
            )}
            title={session.title}
            onDoubleClick={() => {
              setRenamingId(id);
              setDraftTitle(session.title);
            }}
          >
            <button type="button" className="max-w-[180px] truncate" onClick={() => onOpen(id)}>
              {renamingId === id ? (
                <input
                  autoFocus
                  className="w-[160px] border border-border bg-background px-1 text-xs"
                  value={draftTitle}
                  onChange={(event) => setDraftTitle(event.target.value)}
                  onBlur={() => commitRename(id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") commitRename(id);
                    if (event.key === "Escape") setRenamingId(null);
                  }}
                />
              ) : (
                session.title || t.common.untitled
              )}
            </button>
            {status === "approve" && <span className="h-1.5 w-1.5 rounded-full bg-amber-500" title="needs you" />}
            {status === "unread" && <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" title="new" />}
            {status === "busy" && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted-foreground" title="working" />}
            <button
              type="button"
              className="text-muted-foreground opacity-60 hover:text-foreground group-hover:opacity-100"
              aria-label={t.chatComposer.closeTab}
              onClick={() => onClose(id)}
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        );
      })}
      {draftActive && (
        <div className="shrink-0 border border-border bg-foreground/10 px-2 py-1 text-xs">
          {t.chatComposer.newChatTab}
        </div>
      )}
      <button
        type="button"
        className="shrink-0 border border-border p-1 text-muted-foreground hover:bg-foreground/5"
        aria-label={t.chatComposer.newChatTab}
        title={t.chatComposer.newChatTab}
        onClick={onNew}
      >
        <Plus className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
