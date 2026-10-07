import { useCallback, useEffect, useState } from "react";
import { FolderPlus, Gavel, Loader2, Plug, RefreshCw, ScanSearch } from "lucide-react";
import { api } from "@/lib/api";
import type {
  VaultBranch,
  VaultCandidate,
  VaultDecision,
  VaultStatus,
  VaultTag,
  VaultTagSearch,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@vendor/ui/ui/components/button";
import { Input } from "@vendor/ui/ui/components/input";

/* Obsidian layer UI (backend: routers/vault.py). Three panels used by NotesPage:
   TagsPanel / DecisionsPanel (left column tabs) and SetupPanel (right column). */

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const splitTags = (s: string) =>
  s.split(/[\s,]+/).map((t) => t.replace(/^#/, "").trim()).filter(Boolean);

interface OpenProps {
  onOpen: (path: string) => void;
  onError: (msg: string) => void;
}

/* ------------------------------ tags ------------------------------ */

export function TagsPanel({ onOpen, onError, refreshKey }: OpenProps & { refreshKey: number }) {
  const [tags, setTags] = useState<VaultTag[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [mode, setMode] = useState<"any" | "all">("any");
  const [res, setRes] = useState<VaultTagSearch | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api
      .getVaultTags()
      .then((r) => setTags(r.tags))
      .catch((e) => onError(errMsg(e)));
  }, [refreshKey, onError]);

  useEffect(() => {
    if (picked.length === 0) {
      setRes(null);
      return;
    }
    setLoading(true);
    api
      .searchVaultByTags(picked, mode)
      .then(setRes)
      .catch((e) => onError(errMsg(e)))
      .finally(() => setLoading(false));
  }, [picked, mode, refreshKey, onError]);

  const toggle = (t: string) =>
    setPicked((p) => (p.includes(t) ? p.filter((x) => x !== t) : [...p, t]));

  if (tags === null) return <Loader2 className="m-2 h-3 w-3 animate-spin" />;
  if (tags.length === 0) {
    return (
      <p className="p-2 text-xs text-muted-foreground">
        No tags yet. Add <code>tags: [a, b]</code> to a note&apos;s frontmatter or write #tag in the text.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-1 px-1">
        {tags.map((t) => (
          <button
            key={t.tag}
            type="button"
            onClick={() => toggle(t.tag)}
            title={`${t.count} direct, ${t.total} incl. nested`}
            className={cn(
              "rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-foreground/5",
              picked.includes(t.tag) && "bg-foreground/15 font-medium",
            )}
          >
            #{t.tag} <span className="text-muted-foreground">{t.total}</span>
          </button>
        ))}
      </div>
      {picked.length > 0 && (
        <div className="flex items-center justify-between px-2 text-xs text-muted-foreground">
          <span>
            {loading ? "…" : res?.total ?? 0} note(s) ·{" "}
            <button type="button" className="underline" onClick={() => setMode(mode === "any" ? "all" : "any")}>
              match {mode === "any" ? "ANY" : "ALL"}
            </button>
          </span>
          <button type="button" className="underline" onClick={() => setPicked([])}>
            clear
          </button>
        </div>
      )}
      {res?.notes.map((n) => (
        <button
          key={n.path}
          type="button"
          onClick={() => onOpen(n.path)}
          className="block w-full px-2 py-1 text-left text-xs hover:bg-foreground/5"
        >
          <div className="truncate font-medium">{n.path}</div>
          <div className="truncate text-muted-foreground">{n.summary || n.tags.map((t) => `#${t}`).join(" ")}</div>
        </button>
      ))}
    </div>
  );
}

/* --------------------------- decisions ---------------------------- */

export function DecisionsPanel({ onOpen, onError, refreshKey }: OpenProps & { refreshKey: number }) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<VaultDecision[] | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    (query: string) => {
      setLoading(true);
      api
        .getVaultDecisions(query)
        .then((r) => setRows(r.decisions))
        .catch((e) => onError(errMsg(e)))
        .finally(() => setLoading(false));
    },
    [onError],
  );

  useEffect(() => load(""), [load, refreshKey]);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-1">
        <Input
          value={q}
          placeholder="Filter decisions..."
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && load(q.trim())}
        />
        <Button ghost size="sm" onClick={() => load(q.trim())} aria-label="Search decisions">
          {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <ScanSearch className="h-3 w-3" />}
        </Button>
      </div>
      {rows !== null && rows.length === 0 && (
        <p className="p-2 text-xs text-muted-foreground">
          Nothing found. Decisions are notes with <code>type: decision</code>, lines like <code>Решение: …</code>,
          <code> #decision</code>, <code>&gt; [!decision]</code> or a &quot;Decisions&quot; section.
        </p>
      )}
      {rows?.map((d, i) => (
        <button
          key={`${d.path}:${d.line}:${i}`}
          type="button"
          onClick={() => onOpen(d.path)}
          className="block w-full px-2 py-1 text-left text-xs hover:bg-foreground/5"
        >
          <div className="truncate text-muted-foreground">
            {d.date} · {d.note}
            {d.status ? ` · ${d.status}` : ""}
          </div>
          <div className="line-clamp-2">{d.text}</div>
        </button>
      ))}
    </div>
  );
}

