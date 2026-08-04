interface BootstrapVersionProjection {
  update: { current_version: string };
}

interface UpdateHandoffOptions {
  readBootstrap: () => Promise<BootstrapVersionProjection>;
  targetVersion: string;
  initialDelayMs: number;
  currentUrl: string;
  replace: (url: string) => void;
  timeoutMs?: number;
  pollIntervalMs?: number;
}

const delay = (milliseconds: number) => new Promise<void>((resolve) => {
  globalThis.setTimeout(resolve, milliseconds);
});

export async function handOffToUpdatedRuntime(options: UpdateHandoffOptions): Promise<boolean> {
  await delay(options.initialDelayMs);
  const deadline = Date.now() + (options.timeoutMs ?? 90_000);
  let ready = false;
  while (Date.now() < deadline) {
    try {
      const bootstrap = await options.readBootstrap();
      if (bootstrap.update.current_version === options.targetVersion) {
        ready = true;
        break;
      }
    } catch (error) {
      if (typeof error === "object" && error !== null && "status" in error && error.status === 401) {
        // The restarted local Runtime rotates its injected bearer. Replacing
        // the document obtains the new bridge and completes target validation.
        ready = true;
        break;
      }
    }
    await delay(options.pollIntervalMs ?? 750);
  }
  const next = new URL(options.currentUrl);
  next.searchParams.set("emate_updated", options.targetVersion || "latest");
  // Replace the old document in-place so Back cannot reopen a stale WebUI.
  options.replace(next.toString());
  return ready;
}
