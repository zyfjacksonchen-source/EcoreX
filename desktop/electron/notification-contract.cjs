const THREAD_ID = /^thr_[A-Za-z0-9._:-]{1,252}$/;
const TURN_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

function validThreadId(value) {
  return typeof value === "string" && THREAD_ID.test(value);
}

function completionKey(threadId, turnId) {
  return validThreadId(threadId) && typeof turnId === "string" && TURN_ID.test(turnId)
    ? `${threadId}:${turnId}:completed`
    : null;
}

class CompletionFacts {
  constructor(startedAt = Date.now()) {
    this.startedAt = startedAt;
    this.seen = new Set();
  }

  #accept(threadId, turnId, status, createdAt) {
    const key = status === "completed" ? completionKey(threadId, turnId) : null;
    const timestamp = Date.parse(createdAt);
    if (!key || !Number.isFinite(timestamp) || timestamp < this.startedAt || this.seen.has(key)) {
      return null;
    }
    this.seen.add(key);
    return { key, threadId, turnId };
  }

  event(value) {
    if (!value || value.schema_version !== 1 || value.event_type !== "turn.status_changed") return null;
    return this.#accept(
      value.thread_id,
      value.turn_id,
      value.payload?.to,
      value.created_at,
    );
  }

  turn(threadId, value) {
    if (!value || value.thread_id !== threadId) return null;
    return this.#accept(threadId, value.turn_id, value.status, value.updated_at);
  }
}

module.exports = { CompletionFacts, completionKey, validThreadId };