/* ----------------------------- setup ------------------------------ */

export function SetupPanel({
  onChanged,
  onOpen,
  onError,
  onInfo,
}: OpenProps & { onChanged: () => void; onInfo: (msg: string) => void }) {
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [found, setFound] = useState<VaultCandidate[] | null>(null);
  const [scanning, setScanning] = useState(false);
  const [path, setPath] = useState("");
  const [initObs, setInitObs] = useState(true);
  const [busy, setBusy] = useState(false);
  const [branches, setBranches] = useState<VaultBranch[]>([]);
  const [bName, setBName] = useState("");
  const [bDesc, setBDesc] = useState("");
  const [dTitle, setDTitle] = useState("");
  const [dText, setDText] = useState("");
  const [dCtx, setDCtx] = useState("");
  const [dBranch, setDBranch] = useState("");
  const [dTags, setDTags] = useState("");

  const refresh = useCallback(() => {
    api.getVaultStatus().then(setStatus).catch((e) => onError(errMsg(e)));
    api.getVaultBranches().then((r) => setBranches(r.branches)).catch(() => setBranches([]));
  }, [onError]);

  useEffect(refresh, [refresh]);

  const detect = async () => {
    setScanning(true);
    try {
      setFound((await api.discoverVaults()).vaults);
    } catch (e) {
      onError(errMsg(e));
    } finally {
      setScanning(false);
    }
  };

  const connect = async (p: string, create = false) => {
    if (!p.trim()) return;
    setBusy(true);
    try {
      const r = await api.connectVault(p.trim(), { create, init_obsidian: initObs });
      onInfo(`Connected: ${r.path} (${r.note_count} notes)`);
      setPath("");
      setFound(null);
      refresh();
      onChanged();
    } catch (e) {
      onError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const createBranch = async () => {
    if (!bName.trim()) return;
    setBusy(true);
    try {
      const r = await api.createVaultBranch(bName.trim(), bDesc.trim());
      onInfo(r.created ? `Branch created: ${r.path}` : `Branch exists: ${r.path}`);
      setBName("");
      setBDesc("");
      refresh();
      onChanged();
      onOpen(r.index);
    } catch (e) {
      onError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const logDecision = async () => {
    if (!dTitle.trim() || !dText.trim()) return;
    setBusy(true);
    try {
      const r = await api.logVaultDecision({
        title: dTitle.trim(),
        decision: dText.trim(),
        context: dCtx.trim(),
        branch: dBranch,
        tags: splitTags(dTags),
      });
      onInfo(`Decision saved: ${r.path}`);
      setDTitle("");
      setDText("");
      setDCtx("");
      refresh();
      onChanged();
      onOpen(r.path);
    } catch (e) {
      onError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const section = "flex flex-col gap-2 border border-border p-2";
  const label = "text-xs font-medium";
  const area =
    "w-full resize-y border border-border bg-background/40 px-2 py-1 text-xs placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30";

  return (
    <div className="flex flex-col gap-3 p-1">
      <div className={section}>
        <div className="flex items-center justify-between">
          <span className={label}>Vault</span>
          <Button ghost size="sm" onClick={refresh} aria-label="Refresh status">
            <RefreshCw className="h-3 w-3" />
          </Button>
        </div>
        {status ? (
          <div className="text-xs text-muted-foreground">
            <div className="break-all text-foreground">{status.path}</div>
            <div>
              {status.is_obsidian_vault ? "Obsidian vault" : "plain folder (no .obsidian)"} · {status.note_count} notes ·{" "}
              {status.folder_count} folders · {status.tag_count} tags · {status.decision_count} decisions
            </div>
            {(status.config.daily_notes_folder || status.config.templates_folder || status.config.attachments_folder) && (
              <div>
                from Obsidian config:
                {status.config.daily_notes_folder && ` daily=${status.config.daily_notes_folder}`}
                {status.config.templates_folder && ` templates=${status.config.templates_folder}`}
                {status.config.attachments_folder && ` attachments=${status.config.attachments_folder}`}
              </div>
            )}
          </div>
        ) : (
          <Loader2 className="h-3 w-3 animate-spin" />
        )}
      </div>

      <div className={section}>
        <div className="flex items-center justify-between">
          <span className={label}>Connect an Obsidian vault</span>
          <Button ghost size="sm" onClick={() => void detect()} disabled={scanning}>
            {scanning ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <ScanSearch className="mr-1 h-3 w-3" />}
            Detect
          </Button>
        </div>
        {found?.length === 0 && <p className="text-xs text-muted-foreground">No vaults found. Enter a folder below.</p>}
        {found?.map((v) => (
          <div key={v.path} className="flex items-center justify-between gap-2 text-xs">
            <div className="min-w-0">
              <div className="truncate font-medium">
                {v.name} {v.current && <span className="text-emerald-500">● connected</span>}
                {v.open_in_obsidian && <span className="ml-1 text-muted-foreground">(open in Obsidian)</span>}
              </div>
              <div className="truncate text-muted-foreground">
                {v.path} · {v.note_count} notes
                {v.last_modified ? ` · ${v.last_modified.slice(0, 10)}` : ""}
              </div>
            </div>
            <Button size="sm" disabled={busy || v.current} onClick={() => void connect(v.path)}>
              <Plug className="mr-1 h-3 w-3" />
              Connect
            </Button>
          </div>
        ))}
        <div className="flex gap-1">
          <Input
            value={path}
            placeholder="C:\Users\You\Documents\MyVault"
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void connect(path)}
          />
          <Button size="sm" disabled={busy || !path.trim()} onClick={() => void connect(path)}>
            Connect
          </Button>
          <Button ghost size="sm" disabled={busy || !path.trim()} onClick={() => void connect(path, true)}>
            Create
          </Button>
        </div>
        <label className="flex items-center gap-1 text-xs text-muted-foreground">
          <input type="checkbox" checked={initObs} onChange={(e) => setInitObs(e.target.checked)} />
          make a plain folder openable in Obsidian (creates <code>.obsidian</code>)
        </label>
      </div>

      <div className={section}>
        <span className={label}>
          <FolderPlus className="mr-1 inline h-3 w-3" />
          New branch (folder + _index.md)
        </span>
        <Input value={bName} placeholder="Projects/ARIA moments" onChange={(e) => setBName(e.target.value)} />
        <textarea className={area} rows={2} value={bDesc} placeholder="What this branch is about (optional)" onChange={(e) => setBDesc(e.target.value)} />
        <Button size="sm" disabled={busy || !bName.trim()} onClick={() => void createBranch()}>
          Create branch
        </Button>
      </div>

      <div className={section}>
        <span className={label}>
          <Gavel className="mr-1 inline h-3 w-3" />
          Log a key decision
        </span>
        <Input value={dTitle} placeholder="Title" onChange={(e) => setDTitle(e.target.value)} />
        <textarea className={area} rows={2} value={dText} placeholder="Decision" onChange={(e) => setDText(e.target.value)} />
        <textarea className={area} rows={2} value={dCtx} placeholder="Context / why (optional)" onChange={(e) => setDCtx(e.target.value)} />
        <div className="flex gap-1">
          <select
            className="border border-border bg-background px-1 text-xs"
            value={dBranch}
            onChange={(e) => setDBranch(e.target.value)}
          >
            <option value="">no branch (decisions/)</option>
            {branches.map((b) => (
              <option key={b.path} value={b.path}>
                {b.path}
              </option>
            ))}
          </select>
          <Input value={dTags} placeholder="tags: aria, infra" onChange={(e) => setDTags(e.target.value)} />
        </div>
        <Button size="sm" disabled={busy || !dTitle.trim() || !dText.trim()} onClick={() => void logDecision()}>
          Save decision
        </Button>
      </div>
    </div>
  );
}
