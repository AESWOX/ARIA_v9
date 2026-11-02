/* D-39: the chat has two modes. There is no separate Plan tab: the Agent
   builds its plan internally through make_plan. */

export type ChatMode = "chat" | "agent";

export interface ChatModeOption {
  id: ChatMode;
  label: string;
  hint: string;
}

export const CHAT_MODES: ChatModeOption[] = [
  { id: "chat", label: "Chat", hint: "Answers only, no tools" },
  { id: "agent", label: "Agent", hint: "Works step by step with tools; risky steps ask you first" },
];

/** Legacy sessions and runs may still report "plan": silently treat it as Agent. */
export function normalizeChatMode(value: unknown): ChatMode {
  return value === "agent" || value === "plan" ? "agent" : "chat";
}
