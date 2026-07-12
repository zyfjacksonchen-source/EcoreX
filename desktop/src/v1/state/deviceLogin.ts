export function safeDeviceVerificationUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && parsed.username === "" && parsed.password === ""
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

export function devicePollSeconds(nextPollAt: string, now: number): number {
  const timestamp = Date.parse(nextPollAt);
  if (!Number.isFinite(timestamp)) return 0;
  return Math.max(0, Math.ceil((timestamp - now) / 1_000));
}

export function deviceStatusRefreshDelay(
  nextPollAt: string,
  pollIntervalSeconds: number,
  now: number,
): number {
  const timestamp = Date.parse(nextPollAt);
  const fallback = Math.max(1, pollIntervalSeconds) * 1_000;
  return Number.isFinite(timestamp)
    ? Math.max(1_000, Math.min(30_000, timestamp - now))
    : Math.min(30_000, fallback);
}
