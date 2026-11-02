/* UX-1: hover meta for chat messages — local send time and processing time. */

export function formatLocalTime(value: string | null | undefined, locale: string): string | null {
  if (!value) return null;
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return null;
  return when.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Seconds from the user's message to the assistant answer; null when unknown. */
export function processingSeconds(
  sentAt: string | null | undefined,
  answeredAt: string | null | undefined,
): number | null {
  if (!sentAt || !answeredAt) return null;
  const start = Date.parse(sentAt);
  const end = Date.parse(answeredAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return (end - start) / 1000;
}
