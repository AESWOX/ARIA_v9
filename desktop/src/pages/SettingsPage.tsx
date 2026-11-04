import { Link } from "react-router-dom";
import {
  BarChart3,
  BookOpen,
  Clock,
  Cpu,
  KeyRound,
  NotebookText,
  Package,
  Plug,
  Radio,
  Settings,
  ShieldCheck,
  Users,
  Webhook,
  Wrench,
} from "lucide-react";
import { useEffect, useState, type ComponentType } from "react";
import { Card, CardContent } from "@vendor/ui/ui/components/card";
import { H2 } from "@vendor/ui/ui/components/typography/h2";
import { Toast } from "@vendor/ui/ui/components/toast";
import { useToast } from "@vendor/ui/hooks/use-toast";
import { useI18n } from "@/i18n";
import { AppearanceCard } from "@/components/AppearanceCard";
import { api } from "@/lib/api";

type SettingsLink = {
  to: string;
  label: string;
  description: string;
  icon: ComponentType<{ className?: string }>;
};

type SettingsGroup = { title: string; links: SettingsLink[] };

const GROUPS: SettingsGroup[] = [
  {
    title: "Agent",
    links: [
      { to: "/models", label: "Models", description: "Choose the model and provider", icon: Cpu },
      { to: "/skills", label: "Skills", description: "Manage installed agent skills", icon: Package },
      { to: "/profiles", label: "Profiles", description: "Switch and edit agent profiles", icon: Users },
      { to: "/config", label: "Config", description: "Edit application settings", icon: Settings },
      { to: "/env", label: "Keys", description: "API keys and environment values", icon: KeyRound },
    ],
  },
  {
    title: "Automation",
    links: [
      { to: "/cron", label: "Cron", description: "Scheduled agent jobs", icon: Clock },
      { to: "/mcp", label: "MCP", description: "Connected MCP servers and tools", icon: Plug },
      { to: "/webhooks", label: "Webhooks", description: "Incoming webhook triggers", icon: Webhook },
      { to: "/channels", label: "Channels", description: "Messaging platform status", icon: Radio },
      { to: "/pairing", label: "Pairing", description: "Paired messaging users", icon: ShieldCheck },
    ],
  },
  {
    title: "App",
    links: [
      { to: "/analytics", label: "Analytics", description: "Usage statistics and trends", icon: BarChart3 },
      { to: "/notes", label: "Notes", description: "Your saved notes", icon: NotebookText },
      { to: "/system", label: "System", description: "Status, diagnostics and operations", icon: Wrench },
      { to: "/docs", label: "Documentation", description: "Read the ARIA documentation", icon: BookOpen },
    ],
  },
];

function AgentShellCard() {
  const { t } = useI18n();
  const s = t.common.agentShell;
  const { toast, showToast } = useToast();
  const [enabled, setEnabled] = useState(false);
  const [root, setRoot] = useState("");
  const [timeoutSec, setTimeoutSec] = useState(120);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .getConfig()
      .then((cfg) => {
        setConfig(cfg);
        setEnabled(Boolean(cfg.agent_shell_enabled));
        setRoot(String(cfg.agent_project_root ?? ""));
        setTimeoutSec(Number(cfg.agent_shell_timeout_sec ?? 120));
      })
      .catch(() => undefined);
  }, []);

  const save = async () => {
    if (!config) return;
    setBusy(true);
    try {
      const next = {
        ...config,
        agent_shell_enabled: enabled,
        agent_project_root: root.trim(),
        agent_shell_timeout_sec: Math.max(5, Math.min(600, Math.round(timeoutSec) || 120)),
      };
      await api.saveConfig(next);
      setConfig(next);
      showToast(s?.saved ?? "Agent shell settings saved", "success");
    } catch (e) {
      showToast(String(e), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="border-border">
      <CardContent className="flex flex-col gap-3 p-4">
        <div className="flex flex-col gap-1">
          <span className="text-sm font-medium">{s?.title ?? "Shell in Agent"}</span>
          <span className="text-xs text-muted-foreground">
            {s?.hint ??
              "general stays read-only until this is on. When on, shell runs only inside the project root and always asks for approval."}
          </span>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {s?.enabled ?? "Allow shell_execute in Agent"}
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>{s?.projectRoot ?? "Project root"}</span>
          <input
            className="border border-border bg-background/40 px-3 py-2 text-sm"
            value={root}
            onChange={(e) => setRoot(e.target.value)}
            placeholder={s?.projectRootHint ?? "C:\\path\\to\\project"}
          />
          <span className="text-xs text-muted-foreground">{s?.projectRootHint ?? ""}</span>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Timeout, seconds (5–600)</span>
          <input
            type="number"
            min={5}
            max={600}
            className="w-32 border border-border bg-background/40 px-3 py-2 text-sm"
            value={timeoutSec}
            onChange={(e) => setTimeoutSec(Number(e.target.value))}
          />
        </label>
        <div>
          <button
            type="button"
            className="border border-border px-3 py-1.5 text-sm disabled:opacity-50"
            onClick={() => void save()}
            disabled={busy || !config}
          >
            {t.common.save}
          </button>
        </div>
        <Toast toast={toast} />
      </CardContent>
    </Card>
  );
}

export default function SettingsPage() {
  return (
    <div className="flex flex-col gap-8">
      <H2 variant="sm" className="text-muted-foreground">
        Settings
      </H2>
      <AppearanceCard />
      <AgentShellCard />
      {GROUPS.map((group) => (
        <div key={group.title} className="flex flex-col gap-3">
          <H2 variant="sm" className="text-muted-foreground">
            {group.title}
          </H2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {group.links.map(({ to, label, description, icon: Icon }) => (
              <Link key={to} to={to} className="block">
                <Card className="h-full border-border transition-colors hover:border-primary/50">
                  <CardContent className="flex items-start gap-3 p-4">
                    <Icon className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <span className="text-sm font-medium">{label}</span>
                      <span className="text-xs text-muted-foreground">{description}</span>
                    </div>
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
