import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Check, Loader2, MessageCircle, Plus, RotateCcw, Send, Square, X } from "lucide-react";
import { api } from "@/lib/api";
import type { ChatMsg, ChatSessionRow, ChatStatus, RunState } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@vendor/ui/ui/components/button";
import { Markdown } from "@/components/Markdown";

/* ------------------------------------------------------------------ */
/*  NativeChatPage - /chat. Message -> model -> reply, saved per       */
/*  session (backend: /api/chat/*). Replaces the xterm/PTY terminal    */
/*  the dashboard inherited, which has no backend in ARIA.             */
/* ------------------------------------------------------------------ */

function errMessage(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  // fetchJSON errors look like "503: {"detail":"..."}" - show just the detail
  const m = raw.match(/"detail"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  return m ? m[1].replace(/\\"/g, '"') : raw;
}

type Mode = "chat" | "agent" | "plan";

const MODES: { id: Mode; label: string; hint: string }[] = [
  { id: "chat", label: "Chat", hint: "Answers only, no tools" },
  { id: "agent", label: "Agent", hint: "Works step by step with tools; risky steps ask you first" },
  { id: "plan", label: "Plan", hint: "Plans, executes and audits the task; risky steps ask you first" },
];

const POLL_MS = 2000;

function runBusy(run: RunState | null): boolean {
  return run !== null && run.status !== null && !run.terminal;
}

export default function NativeChatPage() {
  const [status, setStatus] = useState<ChatStatus | null>(null);
  const [sessions, setSessions] = useState<ChatSessionRow[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<Mode>("chat");
  const [run, setRun] = useState<RunState | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await api.chatSessions());
    } catch {
      /* the list is a convenience; the chat itself reports real errors */
    }
  }, []);

  const loadMessages = useCallback(async (id: string) => {
    try {
      setMessages(await api.chatMessages(id));
    } catch (e) {
      setError(errMessage(e));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [st, list] = await Promise.all([api.chatStatus(), api.chatSessions()]);
        if (cancelled) return;
        setStatus(st);
        setSessions(list);
        if (list.length > 0) {
          setActiveId(list[0].id);
          setMessages(await api.chatMessages(list[0].id));
        }
      } catch (e) {
        if (!cancelled) setError(errMessage(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Agent/Plan runs execute in the background (TaskRunner); poll while one is active.
  const watching = activeId !== null && runBusy(run);
  useEffect(() => {
    if (!watching || !activeId) return;
    const sid = activeId;
    let stopped = false;
    const tick = async () => {
      try {
        const [state, msgs] = await Promise.all([api.runState(sid), api.chatMessages(sid)]);
        if (stopped) return;
        setRun(state);
        setMessages(msgs);
        if (state.terminal) void refreshSessions();
      } catch (e) {
        if (!stopped) setError(errMessage(e));
      }
    };
    const timer = setInterval(() => void tick(), POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [watching, activeId, refreshSessions]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages, sending]);

  const openSession = async (id: string) => {
    if (sending || id === activeId) return;
    setActiveId(id);
    setError(null);
    setMessages([]);
    setRun(null);
    await loadMessages(id);
    try {
      setRun(await api.runState(id));
    } catch {
      /* no run info: the session is shown as plain chat history */
    }
  };

  const newChat = () => {
    if (sending) return;
    setActiveId(null);
    setMessages([]);
    setError(null);
    setRun(null);
  };

  const sendRun = async (text: string, runMode: "agent" | "plan") => {
    let sid = activeId;
    if (!sid) {
      sid = (await api.chatCreate(text.slice(0, 60))).session_id;
      setActiveId(sid);
    }
    setMessages((prev) => [
      ...prev,
      { id: `local-${Date.now()}`, role: "user", content: text, created_at: null },
    ]);
    setInput("");
    const res = await api.runPost(sid, { content: text, mode: runMode });
    setMessages(await api.chatMessages(sid));
    setRun(await api.runState(sid));
    if (res.queued === false) setError("This task is already queued or running.");
  };

  const decide = async (itemId: string, approve: boolean) => {
    if (!activeId || decidingId) return;
    setDecidingId(itemId);
    try {
      await (approve ? api.attentionApprove(itemId) : api.attentionReject(itemId));
      setRun(await api.runState(activeId));
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setDecidingId(null);
    }
  };

  const cancelRun = async () => {
    if (!run?.task_id || !activeId) return;
    try {
      await api.runCancel(run.task_id);
      setRun(await api.runState(activeId));
    } catch (e) {
      setError(errMessage(e));
    }
  };

  const lastIsUser = messages.length > 0 && messages[messages.length - 1].role === "user";

  const send = async (retry = false) => {
    const text = input.trim();
    if (sending || (!retry && !text)) return;
    if (runBusy(run)) return;
    setSending(true);
    setError(null);
    try {
      if (mode !== "chat" && !retry) {
        await sendRun(text, mode);
        return;
      }
      // the status banner may be stale (e.g. key saved in another tab)
      let sid = activeId;
      if (!sid) {
        sid = (await api.chatCreate()).session_id;
        setActiveId(sid);
      }
      if (!retry) {
        setMessages((prev) => [
          ...prev,
          { id: `local-${Date.now()}`, role: "user", content: text, created_at: null },
        ]);
        setInput("");
      }
      await api.chatSend(sid, retry ? { retry: true } : { content: text });
      await loadMessages(sid);
      setStatus(await api.chatStatus());
    } catch (e) {
      setError(errMessage(e));
      // resync with what the server really stored (it keeps the user's message on model errors)
      if (activeId) await loadMessages(activeId);
      else {
        const list = await api.chatSessions().catch(() => []);
        if (list[0]) {
          setActiveId(list[0].id);
          await loadMessages(list[0].id);
        }
      }
    } finally {
      setSending(false);
      void refreshSessions();
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send(false);
    }
  };

  const notConfigured = status !== null && !status.configured;

  return (
    <div className="grid h-[calc(100vh-9rem)] min-h-[420px] gap-3 md:grid-cols-[240px_1fr]">
      {/* ---------------- sessions ---------------- */}
      <div className="flex min-h-0 flex-col gap-2 border border-border bg-background/40 p-2">
        <Button size="sm" onClick={newChat} disabled={sending}>
          <Plus className="mr-1 h-3 w-3" /> New chat
        </Button>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {sessions.length === 0 && !loading && (
            <p className="p-2 text-xs text-muted-foreground">No chats yet.</p>
          )}
          {sessions.map((s) => (
            <button
              type="button"
              key={s.id}
              onClick={() => void openSession(s.id)}
              className={cn(
                "block w-full truncate px-2 py-1.5 text-left text-xs hover:bg-foreground/5",
                s.id === activeId && "bg-foreground/10",
              )}
              title={s.title}
            >
              <MessageCircle className="mr-1 inline h-3 w-3 text-muted-foreground" />
              {s.title || "Untitled"}
              <span className="ml-1 text-muted-foreground">({s.message_count})</span>
            </button>
          ))}
        </div>
      </div>

      {/* ---------------- conversation ---------------- */}
      <div className="flex min-h-0 flex-col border border-border bg-background/40">
        {notConfigured && (
          <div className="flex items-start gap-2 border-b border-border bg-amber-500/10 p-3 text-xs">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
            <div>
              <div className="font-medium">No language model is configured</div>
              <div className="text-muted-foreground">
                Open <Link className="underline" to="/env">Keys</Link> and add <code>GEMINI_API_KEYS</code> (a free
                key from aistudio.google.com). It works immediately, no restart.
              </div>
            </div>
          </div>
        )}

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
          {loading && (
            <div className="text-xs text-muted-foreground">
              <Loader2 className="inline h-3 w-3 animate-spin" /> loading...
            </div>
          )}
          {!loading && messages.length === 0 && (
            <p className="text-xs text-muted-foreground">Write a message below to start.</p>
          )}
          {messages.map((m) => (
            <div key={m.id} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
              <div
                className={cn(
                  "max-w-[85%] border border-border px-3 py-2",
                  m.role === "user" ? "bg-foreground/10" : "bg-background/60",
                )}
              >
                {m.role === "assistant" ? (
                  <Markdown content={m.content} />
                ) : (
                  <div
                    className={cn(
                      "whitespace-pre-wrap text-sm leading-relaxed",
                      m.role !== "user" && "text-xs text-muted-foreground",
                    )}
                  >
                    {m.content}
                  </div>
                )}
              </div>
            </div>
          ))}
          {sending && (
            <div className="text-xs text-muted-foreground">
              <Loader2 className="inline h-3 w-3 animate-spin" /> thinking...
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {run && run.status && (
          <div className="space-y-2 border-t border-border bg-foreground/5 px-3 py-2 text-xs">
            <div className="flex items-center justify-between gap-2">
              <span>
                {runBusy(run) && <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />}
                Task: <span className="font-medium">{run.status.replace(/_/g, " ")}</span>
              </span>
              {runBusy(run) && (
                <Button ghost size="sm" onClick={() => void cancelRun()}>
                  <Square className="mr-1 h-3 w-3" /> Cancel
                </Button>
              )}
            </div>
            {run.attention.map((item) => (
              <div key={item.id} className="space-y-1 border border-amber-500/40 bg-amber-500/10 p-2">
                <div className="font-medium">{item.title}</div>
                {item.body_md && (
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap text-muted-foreground">
                    {item.body_md}
                  </pre>
                )}
                <div className="flex gap-2">
                  <Button size="sm" disabled={decidingId !== null} onClick={() => void decide(item.id, true)}>
                    <Check className="mr-1 h-3 w-3" /> Approve
                  </Button>
                  <Button ghost size="sm" disabled={decidingId !== null} onClick={() => void decide(item.id, false)}>
                    <X className="mr-1 h-3 w-3" /> Reject
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}

        {error && (
          <div className="flex items-center justify-between gap-2 border-t border-border bg-destructive/10 px-3 py-2 text-xs">
            <span className="text-destructive">{error}</span>
            {mode === "chat" && lastIsUser && !sending && (
              <Button ghost size="sm" onClick={() => void send(true)}>
                <RotateCcw className="mr-1 h-3 w-3" /> Retry
              </Button>
            )}
          </div>
        )}

        <div className="flex items-center gap-1 border-t border-border px-2 pt-2" role="radiogroup" aria-label="Mode">
          {MODES.map((md) => (
            <button
              type="button"
              key={md.id}
              role="radio"
              aria-checked={mode === md.id}
              title={md.hint}
              disabled={sending}
              onClick={() => setMode(md.id)}
              className={cn(
                "border border-border px-2 py-0.5 text-xs",
                mode === md.id ? "bg-foreground/10 font-medium" : "text-muted-foreground hover:bg-foreground/5",
              )}
            >
              {md.label}
            </button>
          ))}
          <span className="ml-2 truncate text-xs text-muted-foreground">
            {MODES.find((md) => md.id === mode)?.hint}
          </span>
        </div>
        <div className="flex gap-2 p-2">
          <textarea
            className="min-h-[56px] flex-1 resize-none border border-border bg-background/40 px-3 py-2 text-sm leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30"
            placeholder={
              mode === "chat"
                ? "Message ARIA...  (Enter to send, Shift+Enter for a new line)"
                : "Describe the task...  (Enter to start, Shift+Enter for a new line)"
            }
            value={input}
            disabled={sending || runBusy(run)}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <Button onClick={() => void send(false)} disabled={sending || runBusy(run) || !input.trim()} aria-label="Send">
            {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          </Button>
        </div>
      </div>
    </div>
  );
}
