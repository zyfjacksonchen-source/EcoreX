const http = require("node:http");
const { CompletionFacts, validThreadId } = require("./notification-contract.cjs");

const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

class RuntimeRequestError extends Error {
  constructor(statusCode) {
    super(`Runtime request failed (${statusCode}).`);
    this.statusCode = statusCode;
  }
}

function requestText(origin, requestPath, bearerToken = null) {
  return new Promise((resolve, reject) => {
    const target = new URL(requestPath, origin);
    if (target.protocol !== "http:" || target.hostname !== "127.0.0.1") {
      reject(new Error("Runtime notification URL is not loopback."));
      return;
    }
    const request = http.get({
      hostname: target.hostname,
      port: target.port,
      path: `${target.pathname}${target.search}`,
      timeout: 3_000,
      headers: bearerToken ? { Authorization: `Bearer ${bearerToken}` } : {},
    }, (response) => {
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > MAX_RESPONSE_BYTES) request.destroy(new Error("Runtime response is oversized."));
        else chunks.push(chunk);
      });
      response.once("end", () => {
        if (response.statusCode !== 200) {
          reject(new RuntimeRequestError(response.statusCode ?? 0));
          return;
        }
        resolve(Buffer.concat(chunks).toString("utf8"));
      });
    });
    request.once("timeout", () => request.destroy(new Error("Runtime request timed out.")));
    request.once("error", reject);
  });
}

async function requestJSON(origin, requestPath, bearerToken) {
  return JSON.parse(await requestText(origin, requestPath, bearerToken));
}

async function readRuntimeBearerToken(origin) {
  const index = await requestText(origin, "/");
  const prefix = "window.__ECOREX_RUNTIME__=Object.freeze(";
  const start = index.indexOf(prefix);
  const end = index.indexOf(");Object.defineProperty", start + prefix.length);
  if (start < 0 || end < 0) throw new Error("Runtime bridge is unavailable.");
  const bridge = JSON.parse(index.slice(start + prefix.length, end));
  if (
    bridge?.apiBase !== "/api/v1"
    || typeof bridge.bearerToken !== "string"
    || !/^[A-Za-z0-9_-]{32,256}$/.test(bridge.bearerToken)
  ) {
    throw new Error("Runtime bridge is invalid.");
  }
  return bridge.bearerToken;
}

class TaskNotificationMonitor {
  constructor({ origin, bearerToken, refreshBearerToken, NotificationClass, onOpenThread, interval = 2_000 }) {
    this.origin = origin;
    this.bearerToken = bearerToken;
    this.refreshBearerToken = refreshBearerToken;
    this.NotificationClass = NotificationClass;
    this.onOpenThread = onOpenThread;
    this.interval = interval;
    this.completions = new CompletionFacts();
    this.threads = new Map();
    this.liveNotifications = new Set();
    this.timer = null;
    this.running = false;
    this.stopped = true;
  }

  async start() {
    if (!this.stopped) return;
    this.stopped = false;
    await this.#tick();
    if (!this.stopped) this.timer = setInterval(() => void this.#tick(), this.interval);
    this.timer?.unref();
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    for (const notification of this.liveNotifications) notification.close();
    this.liveNotifications.clear();
  }

  async #tick() {
    if (this.stopped || this.running) return;
    this.running = true;
    try {
      const catalog = await this.#listThreads();
      for (const thread of catalog) await this.#syncThread(thread);
    } catch (error) {
      if (error instanceof RuntimeRequestError && error.statusCode === 401) {
        this.bearerToken = await this.refreshBearerToken().catch(() => this.bearerToken);
      }
    } finally {
      this.running = false;
    }
  }

  async #listThreads() {
    const items = [];
    let cursor = null;
    do {
      const query = new URLSearchParams({ status: "active", limit: "200" });
      if (cursor) query.set("cursor", cursor);
      const page = await requestJSON(this.origin, `/api/v1/threads?${query}`, this.bearerToken);
      if (!Array.isArray(page?.items) || (page.next_cursor !== null && typeof page.next_cursor !== "string")) {
        throw new Error("Runtime thread catalog is invalid.");
      }
      items.push(...page.items.filter((item) => validThreadId(item?.thread_id)));
      cursor = page.next_cursor;
    } while (cursor);
    return items;
  }

  async #syncThread(thread) {
    const existing = this.threads.get(thread.thread_id);
    const active = typeof thread.active_turn_status === "string";
    if (!existing) {
      const state = { active, cursor: null, title: thread.title, updatedAt: thread.updated_at };
      this.threads.set(thread.thread_id, state);
      if (active || Date.parse(thread.updated_at) >= this.completions.startedAt) {
        await this.#resyncProjection(thread.thread_id, state);
      }
      return;
    }
    const wasActive = existing.active;
    const changedWhileIdle = !active && !wasActive && existing.updatedAt !== thread.updated_at;
    existing.active = active;
    existing.title = thread.title;
    existing.updatedAt = thread.updated_at;
    if (changedWhileIdle || (active && existing.cursor === null)) {
      await this.#resyncProjection(thread.thread_id, existing);
    } else if (active || wasActive) await this.#pollEvents(thread.thread_id, existing);
  }

  async #resyncProjection(threadId, state) {
    const projection = await requestJSON(
      this.origin,
      `/api/v1/threads/${encodeURIComponent(threadId)}/projection`,
      this.bearerToken,
    );
    if (!Number.isSafeInteger(projection?.watermark) || projection.watermark < 0 || !Array.isArray(projection.turns)) {
      throw new Error("Runtime thread projection is invalid.");
    }
    state.cursor = projection.watermark;
    for (const turn of projection.turns) this.#notify(this.completions.turn(threadId, turn), state.title);
  }

  async #pollEvents(threadId, state) {
    for (let pageNumber = 0; pageNumber < 32; pageNumber += 1) {
      const page = await requestJSON(
        this.origin,
        `/api/v1/threads/${encodeURIComponent(threadId)}/events?after_seq=${state.cursor}&limit=1000`,
        this.bearerToken,
      );
      if (!Array.isArray(page?.events) || !Number.isSafeInteger(page.watermark) || page.watermark < state.cursor) {
        throw new Error("Runtime event page is invalid.");
      }
      for (const event of page.events) {
        if (!Number.isSafeInteger(event?.seq) || event.seq <= state.cursor || event.thread_id !== threadId) {
          throw new Error("Runtime event order is invalid.");
        }
        state.cursor = event.seq;
        this.#notify(this.completions.event(event), state.title);
      }
      if (!page.has_more) {
        state.cursor = Math.max(state.cursor, page.watermark);
        return;
      }
      if (!page.events.length) throw new Error("Runtime event page did not advance.");
    }
  }

  #notify(completion, _title) {
    if (!completion || this.stopped || !this.NotificationClass?.isSupported?.()) return;
    const notification = new this.NotificationClass({
      title: "e-Mate 任务已完成",
      body: "点击查看结果。",
    });
    this.liveNotifications.add(notification);
    const release = () => this.liveNotifications.delete(notification);
    notification.once("close", release);
    notification.once("failed", release);
    notification.once("click", () => {
      release();
      void this.onOpenThread(completion.threadId);
    });
    notification.show();
  }
}

module.exports = {
  readRuntimeBearerToken,
  RuntimeRequestError,
  TaskNotificationMonitor,
};
