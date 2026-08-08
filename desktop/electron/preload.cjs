const { contextBridge, ipcRenderer } = require("electron");
const { validThreadId } = require("./notification-contract.cjs");

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
