import { useState } from "react";
import { KeyRound, ShieldCheck, Sparkles, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "@vendor/ui/ui/components/button";
import { H2 } from "@vendor/ui/ui/components/typography/h2";
import { useI18n } from "@/i18n";
import type { Translations } from "@/i18n/types";
import { cn, themedBody } from "@/lib/utils";

type Step = "welcome" | "pin" | "token";

const STEPS: Step[] = ["welcome", "pin", "token"];

const FALLBACK: NonNullable<Translations["onboarding"]> = {
  welcomeTitle: "Welcome to ARIA",
  welcomeBody:
    "Your local agent runtime is up. This quick walkthrough shows you how the security model and access token work — it takes about a minute.",
  pinTitle: "Your PIN lock",
  pinBody:
    "ARIA auto-locks after inactivity. On the first launch a 6-digit PIN was generated and written to your config. You can change it anytime under Keys → LOCAL_AGENT_UI_PIN.",
  pinHint: "If you forget the PIN, delete the lock state in your config directory.",
  openKeys: "Open Keys",
  tokenTitle: "Your runtime token",
  tokenBody:
    "The runtime token authenticates this app against the local backend. It lives in the bootstrap file next to your config and is rotated on every launch.",
  tokenHint: "Keep it private — anyone with the token controls your local agent.",
  skip: "Skip",
  back: "Back",
  next: "Next",
  done: "Get started",
};

interface Props {
  onDone: () => void;
}

const STEP_ICONS: Record<Step, typeof Sparkles> = {
  welcome: Sparkles,
  pin: ShieldCheck,
  token: KeyRound,
};

export function OnboardingOverlay({ onDone }: Props) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>("welcome");

  const ob = t.onboarding;
  const txt = {
    welcomeTitle: ob?.welcomeTitle ?? FALLBACK.welcomeTitle,
    welcomeBody: ob?.welcomeBody ?? FALLBACK.welcomeBody,
    pinTitle: ob?.pinTitle ?? FALLBACK.pinTitle,
    pinBody: ob?.pinBody ?? FALLBACK.pinBody,
    pinHint: ob?.pinHint ?? FALLBACK.pinHint,
    openKeys: ob?.openKeys ?? FALLBACK.openKeys,
    tokenTitle: ob?.tokenTitle ?? FALLBACK.tokenTitle,
    tokenBody: ob?.tokenBody ?? FALLBACK.tokenBody,
    tokenHint: ob?.tokenHint ?? FALLBACK.tokenHint,
    skip: ob?.skip ?? FALLBACK.skip,
    back: ob?.back ?? FALLBACK.back,
    next: ob?.next ?? FALLBACK.next,
    done: ob?.done ?? FALLBACK.done,
  };

  const Icon = STEP_ICONS[step];

  const openKeys = () => {
    navigate("/env");
    onDone();
  };

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="onboarding-title"
    >
      <div className={cn(themedBody, "relative w-full max-w-md border border-border bg-card shadow-2xl")}>
        <Button
          ghost
          size="icon"
          onClick={onDone}
          className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
          aria-label={txt.skip}
        >
          <X />
        </Button>
        <div className="p-6 flex flex-col gap-5">
          <div className="flex flex-col gap-3">
            <span className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-current/20 bg-secondary/40 text-midground">
              <Icon className="h-5 w-5" />
            </span>
            <H2
              id="onboarding-title"
              variant="sm"
              mondwest
              className="tracking-wider uppercase"
            >
              {step === "welcome" && txt.welcomeTitle}
              {step === "pin" && txt.pinTitle}
              {step === "token" && txt.tokenTitle}
            </H2>
            <p className="text-sm leading-relaxed text-muted-foreground">
              {step === "welcome" && txt.welcomeBody}
              {step === "pin" && txt.pinBody}
              {step === "token" && txt.tokenBody}
            </p>
            {step !== "welcome" && (
              <p className="text-xs text-muted-foreground/80 border-t border-border pt-3">
                {step === "pin" ? txt.pinHint : txt.tokenHint}
              </p>
            )}
          </div>

          <div className="flex items-center justify-between gap-2 pt-1">
            <Button ghost onClick={onDone} className="text-muted-foreground hover:text-foreground">
              {txt.skip}
            </Button>

            <div className="flex items-center gap-2">
              {step !== "welcome" && (
                <Button
                  outlined
                  onClick={() =>
                    setStep(STEPS[Math.max(0, STEPS.indexOf(step) - 1)])
                  }
                >
                  {txt.back}
                </Button>
              )}

              {step === "welcome" && (
                <Button onClick={() => setStep("pin")}>{txt.next}</Button>
              )}
              {step === "pin" && <Button onClick={openKeys}>{txt.openKeys}</Button>}
              {step === "token" && <Button onClick={onDone}>{txt.done}</Button>}
            </div>
          </div>

          <div className="flex justify-center gap-1.5" aria-hidden>
            {STEPS.map((s) => (
              <span
                key={s}
                className={cn(
                  "h-1 rounded-full transition-all",
                  s === step ? "w-5 bg-midground" : "w-1.5 bg-muted-foreground/30",
                )}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
