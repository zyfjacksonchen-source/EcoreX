export interface ArtifactPreviewIdentity {
  artifactId: string;
  revisionId: string;
}

interface PreviewRecord {
  revisionId: string;
  url: string;
  bytes: number;
  lastAccess: number;
}

interface PendingPreview {
  artifactId: string;
  revisionId: string;
  controller: AbortController;
  promise: Promise<string>;
  resolve: (url: string) => void;
  reject: (error: unknown) => void;
  started: boolean;
}

interface ArtifactPreviewCacheOptions {
  fetchPreview: (artifactId: string, signal: AbortSignal) => Promise<Blob>;
  createObjectUrl?: (blob: Blob) => string;
  revokeObjectUrl?: (url: string) => void;
  onChange?: (urls: Record<string, string>) => void;
  maxEntries?: number;
  maxBytes?: number;
  maxConcurrent?: number;
}

const DEFAULT_MAX_ENTRIES = 24;
export const ARTIFACT_PREVIEW_MAX_BYTES = 64 * 1024 * 1024;
const DEFAULT_MAX_CONCURRENT = 4;

function abortError(): DOMException {
  return new DOMException("Preview request was cancelled", "AbortError");
}

export class ArtifactPreviewLimitError extends Error {
  constructor() {
    super("Artifact preview exceeds the bounded in-memory cache");
    this.name = "ArtifactPreviewLimitError";
  }
}

export class ArtifactPreviewCache {
  readonly maxEntries: number;
  readonly maxBytes: number;
  readonly maxConcurrent: number;

  private readonly fetchPreview: ArtifactPreviewCacheOptions["fetchPreview"];
  private readonly createObjectUrl: (blob: Blob) => string;
  private readonly revokeObjectUrl: (url: string) => void;
  private readonly onChange: (urls: Record<string, string>) => void;
  private readonly records = new Map<string, PreviewRecord>();
  private readonly pending = new Map<string, PendingPreview>();
  private queue: PendingPreview[] = [];
  private active = 0;
  private clock = 0;
  private disposed = false;

  constructor(options: ArtifactPreviewCacheOptions) {
    this.fetchPreview = options.fetchPreview;
    this.createObjectUrl = options.createObjectUrl ?? URL.createObjectURL.bind(URL);
    this.revokeObjectUrl = options.revokeObjectUrl ?? URL.revokeObjectURL.bind(URL);
    this.onChange = options.onChange ?? (() => undefined);
    this.maxEntries = options.maxEntries ?? DEFAULT_MAX_ENTRIES;
    this.maxBytes = options.maxBytes ?? ARTIFACT_PREVIEW_MAX_BYTES;
    this.maxConcurrent = options.maxConcurrent ?? DEFAULT_MAX_CONCURRENT;
    if (
      !Number.isSafeInteger(this.maxEntries)
      || this.maxEntries < 1
      || !Number.isSafeInteger(this.maxBytes)
      || this.maxBytes < 1
      || !Number.isSafeInteger(this.maxConcurrent)
      || this.maxConcurrent < 1
    ) {
      throw new TypeError("Artifact preview cache limits must be positive integers");
    }
  }

  urls(): Record<string, string> {
    return Object.fromEntries(
      [...this.records].map(([artifactId, record]) => [artifactId, record.url]),
    );
  }

  ensure(identity: ArtifactPreviewIdentity): Promise<string> {
    if (this.disposed) return Promise.reject(abortError());
    const current = this.records.get(identity.artifactId);
    if (current?.revisionId === identity.revisionId) {
      current.lastAccess = ++this.clock;
      return Promise.resolve(current.url);
    }
    if (current) this.removeRecord(identity.artifactId);

    const existing = this.pending.get(identity.artifactId);
    if (existing?.revisionId === identity.revisionId) return existing.promise;
    if (existing) this.cancel(existing);

    let resolve!: (url: string) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<string>((accept, decline) => {
      resolve = accept;
      reject = decline;
    });
    const request: PendingPreview = {
      artifactId: identity.artifactId,
      revisionId: identity.revisionId,
      controller: new AbortController(),
      promise,
      resolve,
      reject,
      started: false,
    };
    this.pending.set(identity.artifactId, request);
    this.queue.push(request);
    this.pump();
    return promise;
  }

  reconcile(identities: readonly ArtifactPreviewIdentity[]): void {
    const desired = new Map(identities.map((item) => [item.artifactId, item.revisionId]));
    let changed = false;
    for (const [artifactId, record] of this.records) {
      if (desired.get(artifactId) !== record.revisionId) {
        this.removeRecord(artifactId);
        changed = true;
      }
    }
    for (const request of this.pending.values()) {
      if (desired.get(request.artifactId) !== request.revisionId) this.cancel(request);
    }
    if (changed) this.publish();
  }

  clear(): void {
    for (const request of [...this.pending.values()]) this.cancel(request);
    this.queue = [];
    for (const artifactId of [...this.records.keys()]) this.removeRecord(artifactId);
    this.publish();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.clear();
  }

  private cancel(request: PendingPreview): void {
    request.controller.abort();
    if (!request.started && this.pending.get(request.artifactId) === request) {
      this.pending.delete(request.artifactId);
      request.reject(abortError());
    }
  }

  private pump(): void {
    while (!this.disposed && this.active < this.maxConcurrent && this.queue.length > 0) {
      const request = this.queue.shift();
      if (!request || this.pending.get(request.artifactId) !== request) continue;
      if (request.controller.signal.aborted) {
        this.pending.delete(request.artifactId);
        request.reject(abortError());
        continue;
      }
      request.started = true;
      this.active += 1;
      void this.run(request);
    }
  }

  private async run(request: PendingPreview): Promise<void> {
    try {
      const blob = await this.fetchPreview(request.artifactId, request.controller.signal);
      if (
        request.controller.signal.aborted
        || this.pending.get(request.artifactId) !== request
      ) {
        throw abortError();
      }
      if (blob.size > this.maxBytes) throw new ArtifactPreviewLimitError();
      const url = this.createObjectUrl(blob);
      if (
        request.controller.signal.aborted
        || this.pending.get(request.artifactId) !== request
      ) {
        this.revokeObjectUrl(url);
        throw abortError();
      }
      this.removeRecord(request.artifactId);
      this.records.set(request.artifactId, {
        revisionId: request.revisionId,
        url,
        bytes: blob.size,
        lastAccess: ++this.clock,
      });
      this.evict(request.artifactId);
      this.publish();
      request.resolve(url);
    } catch (error) {
      request.reject(error);
    } finally {
      if (this.pending.get(request.artifactId) === request) {
        this.pending.delete(request.artifactId);
      }
      this.active -= 1;
      this.pump();
    }
  }

  private evict(protectedArtifactId: string): void {
    const totalBytes = () => [...this.records.values()].reduce(
      (sum, record) => sum + record.bytes,
      0,
    );
    while (this.records.size > this.maxEntries || totalBytes() > this.maxBytes) {
      const candidate = [...this.records]
        .filter(([artifactId]) => artifactId !== protectedArtifactId)
        .sort((left, right) => left[1].lastAccess - right[1].lastAccess)[0];
      if (!candidate) break;
      this.removeRecord(candidate[0]);
    }
  }

  private removeRecord(artifactId: string): void {
    const record = this.records.get(artifactId);
    if (!record) return;
    this.records.delete(artifactId);
    this.revokeObjectUrl(record.url);
  }

  private publish(): void {
    this.onChange(this.urls());
  }
}
