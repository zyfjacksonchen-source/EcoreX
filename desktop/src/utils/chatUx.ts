export const CHAT_SCROLL_THRESHOLD_PX = 80;

export type ScrollMetrics = {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
};

export type ChatScrollState = {
  distanceFromBottom: number;
  autoScrollEnabled: boolean;
  showJumpLatest: boolean;
};

export type SessionLike = {
  id?: string;
  session_id?: string;
  last_active?: string | number | null;
  updatedAt?: string | number | null;
};

export function distanceFromBottom(metrics: ScrollMetrics) {
  return Math.max(0, metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight);
}

export function getChatScrollState(
  metrics: ScrollMetrics,
  thresholdPx = CHAT_SCROLL_THRESHOLD_PX
): ChatScrollState {
  const distance = distanceFromBottom(metrics);
  const autoScrollEnabled = distance <= thresholdPx;
  return {
    distanceFromBottom: distance,
    autoScrollEnabled,
    showJumpLatest: !autoScrollEnabled
  };
}

export function scrollElementToBottom(element: HTMLElement, behavior: ScrollBehavior = "auto") {
  element.scrollTo({ top: element.scrollHeight, behavior });
}

export function sessionIdOf(session: SessionLike) {
  return session.session_id || session.id || "";
}

export function sessionTimeMs(value: string | number | null | undefined) {
  if (typeof value === "number") {
    return value < 10_000_000_000 ? value * 1000 : value;
  }
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function findLatestSession<T extends SessionLike>(
  sessions: readonly T[],
  options: { excludeSessionId?: string } = {}
) {
  const ranked = sessions
    .map((session, index) => ({
      session,
      index,
      id: sessionIdOf(session),
      time: Math.max(sessionTimeMs(session.last_active), sessionTimeMs(session.updatedAt))
    }))
    .filter((entry) => entry.id && entry.id !== options.excludeSessionId);

  ranked.sort((a, b) => b.time - a.time || a.index - b.index);
  return ranked[0]?.session ?? null;
}

export function findLatestSessionId(
  sessions: readonly SessionLike[],
  options: { excludeSessionId?: string } = {}
) {
  const latest = findLatestSession(sessions, options);
  return latest ? sessionIdOf(latest) : "";
}
