const { contextBridge, ipcRenderer } = require("electron");

const THREAD_ID = /^thr_[A-Za-z0-9._:-]{1,252}$/;
const validThreadId = (value) => typeof value === "string" && THREAD_ID.test(value);

let openThreadCallback = null;
let pendingThreadId = null;
ipcRenderer.on("emate:open-thread", (_event, threadId) => {
  if (!validThreadId(threadId)) return;
  if (openThreadCallback) openThreadCallback(threadId);
  else pendingThreadId = threadId;
});

let copyArtifactPathCallback = null;
ipcRenderer.on("emate:copy-artifact-path", (_event, target) => {
  const artifactId = typeof target?.artifactId === "string" ? target.artifactId : "";
  const revisionId = typeof target?.revisionId === "string" ? target.revisionId : "";
  const displayName = typeof target?.displayName === "string" ? target.displayName : "";
  if (artifactId && revisionId && displayName && copyArtifactPathCallback) {
    copyArtifactPathCallback({ artifactId, revisionId, displayName });
  }
});

window.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.desktopPlatform = process.platform;
});

window.addEventListener("contextmenu", (event) => {
  const element = event.target instanceof Element ? event.target : null;
  const artifact = element?.closest("[data-emate-artifact-id]");
  ipcRenderer.send("emate:context-target", artifact ? {
    artifactId: artifact.getAttribute("data-emate-artifact-id"),
    revisionId: artifact.getAttribute("data-emate-artifact-revision"),
    displayName: artifact.getAttribute("data-emate-artifact-name"),
  } : null);
}, true);

contextBridge.exposeInMainWorld("eMateDesktop", {
  platform: process.platform,
  version: () => ipcRenderer.invoke("emate:version"),
  restartRuntime: () => ipcRenderer.invoke("emate:restart-runtime"),
  checkForUpdates: () => ipcRenderer.invoke("emate:check-for-updates"),
  downloadDesktopUpdate: () => ipcRenderer.invoke("emate:download-update"),
  installDesktopUpdate: () => ipcRenderer.invoke("emate:install-update"),
  desktopUpdateStatus: () => ipcRenderer.invoke("emate:desktop-update-status"),
  copyMaterializedPath: (receipt) => ipcRenderer.invoke("emate:copy-materialized-path", receipt),
  onCopyArtifactPath: (callback) => {
    if (typeof callback !== "function") return () => {};
    copyArtifactPathCallback = callback;
    return () => {
      if (copyArtifactPathCallback === callback) copyArtifactPathCallback = null;
    };
  },
  onDesktopUpdateStatus: (callback) => {
    if (typeof callback !== "function") return () => {};
    const listener = (_event, status) => callback(status);
    ipcRenderer.on("emate:update-status", listener);
    return () => ipcRenderer.removeListener("emate:update-status", listener);
  },
  onOpenThread: (callback) => {
    if (typeof callback !== "function") return () => {};
    openThreadCallback = callback;
    if (pendingThreadId) {
      const threadId = pendingThreadId;
      pendingThreadId = null;
      callback(threadId);
    }
    return () => {
      if (openThreadCallback === callback) openThreadCallback = null;
    };
  },
});
