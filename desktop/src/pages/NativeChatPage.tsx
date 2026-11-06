import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, Check, Loader2, RotateCcw, Square, X } from "lucide-react";
import { api } from "@/lib/api";
import { CHAT_MODES, normalizeChatMode, type ChatMode } from "@/lib/chat-modes";
import { formatLocalTime, processingSeconds } from "@/lib/message-timing";
import { useI18n } from "@/i18n";
import type { ChatModels, ChatMsg, ChatSessionRow, ChatStatus, ChatUsage, RunProfile, RunState } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@vendor/ui/ui/components/button";
import { Markdown } from "@/components/Markdown";
import type { ClarifyQuestion } from "@/components/TaskSpecPanel";
import { WorkPanel } from "@/components/WorkPanel";
import { ChatTopBar } from "@/components/ChatTopBar";
import { ChatComposer } from "@/components/ChatComposer";
import { SessionTabs } from "@/components/SessionTabs";
import { ToolCallGroup } from "@/components/ToolCallGroup";

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

const POLL_MS = 2000;
const LIST_POLL_MS = 3000;
const SEEN_KEY = "aria.chat.seen";
const FOCUS_KEY = "aria.chat.focus";
/** Same event App.tsx listens to: collapses / expands the left app menu. */
export const SIDEBAR_EVENT = "aria:set-sidebar-collapsed";

function loadFocus(): boolean {
  try {
    return localStorage.getItem(FOCUS_KEY) === "true";
  } catch {
    return false;
  }
}

type SeenMap = Record<string, string>;

function loadSeen(): SeenMap {
  try {
    return JSON.parse(localStorage.getItem(SEEN_KEY) || "{}") as SeenMap;
  } catch {
    return {};
  }
}

function saveSeen(m: SeenMap) {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify(m));
  } catch {
    /* storage can be blocked; the indicator just forgets */
  }
}

/** Row indicator: waiting for you (blinks amber) > unread reply (blinks green) > working (spinner). */
export function rowIndicator(s: ChatSessionRow, activeId: string | null, seen: SeenMap): "approve" | "unread" | "busy" | null {
  if ((s.pending_approvals ?? 0) > 0) return "approve";
  if (s.id !== activeId && s.last_reply_at && seen[s.id] !== s.last_reply_at) return "unread";
  if (s.busy) return "busy";
  return null;
}

function runBusy(run: RunState | null): boolean {
  return run !== null && run.status !== null && !run.terminal;
}

