import { useEffect, useState } from "react";
import { AlertTriangle, Check, ChevronDown, ChevronUp, Loader2, ListTree, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@vendor/ui/ui/components/button";
import type { PlanStepView, TaskPlanView } from "@/lib/api";
import { useI18n } from "@/i18n/context";

/* П.3 ТЗ: карточка уточнений, предпросмотр плана, волны/подзадачи, отчёт аудита. */

export type ClarifyQuestion = {
  id?: string;
  text: string;
  field?: string;
  options?: string[];
  default_value?: string;
  blocking?: boolean;
};

type Labels = {
  clarifyTitle?: string; clarifyHint?: string; clarifyAnswer?: string;
  clarifySend?: string; clarifyAccept?: string;
  planTitle?: string; planRequest?: string; planApprove?: string; planReject?: string;
  planNoDeps?: string; planNoneDeps?: string; planWaves?: string;
  waves?: string; subtasks?: string; noSubtasks?: string;
  auditTitle?: string; auditVerdict?: string; auditCriteria?: string;
};

type ChildTask = { id: string; objective?: string; status?: string; role?: string };
type AuditRow = { id: string; verdict: string; attempt_no: number; metrics_compared?: Record<string, unknown>; missing_requirements?: string[] };

function errMessage(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  const m = raw.match(/"detail"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  return m ? m[1].replace(/\\"/g, '"') : raw;
}

function depsText(step: PlanStepView, L: Labels): string {
  if (step.depends_on === null || step.depends_on === undefined) return L.planNoDeps ?? "not set (runs in plan order)";
  if (step.depends_on.length === 0) return L.planNoneDeps ?? "none (independent)";
  return step.depends_on.map((d) => String(d).slice(0, 8)).join(", ");
}

export function TaskSpecPanel({
  taskId, taskStatus, questions, labels, onAnswered, onError,
}: {
  taskId: string | null;
  taskStatus?: string | null;
  sessionId: string | null;
  questions: ClarifyQuestion[] | null;
  labels?: Labels;
  onAnswered?: () => void;
  onError?: (m: string) => void;
}) {
  const { t } = useI18n();
  const L: Labels = labels ?? ((t as unknown as { taskSpec?: Labels }).taskSpec ?? {});
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<TaskPlanView | null>(null);
  const [children, setChildren] = useState<ChildTask[]>([]);
  const [audits, setAudits] = useState<AuditRow[]>([]);
  const [open, setOpen] = useState(true);

  const load = async () => {
    if (!taskId) return;
    const [pl, kids, au] = await Promise.all([
      api.taskPlan(taskId).catch(() => null),
      api.taskChildren(taskId).catch(() => [] as ChildTask[]),
      api.taskAuditReports(taskId).catch(() => [] as AuditRow[]),
    ]);
    setPlan(pl);
    setChildren(kids as ChildTask[]);
    setAudits(au as AuditRow[]);
  };

  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [taskId]);

  const submit = async (accept: boolean) => {
    if (!taskId) return;
    setBusy(true);
    try {
      const payload = accept
        ? { answers: [], accept_assumptions: true }
        : {
            // Чек-лист: неотвеченный вопрос уходит с рекомендованным вариантом (default_value), владелец только правит.
            answers: (questions ?? [])
              .map((q, i) => {
                const k = q.id || `q${i}`;
                return { id: k, field: q.field ?? k, value: (answers[k] ?? q.default_value ?? "").trim() };
              })
              .filter((a) => a.value),
          };
      await api.taskClarify(taskId, payload);
      setAnswers({});
      onAnswered?.();
      await load();
    } catch (e) {
      onError?.(errMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const requestApproval = async () => {
    if (!taskId) return;
    setBusy(true);
    try { await api.taskRequestPlanApproval(taskId); onAnswered?.(); await load(); }
    catch (e) { onError?.(errMessage(e)); }
    finally { setBusy(false); }
  };

  const hasQuestions = !!questions && questions.length > 0;
  if (!hasQuestions && !plan && children.length === 0 && audits.length === 0) return null;

  const lastAudit = audits.length > 0 ? audits[audits.length - 1] : null;

  return (
    <div className="border-t border-border bg-background/60 px-3 py-2 text-xs">
      <button type="button" className="flex w-full items-center gap-1 text-left font-medium" onClick={() => setOpen(!open)}>
        <ListTree className="h-3 w-3" />
        {L.planTitle ?? "Task contract and plan"}
        <span className="ml-auto text-muted-foreground">
          {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </span>
      </button>

      {open && (
        <div className="mt-2 max-h-[40vh] space-y-2 overflow-y-auto pr-1">
          {hasQuestions && (
            <div className="space-y-1 border border-amber-500/40 bg-amber-500/10 p-2">
              <div className="flex items-center gap-1 font-medium">
                <AlertTriangle className="h-3 w-3 text-amber-500" />
                {L.clarifyTitle ?? "A few answers are needed"}
              </div>
              {L.clarifyHint && <div className="text-muted-foreground">{L.clarifyHint}</div>}
              {(questions as ClarifyQuestion[]).map((q, i) => {
                // Раунд 2 (п.1.1.4): ключ ответа — только q.id (стабильный с бэкенда);
                // запасной q{i} — на случай старых данных без id.
                const key = q.id || `q${i}`;
                return (
                  <div key={key} className="space-y-1">
                    <div>{q.text}</div>
                    {q.options && q.options.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {q.options.map((opt) => (
                          <button
                            type="button" key={opt}
                            onClick={() => setAnswers((p) => ({ ...p, [key]: opt }))}
                            className={cn(
                              "border border-border px-2 py-0.5",
                              answers[key] === opt ? "bg-foreground/10 font-medium" : "text-muted-foreground hover:bg-foreground/5",
                            )}
                          >
                            {opt}
                          </button>
                        ))}
                      </div>
                    )}
                    <input
                      className="w-full border border-border bg-background/40 px-2 py-1"
                      placeholder={L.clarifyAnswer ?? "Your answer"}
                      value={answers[key] ?? q.default_value ?? ""}
                      onChange={(e) => setAnswers((p) => ({ ...p, [key]: e.target.value }))}
                    />
                  </div>
                );
              })}
              <div className="sticky bottom-0 -mx-2 -mb-2 flex gap-2 border-t border-amber-500/40 bg-background px-2 py-2">
                <Button size="sm" disabled={busy} onClick={() => void submit(false)}>
                  {busy ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Check className="mr-1 h-3 w-3" />}
                  {L.clarifySend ?? "Send answers"}
                </Button>
                <Button ghost size="sm" disabled={busy} onClick={() => void submit(true)}>
                  <ShieldCheck className="mr-1 h-3 w-3" /> {L.clarifyAccept ?? "Accept assumptions"}
                </Button>
              </div>
            </div>
          )}

          {plan && plan.steps.length > 0 && (
            <div className="space-y-1 border border-border p-2">
              <div className="flex flex-wrap items-center gap-2 font-medium">
                <span>{L.planTitle ?? "Plan preview"}</span>
                <span className="text-muted-foreground">
                  {L.planWaves ?? "waves"}: {plan.waves.length} · {String(plan.phase ?? "")}
                </span>
                {(!taskStatus || ["draft", "awaiting_clarification"].includes(taskStatus)) && (
                  <Button ghost size="sm" className="ml-auto" disabled={busy} onClick={() => void requestApproval()}>
                    {L.planRequest ?? "Request approval"}
                  </Button>
                )}
              </div>
              {plan.waves.map((wave, wi) => (
                <div key={`w${wi}`} className="text-muted-foreground">
                  {L.waves ?? "wave"} {wi + 1}: {wave.map((idx) => `#${idx}`).join(" ")}
                </div>
              ))}
              <ul className="space-y-0.5">
                {plan.steps.map((s, i) => (
                  <li key={String(s.step_id) + i} className="flex flex-wrap gap-2">
                    <span className="font-mono text-muted-foreground">{String(s.step_id).slice(0, 8)}</span>
                    <span>{s.objective}</span>
                    <span className="text-muted-foreground">[{s.role} · {s.tool_ref}]</span>
                    <span className="text-muted-foreground">deps: {depsText(s, L)}</span>
                    {s.covers?.length > 0 && <span className="text-muted-foreground">covers: {s.covers.join(",")}</span>}
                    {s.status && <span className="text-muted-foreground">{s.status}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="border border-border p-2">
            <div className="font-medium">{L.subtasks ?? "Subtasks"}</div>
            {children.length === 0
              ? <div className="text-muted-foreground">{L.noSubtasks ?? "none yet"}</div>
              : <ul className="space-y-0.5">
                  {children.map((c) => (
                    <li key={c.id}>
                      <span className="font-mono text-muted-foreground">{c.id.slice(0, 8)}</span>{" "}
                      {c.objective} <span className="text-muted-foreground">[{c.role} · {c.status}]</span>
                    </li>
                  ))}
                </ul>}
          </div>

          {lastAudit && (
            <div className="border border-border p-2">
              <div className="flex items-center gap-1 font-medium">
                <ShieldCheck className="h-3 w-3" />
                {L.auditTitle ?? "Audit report"}
              </div>
              <div className="text-muted-foreground">
                {L.auditVerdict ?? "verdict"}: <span className="font-medium">{lastAudit.verdict}</span> · attempt {lastAudit.attempt_no}
              </div>
              {lastAudit.metrics_compared && typeof lastAudit.metrics_compared === "object" && "criteria" in lastAudit.metrics_compared && (
                <ul className="space-y-0.5">
                  {Object.entries((lastAudit.metrics_compared as { criteria: Record<string, { status: string; note?: string }> }).criteria || {}).map(([cid, data]) => (
                    <li key={cid}>
                      {cid}: <span className={data.status === "pass" ? "text-emerald-500" : "text-destructive"}>{data.status}</span>{" "}
                      {data.note && <span className="text-muted-foreground">{data.note}</span>}
                    </li>
                  ))}
                </ul>
              )}
              {lastAudit.missing_requirements && lastAudit.missing_requirements.length > 0 && (
                <ul className="list-disc pl-4 text-muted-foreground">
                  {lastAudit.missing_requirements.map((m, i) => <li key={i}>{m}</li>)}
                </ul>
              )}
            </div>
          )}

          <div className="flex gap-2">
            <Button ghost size="sm" disabled={busy} onClick={() => void load()}>
              {busy ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
              {L.planReject ? "" : ""}Refresh
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export default TaskSpecPanel;
