import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  FileText,
  Folder,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Search,
} from "lucide-react";
import { api } from "@/lib/api";
import type { VaultSearchMatch, VaultTree } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@vendor/ui/ui/components/button";
import { Input } from "@vendor/ui/ui/components/input";
import { useToast } from "@vendor/ui/hooks/use-toast";
import { Toast } from "@vendor/ui/ui/components/toast";

/* ------------------------------------------------------------------ */
/*  NotesPage - browse, search, create and edit the Obsidian vault     */
/*  (backend: /api/vault/*). Plain markdown, no preview: the vault is  */
/*  the same folder Obsidian opens, so edits show up there as well.    */
/* ------------------------------------------------------------------ */

const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);

function errMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

interface TreeNodeProps {
  dir: string;
  depth: number;
  trees: Record<string, VaultTree>;
  expanded: Set<string>;
  selected: string | null;
  onToggle: (dir: string) => void;
  onOpen: (path: string) => void;
}

function TreeNode({ dir, depth, trees, expanded, selected, onToggle, onOpen }: TreeNodeProps) {
  const tree = trees[dir];
  if (!tree) {
    return (
      <div className="px-2 py-1 text-xs text-muted-foreground" style={{ paddingLeft: 8 + depth * 14 }}>
        <Loader2 className="inline h-3 w-3 animate-spin" />
      </div>
    );
  }
  return (
    <>
      {tree.dirs.map((d) => {
        const path = join(dir, d);
        const open = expanded.has(path);
        return (
          <div key={`d:${path}`}>
            <button
              type="button"
              onClick={() => onToggle(path)}
              className="flex w-full items-center gap-1 px-2 py-1 text-left text-xs hover:bg-foreground/5"
              style={{ paddingLeft: 8 + depth * 14 }}
            >
              {open ? <ChevronDown className="h-3 w-3 shrink-0" /> : <ChevronRight className="h-3 w-3 shrink-0" />}
              <Folder className="h-3 w-3 shrink-0 text-muted-foreground" />
              <span className="truncate">{d}</span>
            </button>
            {open && (
              <TreeNode
                dir={path}
                depth={depth + 1}
                trees={trees}
                expanded={expanded}
                selected={selected}
                onToggle={onToggle}
                onOpen={onOpen}
              />
            )}
          </div>
        );
      })}
      {tree.notes.map((n) => {
        const path = join(dir, n);
        return (
          <button
            type="button"
            key={`n:${path}`}
            onClick={() => onOpen(path)}
            className={cn(
              "flex w-full items-center gap-1 px-2 py-1 text-left text-xs hover:bg-foreground/5",
              selected === path && "bg-foreground/10",
            )}
            style={{ paddingLeft: 8 + depth * 14 + 16 }}
          >
            <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
            <span className="truncate">{n.replace(/\.md$/, "")}</span>
          </button>
        );
      })}
    </>
  );
}

