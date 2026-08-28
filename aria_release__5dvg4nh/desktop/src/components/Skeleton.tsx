/**
 * Skeleton — lightweight shimmer placeholders (Tailwind `animate-pulse` only).
 *
 * Used as:
 *   • Suspense fallback for lazy-loaded routes (App.tsx)
 *   • first-load placeholder on data-driven pages (SessionsPage, ChatPage)
 *
 * P2.4: keeps first paint honest without shipping a spinner-only flash.
 */
import { cn } from "@/lib/utils";

export function SkeletonBlock({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cn("animate-pulse rounded-md bg-midground/10", className)}
    />
  );
}

/** Full-page placeholder: page-header bar + summary lines + card rows. */
export function PageSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div
      className="flex w-full min-w-0 flex-col gap-4 p-1"
      role="status"
      aria-label="Loading…"
      aria-busy="true"
    >
      <div className="flex items-center justify-between gap-3">
        <SkeletonBlock className="h-7 w-44" />
        <SkeletonBlock className="h-7 w-24" />
      </div>
      <SkeletonBlock className="h-3 w-2/3" />
      <SkeletonBlock className="h-3 w-1/2" />

      <div className="mt-2 flex flex-col gap-2">
        {Array.from({ length: rows }, (_, i) => (
          <div
            key={i}
            className="flex items-center gap-3 rounded-lg border border-current/10 p-3"
          >
            <SkeletonBlock className="h-4 w-4 shrink-0" />
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <SkeletonBlock className="h-3 w-1/3" />
              <SkeletonBlock className="h-3 w-1/2" />
            </div>
            <SkeletonBlock className="h-6 w-16 shrink-0" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** Terminal-window placeholder shown while the PTY WebSocket connects. */
export function TerminalSkeleton() {
  return (
    <div
      className="flex h-full min-h-0 w-full flex-col gap-3 p-4"
      role="status"
      aria-label="Connecting to terminal…"
      aria-busy="true"
    >
      <div className="flex items-center gap-2">
        <SkeletonBlock className="h-3 w-3 rounded-full" />
        <SkeletonBlock className="h-3 w-3 rounded-full" />
        <SkeletonBlock className="h-3 w-3 rounded-full" />
        <SkeletonBlock className="ml-2 h-3 w-40" />
      </div>
      {(
        [
          "w-full",
          "w-11/12",
          "w-full",
          "w-4/5",
          "w-full",
          "w-2/3",
          "w-11/12",
          "w-1/2",
        ] as const
      ).map((w, i) => (
        <SkeletonBlock key={i} className={cn("h-3", w)} />
      ))}
      <SkeletonBlock className="h-3 w-2/3" />
      <SkeletonBlock className="h-3 w-1/3" />
      <SkeletonBlock className="mt-auto h-8 w-full" />
    </div>
  );
}
