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

function getIstParts(now: Date = new Date()): { weekday: string; hour: number; minute: number } {
  const parts = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(now);

  return {
    weekday: parts.find((p) => p.type === "weekday")?.value ?? "",
    hour: Number(parts.find((p) => p.type === "hour")?.value ?? 0),
    minute: Number(parts.find((p) => p.type === "minute")?.value ?? 0),
  };
}

/**
 * Returns true when current IST time falls in the post-entry cutoff window (14:30 to 15:30 IST).
 */
export function isIstCutoffWindow(now: Date = new Date()): boolean {
  const { hour, minute } = getIstParts(now);
  const totalMinutes = hour * 60 + minute;
  return totalMinutes >= 14 * 60 + 30 && totalMinutes <= 15 * 60 + 30;
}

/**
 * Returns true during NSE regular market hours (Mon-Fri, 09:15 to 15:30 IST).
 */
export function isMarketHoursIST(now: Date = new Date()): boolean {
  const { weekday, hour, minute } = getIstParts(now);
  if (weekday === "Sat" || weekday === "Sun") return false;
  const totalMinutes = hour * 60 + minute;
  return totalMinutes >= 9 * 60 + 15 && totalMinutes <= 15 * 60 + 30;
}
