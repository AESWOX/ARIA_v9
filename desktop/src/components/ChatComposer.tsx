import { useEffect, useRef } from "react";
import { ChevronUp, Loader2, Plus, Send, Settings2, Square, X } from "lucide-react";
import type { ChatModels, ProfileSeat, RunProfile } from "@/lib/api";
import type { ChatMode } from "@/lib/chat-modes";
import { CHAT_MODES } from "@/lib/chat-modes";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";
import { Button } from "@vendor/ui/ui/components/button";
import { TeamPanel } from "@/components/TeamPanel";

export interface ComposerAttachment {
  name: string;
  path: string;
}

function seatValue(seat: ProfileSeat | undefined): string {
  if (!seat) return "";
  if (seat.model) return `m:${seat.model}`;
  if (seat.tier) return `t:${seat.tier}`;
  return "";
}

function parseSeat(value: string): ProfileSeat {
  if (value.startsWith("m:")) return { model: value.slice(2) };
  if (value.startsWith("t:")) return { tier: value.slice(2) as ProfileSeat["tier"] };
  return {};
}

const TIERS: NonNullable<ProfileSeat["tier"]>[] = ["premium", "standard", "free", "fast"];

export function ChatComposer({
  input,
  onInputChange,
  attachments,
  uploading,
  onAddFiles,
  onRemoveAttachment,
  sending,
  busy,
  mode,
  onModeChange,
  profile,
  profileNotes,
  models,
  onChangeProfile,
  onSaveDefault,
  savingDefault,
  onSend,
  onStop,
  advancedOpen,
  onToggleAdvanced,
  error,
}: {
  input: string;
  onInputChange: (value: string) => void;
  attachments: ComposerAttachment[];
  uploading: boolean;
  onAddFiles: (files: FileList | null) => void;
  onRemoveAttachment: (index: number) => void;
  sending: boolean;
  busy: boolean;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  profile: RunProfile | null;
  profileNotes: string[];
  models: ChatModels | null;
  onChangeProfile: (profile: RunProfile) => void;
  onSaveDefault: () => void;
  savingDefault: boolean;
  onSend: () => void;
  onStop: () => void;
  advancedOpen: boolean;
  onToggleAdvanced: () => void;
  error: string | null;
}) {
  const { t } = useI18n();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(Math.max(el.scrollHeight, 36), 168)}px`;
  }, [input]);

  useEffect(() => {
    if (!advancedOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onToggleAdvanced();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [advancedOpen, onToggleAdvanced]);

  const disabled = sending || busy;
  const active = CHAT_MODES.find((item) => item.id === mode) ?? CHAT_MODES[0];

  return (
    <div className="relative border-t border-border bg-background/80 p-2">
      {advancedOpen && <div className="fixed inset-0 z-20" onClick={onToggleAdvanced} />}
      {advancedOpen && profile && models && (
        <div className="absolute bottom-full left-2 right-2 z-30 mb-1 max-h-[60vh] overflow-y-auto border border-border bg-background p-2 shadow-lg">
          <TeamPanel
            profile={profile}
            models={models}
            disabled={busy}
            notes={profileNotes}
            onChange={onChangeProfile}
            allowBoss={mode === "plan"}
            onRequestPlan={() => onModeChange("plan")}
          />
          <div className="flex items-center justify-between gap-2 px-2 py-1">
            <Button ghost size="sm" disabled={savingDefault || busy} onClick={onSaveDefault}>
              {savingDefault ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
              {t.chatComposer.useForNew}
            </Button>
            <div className="flex items-center gap-1">
              <Button
                ghost
                size="sm"
                disabled={busy}
                onClick={() =>
                  onChangeProfile({ style: "solo", audit: "strict", thinking: "off", main: {}, workers: {}, auditor: {} })
                }
              >
                {t.chatComposer.reset}
              </Button>
              <Button ghost size="sm" onClick={onToggleAdvanced}>
                <X className="mr-1 h-3 w-3" /> {t.chatComposer.close}
              </Button>
            </div>
          </div>
        </div>
      )}

      <div
        className="mx-auto flex w-full max-w-[920px] flex-col gap-2 rounded-xl border border-border bg-background/70 p-2 shadow-sm"
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes("Files")) event.preventDefault();
        }}
        onDrop={(event) => {
          if (event.dataTransfer.files.length === 0) return;
          event.preventDefault();
          onAddFiles(event.dataTransfer.files);
        }}
      >
        {(attachments.length > 0 || uploading) && (
          <div className="flex flex-wrap items-center gap-1">
            {attachments.map((item, index) => (
              <span key={`${item.path}-${index}`} className="flex items-center gap-1 border border-border px-2 py-0.5 text-xs">
                {item.name}
                <button type="button" aria-label={item.name} onClick={() => onRemoveAttachment(index)}>
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
            {uploading && <span className="text-xs text-muted-foreground">{t.common.loading}</span>}
          </div>
        )}

        <textarea
          ref={textareaRef}
          className="min-h-[36px] w-full resize-none bg-transparent px-2 py-1 text-sm leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none"
          placeholder={mode === "plan" ? t.chatComposer.placeholderPlan : t.chatComposer.placeholderChat}
          value={input}
          disabled={disabled}
          onChange={(event) => onInputChange(event.target.value)}
          onPaste={(event) => {
            if (event.clipboardData.files.length === 0) return;
            event.preventDefault();
            onAddFiles(event.clipboardData.files);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              onSend();
            }
          }}
        />

        <div className="flex flex-wrap items-center gap-1">
          <input
            ref={fileRef}
            type="file"
            multiple
            className="hidden"
            onChange={(event) => {
              onAddFiles(event.target.files);
              event.target.value = "";
            }}
          />
          <Button ghost size="sm" aria-label={t.app.attachFiles} title={t.app.attachFiles} disabled={disabled} onClick={() => fileRef.current?.click()}>
            <Plus className="h-3.5 w-3.5" />
          </Button>

          {profile && models && (
            <select
              className="h-7 max-w-[170px] border border-border bg-background px-1 text-xs"
              value={seatValue(profile.main)}
              disabled={disabled}
              aria-label={t.chatComposer.model}
              onChange={(event) => onChangeProfile({ ...profile, main: parseSeat(event.target.value) })}
            >
              <option value="">{t.chatComposer.auto}</option>
              {TIERS.map((tier) => (
                <option key={tier} value={`t:${tier}`} disabled={!models.tiers[tier ?? ""]}>
                  {tier}{models.tiers[tier ?? ""] ? "" : ` (${t.chatComposer.noKey})`}
                </option>
              ))}
              {models.models.map((model) => (
                <option key={model.id} value={`m:${model.id}`}>{model.model}</option>
              ))}
            </select>
          )}

          {profile && (
            <select
              className="h-7 border border-border bg-background px-1 text-xs"
              value={profile.thinking}
              disabled={disabled}
              aria-label={t.chatComposer.thinking}
              onChange={(event) => onChangeProfile({ ...profile, thinking: event.target.value as RunProfile["thinking"] })}
            >
              <option value="off">{t.chatComposer.thinkingFast}</option>
              <option value="medium">{t.chatComposer.thinkingBalanced}</option>
              <option value="high">{t.chatComposer.thinkingDeep}</option>
            </select>
          )}

          <div className="flex border border-border" role="radiogroup" aria-label={t.chatComposer.mode}>
            {CHAT_MODES.map((item) => (
              <button
                key={item.id}
                type="button"
                role="radio"
                aria-checked={mode === item.id}
                title={item.hint}
                disabled={disabled}
                onClick={() => onModeChange(item.id)}
                className={cn(
                  "px-2 py-0.5 text-xs",
                  mode === item.id ? "bg-foreground/10 font-medium" : "text-muted-foreground hover:bg-foreground/5",
                )}
              >
                {item.label}
              </button>
            ))}
          </div>

          <Button ghost size="sm" disabled={!profile || !models} onClick={onToggleAdvanced} aria-expanded={advancedOpen}>
            <Settings2 className="mr-1 h-3 w-3" /> {t.chatComposer.advanced}
            <ChevronUp className={cn("ml-1 h-3 w-3 transition-transform", advancedOpen && "rotate-180")} />
          </Button>

          <span className="flex-1" />

          {sending && mode === "chat" ? (
            <Button ghost aria-label={t.chatComposer.stop} title={t.chatComposer.stop} onClick={onStop}>
              <Square className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              aria-label={t.chatComposer.send}
              title={t.chatComposer.send}
              disabled={disabled || (!input.trim() && attachments.length === 0)}
              onClick={onSend}
            >
              {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            </Button>
          )}
        </div>

        <div className="text-[10px] text-muted-foreground">{active.hint}</div>
      </div>

      {error && <div className="mx-auto mt-1 max-w-[920px] text-xs text-destructive">{error}</div>}
    </div>
  );
}