export default function NativeChatPage() {
  const { t, locale } = useI18n();
  const [searchParams] = useSearchParams();
  const [status, setStatus] = useState<ChatStatus | null>(null);
  const [sessions, setSessions] = useState<ChatSessionRow[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [sending, setSending] = useState(false);
  const [sendStartedAt, setSendStartedAt] = useState<number | null>(null);
  const [clock, setClock] = useState(() => Date.now());
  const [attachments, setAttachments] = useState<{ name: string; path: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [taskQuestions, setTaskQuestions] = useState<ClarifyQuestion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<ChatMode>("chat");
  const [run, setRun] = useState<RunState | null>(null);
  const [usage, setUsage] = useState<ChatUsage | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const usageInFlight = useRef(false);
  const usageTick = useRef(0);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const [profile, setProfile] = useState<RunProfile | null>(null);
  const [models, setModels] = useState<ChatModels | null>(null);
  const [profileNotes, setProfileNotes] = useState<string[]>([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [savingDefault, setSavingDefault] = useState(false);
  const [openIds, setOpenIds] = useState<string[]>([]);
  const prefsLoaded = useRef(false);
  const [seen, setSeen] = useState<SeenMap>(() => loadSeen());
  const [focus, setFocus] = useState<boolean>(() => loadFocus());
  const [costToday, setCostToday] = useState<number | null>(null);
  const [costTodayUnpriced, setCostTodayUnpriced] = useState(0);
  const profileDirty = useRef(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const [mdl, def] = await Promise.all([api.chatModels(), api.chatDefaultProfile()]);
        setModels(mdl);
        setProfile(def.profile);
      } catch {
        /* the team selector is optional; chat works without it */
      }
    })();
  }, []);

  useEffect(() => {
    if (!activeId || profileDirty.current) return;
    api.chatSessionProfile(activeId).then((r) => setProfile(r.profile)).catch(() => undefined);
  }, [activeId]);

  const applyFocus = useCallback((next: boolean) => {
    setFocus(next);
    try {
      localStorage.setItem(FOCUS_KEY, String(next));
    } catch {
      /* storage can be blocked; focus just is not remembered */
    }
    window.dispatchEvent(new CustomEvent(SIDEBAR_EVENT, { detail: next }));
  }, []);

  // Restore the app menu state that matches a remembered focus mode on first mount.
  useEffect(() => {
    if (loadFocus()) window.dispatchEvent(new CustomEvent(SIDEBAR_EVENT, { detail: true }));
  }, []);

  // Ctrl+\ toggles focus mode.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "\\") {
        e.preventDefault();
        applyFocus(!focus);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focus, applyFocus]);

  // Spend today (estimate) for the top bar; refreshed once a minute, failures are silent.
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      api
        .getAnalytics(1)
        .then((r) => {
          if (cancelled) return;
          setCostToday(r.totals.ledger_cost_usd);
          setCostTodayUnpriced(r.totals.unpriced_calls);
        })
        .catch(() => undefined);
    };
    load();
    const id = window.setInterval(load, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const changeProfile = (next: RunProfile) => {
    profileDirty.current = true;
    setProfile(next);
  };

  const changeInput = (value: string) => {
    setInput(value);
    const key = activeId ?? "draft";
    setDrafts((prev) => ({ ...prev, [key]: value }));
  };

  /** Save the selection to the session before a send, so the backend uses it (and keeps it after restart). */
  const syncProfile = async (sid: string) => {
    if (!profile || !profileDirty.current) return;
    const saved = await api.chatSaveProfile(sid, profile);
    setProfile(saved.profile);
    setProfileNotes(saved.notes);
    profileDirty.current = false;
  };

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

  // 19.6: context and cost of the open chat. Informational: failures never block the chat.
  const loadUsage = useCallback(async (sid: string | null) => {
    if (!sid || usageInFlight.current) return;
    usageInFlight.current = true;
    try {
      setUsage(await api.chatUsage(sid));
    } catch {
      /* usage is informational */
    } finally {
      usageInFlight.current = false;
    }
  }, []);

  useEffect(() => {
    void loadUsage(activeId);
  }, [activeId, loadUsage]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [st, list, prefs] = await Promise.all([
          api.chatStatus(),
          api.chatSessions(),
          api.getDashboardPrefs().catch(() => null),
        ]);
        if (cancelled) return;
        setStatus(st);
        setSessions(list);
        const stored = (prefs?.open_tabs ?? []).filter((id) => list.some((row) => row.id === id));
        setOpenIds(stored.length > 0 ? stored : list.slice(0, 8).map((row) => row.id));
        const resume = searchParams.get("resume");
        const wanted =
          resume && list.some((row) => row.id === resume)
            ? resume
            : prefs?.active_tab && list.some((row) => row.id === prefs.active_tab)
              ? prefs.active_tab
              : list[0]?.id ?? null;
        prefsLoaded.current = true;
        if (wanted) {
          setActiveId(wanted);
          setMessages(await api.chatMessages(wanted));
          // After a reload the run state is what brings back the clarification card (PRD-3)
          // and resumes polling of a task that is still running.
          const state = await api.runState(wanted).catch(() => null);
          if (!cancelled && state) setRun(state);
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

  useEffect(() => {
    if (!prefsLoaded.current) return;
    const timer = window.setTimeout(() => {
      void api.saveDashboardPrefs({ open_tabs: openIds, active_tab: activeId }).catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [openIds, activeId]);

  useEffect(() => {
    if (!prefsLoaded.current || sessions.length === 0) return;
    setOpenIds((prev) => {
      const next = prev.filter((id) => sessions.some((row) => row.id === id));
      return next.length === prev.length ? prev : next;
    });
  }, [sessions]);

  // PRD-3: the clarification card survives a reload and a session switch. While the task waits
  // for answers the server returns the open questions with the run state; once it stops
  // waiting the card goes away.
  useEffect(() => {
    if (!run) return;
    const qs = run.status === "awaiting_clarification" ? ((run.questions ?? []) as unknown as ClarifyQuestion[]) : [];
    setTaskQuestions((cur) => {
      const next = qs.length > 0 ? qs : null;
      return JSON.stringify(cur) === JSON.stringify(next) ? cur : next;
    });
  }, [run]);

  // Agent runs execute in the background (TaskRunner); poll while one is active.
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
        usageTick.current += 1;
        if (usageTick.current % 3 === 0) void loadUsage(sid);
      } catch (e) {
        if (!stopped) setError(errMessage(e));
      }
    };
    const timer = setInterval(() => void tick(), POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [watching, activeId, refreshSessions, loadUsage]);

  // keep the list fresh so other chats can start blinking while you work in this one
  useEffect(() => {
    const timer = setInterval(() => void refreshSessions(), LIST_POLL_MS);
    return () => clearInterval(timer);
  }, [refreshSessions]);

  // the open chat counts as read: remember its latest reply time
  useEffect(() => {
    if (!activeId) return;
    const row = sessions.find((x) => x.id === activeId);
    if (!row?.last_reply_at || seen[activeId] === row.last_reply_at) return;
    const next = { ...seen, [activeId]: row.last_reply_at };
    setSeen(next);
    saveSeen(next);
  }, [activeId, sessions, seen]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages, sending, run, taskQuestions, streamingText]);

  useEffect(() => {
    if (!sending) return;
    const timer = window.setInterval(() => setClock(Date.now()), 200);
    return () => window.clearInterval(timer);
  }, [sending]);

  const elapsedSeconds = sendStartedAt === null ? null : Math.max(0, (clock - sendStartedAt) / 1000);

  const openSession = async (id: string) => {
    if (sending || id === activeId) return;
    setActiveId(id);
    setOpenIds((prev) => (prev.includes(id) ? prev : [...prev, id]));
    setInput(drafts[id] ?? "");
    setError(null);
    profileDirty.current = false;
    api.chatSessionProfile(id).then((result) => setProfile(result.profile)).catch(() => undefined);
    setMessages([]);
    setRun(null);
    setTaskQuestions(null);
    await loadMessages(id);
    try {
      setRun(await api.runState(id));
    } catch {
      /* no run info: the session is shown as plain chat history */
    }
  };

  const closeTab = (id: string) => {
    const index = openIds.indexOf(id);
    const next = openIds.filter((item) => item !== id);
    setOpenIds(next);
    if (id !== activeId) return;
    const neighbor = next[Math.min(index, next.length - 1)] ?? null;
    if (neighbor) void openSession(neighbor);
    else newChat();
  };

  const renameTab = async (id: string, title: string) => {
    try {
      await api.renameSession(id, title);
      setSessions((prev) => prev.map((row) => (row.id === id ? { ...row, title } : row)));
    } catch (e) {
      setError(errMessage(e));
    }
  };

  const saveDefaultProfile = async () => {
    if (!profile) return;
    setSavingDefault(true);
    try {
      const saved = await api.chatSaveDefaultProfile(profile);
      setProfile(saved.profile);
      setProfileNotes(saved.notes);
      profileDirty.current = false;
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setSavingDefault(false);
    }
  };

  const resumeId = searchParams.get("resume");
  useEffect(() => {
    if (resumeId && sessions.some((row) => row.id === resumeId)) void openSession(resumeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeId, sessions.length]);

  const newChat = () => {
    if (sending) return;
    setActiveId(null);
    setInput(drafts["draft"] ?? "");
    profileDirty.current = false;
    api.chatDefaultProfile().then((result) => setProfile(result.profile)).catch(() => undefined);
    setMessages([]);
    setError(null);
    setRun(null);
    // 19.6 D-25: карточка уточнений и хвост стрима прошлой задачи не должны переживать New chat.
    setTaskQuestions(null);
    setStreamingText(null);
    setUsage(null);
    setMode("chat");
  };

  const sendRun = async (text: string, runMode: "agent") => {
    let sid = activeId;
    if (!sid) {
      sid = (await api.chatCreate(text.slice(0, 60))).session_id;
      setActiveId(sid);
      setOpenIds((prev) => (prev.includes(sid as string) ? prev : [...prev, sid as string]));
    }
    await syncProfile(sid);
    setMessages((prev) => [
      ...prev,
      { id: `local-${Date.now()}`, role: "user", content: text, created_at: new Date().toISOString() },
    ]);
    setInput("");
    setDrafts({});
    const res = await api.runPost(sid, { content: text, mode: runMode });
    // Гейт уточнений (Agent): бэкенд отвечает queued:false + questions — показываем карточку,
    // а не ошибку «already queued».
    const qs = (res as unknown as { questions?: ClarifyQuestion[] })?.questions ?? null;
    const hasQuestions = !!qs && qs.length > 0;
    setTaskQuestions(hasQuestions ? qs : null);
    setMessages(await api.chatMessages(sid));
    setRun(await api.runState(sid));
    void loadUsage(sid);
    // E3: дорогая задача ждёт Approve (attention_required) — это не ошибка «already queued».
    if (res.queued === false && !hasQuestions && !res.attention_required) {
      setError("This task is already queued or running.");
    }
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

  // UX-1: the nearest user message before each answer gives the processing time.
  const previousUser = new Map<string, ChatMsg>();
  let lastUserMessage: ChatMsg | null = null;
  for (const message of messages) {
    if (message.role === "user") lastUserMessage = message;
    else if (lastUserMessage) previousUser.set(message.id, lastUserMessage);
  }

  const addFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const added: { name: string; path: string }[] = [];
      for (const file of Array.from(files)) {
        const safe = file.name.replace(/[^\w.\-]+/g, "_").slice(0, 80) || "file";
        const target = `chat-uploads/${Date.now()}-${added.length}-${safe}`;
        const res = await api.uploadFile(target, file, true);
        added.push({ name: file.name, path: res.path });
      }
      setAttachments((prev) => [...prev, ...added].slice(0, 10));
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setUploading(false);

    }
  };

  const toolHint = (name: string) => {
    const ext = name.toLowerCase().split(".").pop() ?? "";
    if (["png", "jpg", "jpeg", "webp", "gif", "bmp"].includes(ext)) return "vision_analyze";
    if (["mp3", "wav", "m4a", "ogg", "webm", "flac"].includes(ext)) return "audio_transcribe";
    return "file_extract";
  };
  const attachmentBlock = attachments.length
    ? `\n\nAttached files:\n${attachments
        .map((a) => `- ${a.path} (${a.name}) — use ${toolHint(a.name)}`)
        .join("\n")}`
    : "";
  const outgoing = (text: string) => (text + attachmentBlock).trim();

  const copyChatText = useCallback(async () => {
    const parts = messages
      .filter((message) => message.role === "user" || message.role === "assistant")
      .map((message) => `${message.role === "user" ? "You" : "ARIA"}:\n${message.content}`);
    if (parts.length === 0) return;
    try {
      await navigator.clipboard.writeText(parts.join("\n\n"));
    } catch {
      /* clipboard can be blocked; the button simply does nothing */
    }
  }, [messages]);

  const send = async (retry = false) => {
    const text = input.trim();
    if (sending || (!retry && !text && attachments.length === 0)) return;
    if (runBusy(run)) return;
    setSending(true);
    setSendStartedAt(Date.now());
    setClock(Date.now());
    setError(null);
    const outgoingText = outgoing(text);
    try {
      if (mode !== "chat" && !retry) {
        await sendRun(outgoingText, "agent");
        setInput("");
    setDrafts({});
        setAttachments([]);
        return;
      }
      // the status banner may be stale (e.g. key saved in another tab)
      let sid = activeId;
      if (!sid) {
        sid = (await api.chatCreate()).session_id;
        setActiveId(sid);
        setOpenIds((prev) => (prev.includes(sid as string) ? prev : [...prev, sid as string]));
      }
      await syncProfile(sid);
      if (!retry) {
        setMessages((prev) => [
          ...prev,
          { id: `local-${Date.now()}`, role: "user", content: outgoingText, created_at: new Date().toISOString() },
        ]);
        setInput("");
    setDrafts({});
      }
      const sent = await api.chatSendStream(
        sid,
        retry
          ? { retry: true }
          : { content: outgoingText, attachments: attachments.map((a) => a.path) },
        (delta) => setStreamingText((prev) => (prev ?? "") + delta),
      );
      setStreamingText(null);
      if (!retry) setAttachments([]);
      // 19.6: Stop вернул {"type":"stopped"} — ассистент не сохранён, это не ошибка.
      if (sent.stopped) {
        await loadMessages(sid);
        void loadUsage(sid);
        return;
      }
      // 19.4b B2: запросу нужен инструмент — бэкенд перевёл сессию в Agent.
      // Переключаем вкладку и включаем опрос запуска: шаги и ответ придут существующим polling.
      const returnedMode = (sent as { mode?: unknown }).mode;
      if (returnedMode && normalizeChatMode(returnedMode) === "plan") {
        setMode("plan");
        setRun(await api.runState(sid).catch(() => null));
      }
      // П.3.1 ТЗ: бэкенд вернул пакет уточнений -> показываем карточку, а не просто текст.
      const qs = (sent as unknown as { questions?: ClarifyQuestion[] })?.questions ?? null;
      setTaskQuestions(qs && qs.length > 0 ? qs : null);
      await loadMessages(sid);
      void loadUsage(sid);
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
      setStreamingText(null);
      setSending(false);
      setSendStartedAt(null);
      void refreshSessions();
    }
  };

  const notConfigured = status !== null && !status.configured;

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
      <SessionTabs
        sessions={sessions}
        openIds={openIds}
        activeId={activeId}
        draftActive={activeId === null}
        indicator={(id) => {
          const row = sessions.find((item) => item.id === id);
          return row ? rowIndicator(row, activeId, seen) : null;
        }}
        onOpen={(id) => void openSession(id)}
        onClose={closeTab}
        onNew={newChat}
        onRename={(id, title) => void renameTab(id, title)}
      />

      {/* ---------------- conversation ---------------- */}
      <div className="flex min-h-0 min-w-0 flex-col border border-border bg-background/40">
        <ChatTopBar
          title={sessions.find((x) => x.id === activeId)?.title ?? ""}
          mode={CHAT_MODES.find((md) => md.id === mode)?.label ?? mode}
          busy={runBusy(run) || sending}
          waiting={(run?.attention.length ?? 0) > 0}
          costToday={costToday}
          costTodayUnpriced={costTodayUnpriced}
          usage={usage}
          focus={focus}
          inspectorOpen={inspectorOpen}
          onCopyChat={() => void copyChatText()}
          onToggleInspector={() => setInspectorOpen((open) => !open)}
          onToggleFocus={() => applyFocus(!focus)}
        />
        <div className="flex min-h-0 flex-1">
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
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
          {messages.map((m, index) => {
            if (m.role === "tool") {
              if (messages[index - 1]?.role === "tool") return null;
              const group: ChatMsg[] = [];
              for (let j = index; j < messages.length && messages[j].role === "tool"; j += 1) {
                group.push(messages[j]);
              }
              return <ToolCallGroup key={m.id} messages={group} />;
            }
            const sentLabel = formatLocalTime(m.created_at, locale);
            const seconds =
              m.role === "assistant"
                ? processingSeconds(previousUser.get(m.id)?.created_at, m.created_at)
                : null;
            const meta = [
              sentLabel ? t.chatMeta.sentAt.replace("{time}", sentLabel) : null,
              seconds != null ? t.chatMeta.tookSeconds.replace("{seconds}", seconds.toFixed(1)) : null,
            ]
              .filter((part): part is string => Boolean(part))
              .join(" · ");
            return (
              <div
                key={m.id}
                className={cn("flex flex-col gap-0.5", m.role === "user" ? "items-end" : "items-start")}
              >
                <div
                  title={meta || undefined}
                  className={cn(
                    focus ? "max-w-[92%] border border-border px-3 py-2" : "max-w-[85%] border border-border px-3 py-2",
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
                {meta && (
                  <div className="px-1 text-[10px] text-muted-foreground">{meta}</div>
                )}
                {m.content_json?.notice === "telegram_not_connected" && (
                  <Link
                    to={m.content_json.action_path || "/channels"}
                    className="border border-border px-2 py-1 text-xs underline hover:bg-foreground/5"
                  >
                    {t.app.openChannels}
                  </Link>
                )}
              </div>
            );
          })}
          {sending && streamingText !== null && streamingText !== "" && (
            <div className="flex flex-col items-start gap-0.5">
              <div
                className={cn(
                  focus ? "max-w-[92%]" : "max-w-[85%]",
                  "border border-border bg-background/60 px-3 py-2",
                )}
              >
                <Markdown content={streamingText} />
              </div>
              {elapsedSeconds != null && (
                <div className="px-1 text-[10px] text-muted-foreground">
                  {elapsedSeconds.toFixed(1)} s
                </div>
              )}
            </div>
          )}
          {sending && (streamingText === null || streamingText === "") && (
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="inline h-3 w-3 animate-spin" /> thinking...
              {elapsedSeconds != null && <span>{elapsedSeconds.toFixed(1)} s</span>}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {run && run.status && (
          <div className="space-y-2 border-t border-border bg-foreground/5 px-3 py-2 text-xs">
            <div className="flex items-center justify-between gap-2">
              <span>
                {runBusy(run) && <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />}
                {run.attention.length > 0 ? <span className="font-medium text-amber-500">Waiting for your decision</span> : <>Task: <span className="font-medium">{run.status.replace(/_/g, " ")}</span></>}
              </span>
              {runBusy(run) && (
                <Button ghost size="sm" onClick={() => void cancelRun()}>
                  <Square className="mr-1 h-3 w-3" /> Cancel
                </Button>
              )}
            </div>
            {run.status === "failed" && (
              <div className="space-y-1 border border-destructive/40 bg-destructive/10 p-2">
                <div className="font-medium text-destructive">
                  Task failed{run.error_code ? `: ${run.error_code}` : ""}
                </div>
                <pre className="max-h-40 select-text overflow-auto whitespace-pre-wrap text-muted-foreground">
                  {run.error_message || "No reason was recorded. Open Logs and look for this task id: " + (run.task_id ?? "?")}
                </pre>
                <div className="flex gap-2">
                  <Button
                    ghost
                    size="sm"
                    onClick={() =>
                      void navigator.clipboard
                        ?.writeText(`${run.error_code ?? ""} ${run.error_message ?? ""} task=${run.task_id ?? ""}`.trim())
                        .catch(() => undefined)
                    }
                  >
                    Copy
                  </Button>
                  <Link className="self-center text-xs underline" to="/logs">Open Logs</Link>
                </div>
              </div>
            )}
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

        <ChatComposer
          input={input}
          onInputChange={changeInput}
          attachments={attachments}
          uploading={uploading}
          onAddFiles={(files) => void addFiles(files)}
          onRemoveAttachment={(index) => setAttachments((prev) => prev.filter((_, j) => j !== index))}
          sending={sending}
          busy={runBusy(run)}
          mode={mode}
          onModeChange={setMode}
          profile={profile}
          profileNotes={profileNotes}
          models={models}
          onChangeProfile={changeProfile}
          onSaveDefault={() => void saveDefaultProfile()}
          savingDefault={savingDefault}
          onSend={() => void send(false)}
          onStop={() => {
            if (activeId) void api.chatStop(activeId).catch(() => undefined);
          }}
          advancedOpen={advancedOpen}
          onToggleAdvanced={() => setAdvancedOpen((open) => !open)}
          error={null}
        />
          </div>
          {inspectorOpen && (
            <aside className="flex w-[360px] shrink-0 flex-col overflow-hidden border-l border-border bg-background/40">
              <WorkPanel
                title={sessions.find((x) => x.id === activeId)?.title ?? ""}
                taskId={run?.task_id ?? null}
                taskStatus={run?.status ?? null}
                sessionId={activeId}
                questions={taskQuestions}
                messages={messages}
                attachments={attachments}
                usage={usage}
                busy={mode !== "chat" && (runBusy(run) || sending)}
                mode={mode}
                onClose={() => setInspectorOpen(false)}
                onAnswered={async () => {
                  setTaskQuestions(null);
                  if (activeId) {
                    await loadMessages(activeId);
                    setRun(await api.runState(activeId).catch(() => null));
                  }
                }}
                onError={(m) => setError(m)}
                onSwitchToPlan={() => setMode("plan")}
              />
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}