export default function NotesPage() {
  const { toast, showToast } = useToast();
  const [trees, setTrees] = useState<Record<string, VaultTree>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [rootError, setRootError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [content, setContent] = useState("");
  const [original, setOriginal] = useState("");
  const [loadingNote, setLoadingNote] = useState(false);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<VaultSearchMatch[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [newPath, setNewPath] = useState("");
  const dirtyRef = useRef(false);

  const dirty = selected !== null && content !== original;
  dirtyRef.current = dirty;

  const loadDir = useCallback(async (dir: string) => {
    try {
      const tree = await api.getVaultTree(dir);
      setTrees((prev) => ({ ...prev, [dir]: tree }));
      if (dir === "") setRootError(null);
    } catch (e) {
      if (dir === "") setRootError(errMessage(e));
      else showToast(errMessage(e), "error");
    }
  }, [showToast]);

  const reload = useCallback(() => {
    setTrees({});
    void loadDir("");
    expanded.forEach((d) => void loadDir(d));
  }, [expanded, loadDir]);

  useEffect(() => {
    void loadDir("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Warn before closing the tab/window with unsaved changes.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);

  const confirmDiscard = () => !dirtyRef.current || window.confirm("Discard unsaved changes?");

  const toggle = (dir: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else {
        next.add(dir);
        if (!trees[dir]) void loadDir(dir);
      }
      return next;
    });
  };

  const openNote = async (path: string) => {
    if (path === selected && !isNew) return;
    if (!confirmDiscard()) return;
    setLoadingNote(true);
    try {
      const note = await api.getVaultNote(path);
      setSelected(note.path ? note.path.replace(/\\/g, "/") : path);
      setIsNew(false);
      setContent(note.content ?? "");
      setOriginal(note.content ?? "");
    } catch (e) {
      showToast(`Cannot open ${path}: ${errMessage(e)}`, "error");
    } finally {
      setLoadingNote(false);
    }
  };

  const startNew = () => {
    const raw = newPath.trim().replace(/^\/+/, "").replace(/\\/g, "/");
    if (!raw) return;
    if (raw.split("/").some((seg) => seg === ".." || seg === "." || seg === "")) {
      showToast("Use a relative path like 00-TASKS/idea (no '..').", "error");
      return;
    }
    if (!confirmDiscard()) return;
    const path = raw.toLowerCase().endsWith(".md") ? raw : `${raw}.md`;
    setSelected(path);
    setIsNew(true);
    setContent("");
    setOriginal("");
    setNewPath("");
  };

  const save = useCallback(async () => {
    if (!selected) return;
    setSaving(true);
    try {
      const res = await api.putVaultNote(selected, content);
      const saved = res.path.replace(/\\/g, "/");
      setSelected(saved);
      setOriginal(content);
      setIsNew(false);
      showToast(`${saved} saved ✓`, "success");
      // refresh the folder that now holds the note (and its ancestors)
      const parts = saved.split("/");
      parts.pop();
      const dirs = ["", ...parts.map((_, i) => parts.slice(0, i + 1).join("/"))];
      setExpanded((prev) => new Set([...prev, ...dirs.filter(Boolean)]));
      dirs.forEach((d) => void loadDir(d));
    } catch (e) {
      showToast(`Save failed: ${errMessage(e)}`, "error");
    } finally {
      setSaving(false);
    }
  }, [content, loadDir, selected, showToast]);

  // Ctrl/Cmd+S
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (dirtyRef.current && !saving) void save();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [save, saving]);

  const runSearch = async () => {
    const q = query.trim();
    if (!q) {
      setResults(null);
      return;
    }
    setSearching(true);
    try {
      const res = await api.searchVault(q);
      setResults(res.matches);
    } catch (e) {
      showToast(`Search failed: ${errMessage(e)}`, "error");
    } finally {
      setSearching(false);
    }
  };

  const rootEmpty = useMemo(() => {
    const root = trees[""];
    return !!root && root.dirs.length === 0 && root.notes.length === 0;
  }, [trees]);

  return (
    <div className="flex flex-col gap-3">
      <Toast toast={toast} />
      <div className="grid gap-3 md:grid-cols-[300px_1fr]">
        {/* ---------------- left: search + tree ---------------- */}
        <div className="flex min-h-[200px] flex-col gap-2 border border-border bg-background/40 p-2">
          <div className="flex gap-1">
            <Input
              value={query}
              placeholder="Search notes..."
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void runSearch()}
            />
            <Button ghost size="sm" onClick={() => void runSearch()} disabled={searching} aria-label="Search">
              {searching ? <Loader2 className="h-3 w-3 animate-spin" /> : <Search className="h-3 w-3" />}
            </Button>
            <Button ghost size="sm" onClick={reload} aria-label="Refresh">
              <RefreshCw className="h-3 w-3" />
            </Button>
          </div>
          <div className="flex gap-1">
            <Input
              value={newPath}
              placeholder="New note: folder/name"
              onChange={(e) => setNewPath(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && startNew()}
            />
            <Button ghost size="sm" onClick={startNew} aria-label="New note">
              <Plus className="h-3 w-3" />
            </Button>
          </div>

          <div className="max-h-[65vh] overflow-y-auto">
            {results !== null ? (
              <div>
                <div className="flex items-center justify-between px-2 py-1 text-xs text-muted-foreground">
                  <span>{results.length} match(es)</span>
                  <button type="button" className="underline" onClick={() => setResults(null)}>
                    back to tree
                  </button>
                </div>
                {results.map((m, i) => (
                  <button
                    type="button"
                    key={`${m.file_path}:${m.line}:${i}`}
                    onClick={() => void openNote(m.file_path)}
                    className="block w-full px-2 py-1 text-left text-xs hover:bg-foreground/5"
                  >
                    <div className="truncate font-medium">
                      {m.file_path}:{m.line}
                    </div>
                    <div className="truncate text-muted-foreground">{m.snippet}</div>
                  </button>
                ))}
              </div>
            ) : rootError ? (
              <p className="p-2 text-xs text-destructive">Cannot read the vault: {rootError}</p>
            ) : rootEmpty ? (
              <p className="p-2 text-xs text-muted-foreground">
                The vault is empty. To use your existing Obsidian vault, set OBSIDIAN_VAULT_PATH on the Keys page
                to its folder (the one that contains <code>.obsidian</code>), then press refresh.
              </p>
            ) : (
              <TreeNode
                dir=""
                depth={0}
                trees={trees}
                expanded={expanded}
                selected={selected}
                onToggle={toggle}
                onOpen={(p) => void openNote(p)}
              />
            )}
          </div>
        </div>

        {/* ---------------- right: editor ---------------- */}
        <div className="flex min-h-[320px] flex-col gap-2 border border-border bg-background/40 p-2">
          {selected === null ? (
            <p className="p-2 text-xs text-muted-foreground">
              Pick a note on the left, or type a path such as <code>00-TASKS/idea</code> and press +.
            </p>
          ) : (
            <>
              <div className="flex items-center justify-between gap-2">
                <div className="truncate text-xs font-medium">
                  {selected}
                  {isNew && <span className="ml-2 text-muted-foreground">(new)</span>}
                  {dirty && <span className="ml-2 text-amber-500">● unsaved</span>}
                </div>
                <Button size="sm" onClick={() => void save()} disabled={!dirty || saving}>
                  {saving ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Save className="mr-1 h-3 w-3" />}
                  Save
                </Button>
              </div>
              {loadingNote ? (
                <div className="p-4 text-xs text-muted-foreground">
                  <Loader2 className="inline h-3 w-3 animate-spin" /> loading...
                </div>
              ) : (
                <textarea
                  spellCheck={false}
                  className="min-h-[55vh] w-full resize-y border border-border bg-background/40 px-3 py-2 font-mono text-xs leading-relaxed shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30"
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
