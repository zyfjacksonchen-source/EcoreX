import assert from "node:assert/strict";
import test from "node:test";

import {
  ArtifactPreviewCache,
  ArtifactPreviewLimitError,
} from "./artifactPreviewCache.ts";

function deferredBlob(size: number) {
  let resolve!: (blob: Blob) => void;
  const promise = new Promise<Blob>((accept) => {
    resolve = accept;
  });
  return { promise, resolve: () => resolve(new Blob([new Uint8Array(size)])) };
}

test("preview requests are deduplicated and bounded to the configured concurrency", async () => {
  const waiting = new Map<string, ReturnType<typeof deferredBlob>>();
  let active = 0;
  let peak = 0;
  const cache = new ArtifactPreviewCache({
    maxConcurrent: 2,
    fetchPreview: async (artifactId) => {
      active += 1;
      peak = Math.max(peak, active);
      const task = deferredBlob(4);
      waiting.set(artifactId, task);
      try {
        return await task.promise;
      } finally {
        active -= 1;
      }
    },
    createObjectUrl: (blob) => `blob:${blob.size}:${Math.random()}`,
    revokeObjectUrl: () => undefined,
  });

  const first = cache.ensure({ artifactId: "a", revisionId: "r1" });
  assert.equal(cache.ensure({ artifactId: "a", revisionId: "r1" }), first);
  const second = cache.ensure({ artifactId: "b", revisionId: "r1" });
  const third = cache.ensure({ artifactId: "c", revisionId: "r1" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual([...waiting.keys()].sort(), ["a", "b"]);
  waiting.get("a")?.resolve();
  await first;
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(waiting.has("c"));
  waiting.get("b")?.resolve();
  waiting.get("c")?.resolve();
  await Promise.all([second, third]);
  assert.equal(peak, 2);
  cache.dispose();
});

test("preview cache applies byte and entry LRU limits and revokes evicted URLs", async () => {
  let sequence = 0;
  const revoked: string[] = [];
  const cache = new ArtifactPreviewCache({
    maxEntries: 2,
    maxBytes: 8,
    maxConcurrent: 1,
    fetchPreview: async () => new Blob([new Uint8Array(4)]),
    createObjectUrl: () => `blob:${++sequence}`,
    revokeObjectUrl: (url) => revoked.push(url),
  });
  await cache.ensure({ artifactId: "a", revisionId: "r1" });
  await cache.ensure({ artifactId: "b", revisionId: "r1" });
  await cache.ensure({ artifactId: "a", revisionId: "r1" });
  await cache.ensure({ artifactId: "c", revisionId: "r1" });
  assert.deepEqual(Object.keys(cache.urls()).sort(), ["a", "c"]);
  assert.deepEqual(revoked, ["blob:2"]);
  cache.dispose();
  assert.deepEqual(revoked.sort(), ["blob:1", "blob:2", "blob:3"]);
});

test("revision reconciliation aborts stale work and oversized previews never enter memory", async () => {
  const slow = deferredBlob(4);
  const cache = new ArtifactPreviewCache({
    maxBytes: 4,
    maxConcurrent: 1,
    fetchPreview: async (artifactId, signal) => {
      if (artifactId === "slow") {
        signal.addEventListener("abort", () => slow.resolve(), { once: true });
        return slow.promise;
      }
      return new Blob([new Uint8Array(5)]);
    },
    createObjectUrl: () => "blob:never",
    revokeObjectUrl: () => undefined,
  });
  const stale = cache.ensure({ artifactId: "slow", revisionId: "r1" });
  cache.reconcile([{ artifactId: "slow", revisionId: "r2" }]);
  await assert.rejects(stale, (error: unknown) => (
    error instanceof DOMException && error.name === "AbortError"
  ));
  await assert.rejects(
    cache.ensure({ artifactId: "large", revisionId: "r1" }),
    ArtifactPreviewLimitError,
  );
  assert.deepEqual(cache.urls(), {});
  cache.dispose();
});
