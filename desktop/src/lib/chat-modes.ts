/* D-39 / PLAN 0.2.1: the composer has two modes. "Plan" runs the agent
   pipeline (plan + tools + Approve); legacy sessions that report "agent"
   are shown as Plan. The API keeps using mode="agent". */

export type ChatMode = "chat" | "plan";

export interface ChatModeOption {
  id: ChatMode;
  label: string;
  hint: string;
}

export const CHAT_MODES: ChatModeOption[] = [
  { id: "chat", label: "Chat", hint: "Answers only, no tools" },
  { id: "plan", label: "Plan", hint: "Plans, runs steps with tools and asks before risky actions" },
];

/** Legacy sessions and runs may still report "agent": show them as Plan. */
export function normalizeChatMode(value: unknown): ChatMode {
  return value === "agent" || value === "plan" ? "plan" : "chat";
}

/** Backend wire value for a UI mode (the API still calls it "agent"). */
export function runModeFor(mode: ChatMode): "agent" | "plan" {
  return mode === "plan" ? "agent" : "plan";
}
