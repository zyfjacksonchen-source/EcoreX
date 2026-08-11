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

contextBridge.exposeInMainWorld("eMateDesktop", {
  platform: process.platform,
  version: () => ipcRenderer.invoke("emate:version"),
  restartRuntime: () => ipcRenderer.invoke("emate:restart-runtime"),
  checkForUpdates: () => ipcRenderer.invoke("emate:check-for-updates"),
  openUpdatePage: () => ipcRenderer.invoke("emate:open-update-page"),
  downloadDesktopUpdate: () => ipcRenderer.invoke("emate:download-update"),
  installDesktopUpdate: () => ipcRenderer.invoke("emate:install-update"),
  desktopUpdateStatus: () => ipcRenderer.invoke("emate:desktop-update-status"),
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
