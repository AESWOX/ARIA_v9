// Vendor module declarations — @vendor/ui has no .d.ts files
// This file is included in tsconfig.app.json via src/**/*

declare module "@vendor/ui" {
  import type { ComponentType } from "react";
  export const ChatMessage: ComponentType<Record<string, unknown>>;
  export const ChatInput: ComponentType<Record<string, unknown>>;
  export const ModelSelector: ComponentType<Record<string, unknown>>;
  export const SettingsPanel: ComponentType<Record<string, unknown>>;
  export const TopBar: ComponentType<Record<string, unknown>>;
  export const Sidebar: ComponentType<Record<string, unknown>>;
}

declare module "@vendor/ui/hooks/use-gpu-tier" {
  export function useGpuTier(): number;
}

declare module "@vendor/ui/assets/*" {
  const src: string;
  export default src;
}

declare module "*.webp" {
  const src: string;
  export default src;
}
