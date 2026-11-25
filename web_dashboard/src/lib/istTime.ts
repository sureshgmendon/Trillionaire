/**
 * Shared IST (Asia/Kolkata) market time & session cutoff utilities.
 */

export function getIstClockString(now: Date = new Date()): string {
  return now.toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * Returns true when current IST time falls in the post-entry cutoff window (14:30 to 15:30 IST).
 */
export function isIstCutoffWindow(now: Date = new Date()): boolean {
  const parts = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(now);

  const hour = Number(parts.find((p) => p.type === "hour")?.value ?? 0);
  const minute = Number(parts.find((p) => p.type === "minute")?.value ?? 0);
  const totalMinutes = hour * 60 + minute;

  return totalMinutes >= 14 * 60 + 30 && totalMinutes <= 15 * 60 + 30;
}
