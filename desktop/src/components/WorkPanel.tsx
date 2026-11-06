import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { FolderOpen, ListTree, Paperclip, X } from "lucide-react";
import type { ChatMsg, ChatUsage } from "@/lib/api";
import { api } from "@/lib/api";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { Button } from "@vendor/ui/ui/components/button";
import { SessionInspector } from "@/components/SessionInspector";
import { TaskSpecPanel, type ClarifyQuestion } from "@/components/TaskSpecPanel";
import type { ComposerAttachment } from "@/components/ChatComposer";

type Tab = "plan" | "files" | "info";

function collectPaths(messages: ChatMsg[]): string[] {
  const found = new Set<string>();
  for (const message of messages) {
    if (message.role !== "tool") continue;
    const tool = message.tool_output;
    if (!tool) continue;
    for (const source of [tool.arguments, tool.output]) {
      if (!source) continue;
      for (const key of ["path", "file", "target", "output_path"]) {
        const value = source[key];
        if (typeof value === "string" && value.trim()) found.add(value.trim());
      }
    }
  }
  return Array.from(found).slice(0, 50);
}

export function WorkPanel({
  title,
  taskId,
  taskStatus,
  sessionId,
  questions,
  messages,
  attachments,
  usage,
  busy,
  mode,
  onClose,
  onAnswered,
  onError,
  onSwitchToPlan,
}: {
  title: string;
  taskId: string | null;
  taskStatus: string | null;
  sessionId: string | null;
  questions: ClarifyQuestion[] | null;
  messages: ChatMsg[];
  attachments: ComposerAttachment[];
  usage: ChatUsage | null;
  busy: boolean;
  mode: "chat" | "plan";
  onClose: () => void;
  onAnswered: () => void;
  onError: (message: string) => void;
  onSwitchToPlan: () => void;
}) {
  const { t } = useI18n();
  const [tab, setTab] = useState<Tab>(taskId ? "plan" : "files");
  const paths = useMemo(() => collectPaths(messages), [messages]);

  useEffect(() => {
    if (taskId) setTab("plan");
  }, [taskId]);

  const tabs: { id: Tab; label: string; icon: typeof ListTree }[] = [
    { id: "plan", label: t.chatPanel.plan, icon: ListTree },
    { id: "files", label: t.chatPanel.files, icon: FolderOpen },
    { id: "info", label: t.chatPanel.info, icon: Paperclip },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col bg-background/60 text-xs">
      <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
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

      <div className="flex gap-1 border-b border-border px-2 py-1">
        {tabs.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => setTab(item.id)}
              className={cn(
                "flex items-center gap-1 border border-border px-2 py-0.5",
                tab === item.id ? "bg-foreground/10 font-medium" : "text-muted-foreground hover:bg-foreground/5",
              )}
            >
              <Icon className="h-3 w-3" /> {item.label}
            </button>
          );
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "plan" && (
          <div className="p-2">
            {taskId ? (
              <TaskSpecPanel
                taskId={taskId}
                taskStatus={taskStatus}
                sessionId={sessionId}
                questions={questions}
                busy={busy}
                defaultOpen
                onAnswered={onAnswered}
                onError={onError}
              />
            ) : (
              <div className="space-y-2 p-2 text-muted-foreground">
                <p>{t.chatPanel.noPlan}</p>
                {mode === "chat" && (
                  <Button size="sm" onClick={onSwitchToPlan}>
                    {t.chatPanel.switchToPlan}
                  </Button>
                )}
              </div>
            )}
          </div>
        )}

        {tab === "files" && (
          <div className="space-y-3 p-3">
            <div>
              <div className="mb-1 font-medium">{t.chatPanel.attachments}</div>
              {attachments.length === 0 ? (
                <p className="text-muted-foreground">—</p>
              ) : (
                <ul className="space-y-0.5">
                  {attachments.map((item) => (
                    <li key={item.path} className="truncate" title={item.path}>{item.name}</li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <div className="mb-1 font-medium">{t.chatPanel.touchedFiles}</div>
              {paths.length === 0 ? (
                <p className="text-muted-foreground">—</p>
              ) : (
                <ul className="space-y-0.5">
                  {paths.map((path) => (
                    <li key={path} className="truncate" title={path}>{path}</li>
                  ))}
                </ul>
              )}
            </div>
            <div className="flex flex-wrap gap-1">
              <Button ghost size="sm" onClick={() => void api.openLogsFolder().catch(() => undefined)}>
                <FolderOpen className="mr-1 h-3 w-3" /> {t.chatPanel.openLogs}
              </Button>
              <Link to="/files" className="border border-border px-2 py-1 text-xs hover:bg-foreground/5">
                {t.chatPanel.openFiles}
              </Link>
            </div>
          </div>
        )}

        {tab === "info" && <SessionInspector usage={usage} title={title} onClose={onClose} embedded />}
      </div>
    </div>
  );
}
