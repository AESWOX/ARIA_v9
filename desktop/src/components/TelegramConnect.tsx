import { useState } from "react";
import { Loader2, Send } from "lucide-react";
import { api } from "@/lib/api";
import { useI18n } from "@/i18n";
import { Button } from "@vendor/ui/ui/components/button";

function errMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  const match = raw.match(/"detail"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  return match ? match[1].replace(/\\"/g, '"') : raw;
}

export function TelegramConnect({ onSaved }: { onSaved?: () => void }) {
  const { locale } = useI18n();
  const ru = locale.toLowerCase().startsWith("ru");
  const [token, setToken] = useState("");
  const [channel, setChannel] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.telegramConnect(token.trim(), channel.trim());
      setSaved(true);
      setToken("");
      onSaved?.();
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setSaving(false);
    }
  };

  if (saved) {
    return (
      <div className="border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 text-xs">
        {ru ? "Telegram подключён. Отправь вопрос ещё раз." : "Telegram is connected. Send the question again."}
      </div>
    );
  }

  return (
    <div className="space-y-1 border border-border bg-background/60 p-2 text-xs">
      <div className="font-medium">{ru ? "Подключить Telegram" : "Connect Telegram"}</div>
      <input
        type="password"
        autoComplete="off"
        spellCheck={false}
        className="w-full border border-border bg-background/40 px-2 py-1"
        placeholder={ru ? "Токен от @BotFather" : "Token from @BotFather"}
        value={token}
        onChange={(event) => setToken(event.target.value)}
      />
      <input
        className="w-full border border-border bg-background/40 px-2 py-1"
        placeholder={ru ? "@канал или @чат" : "@channel or @chat"}
        value={channel}
        onChange={(event) => setChannel(event.target.value)}
      />
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={saving || !token.trim() || !channel.trim()} onClick={() => void submit()}>
          {saving ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Send className="mr-1 h-3 w-3" />}
          {ru ? "Подключить" : "Connect"}
        </Button>
        <span className="text-muted-foreground">
          {ru ? "Токен сохраняется только локально" : "The token is stored locally only"}
        </span>
      </div>
      {error && <div className="text-destructive">{error}</div>}
    </div>
  );
}
