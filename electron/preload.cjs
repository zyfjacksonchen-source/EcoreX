const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ecorex', {
  getBackendStatus: () => ipcRenderer.invoke('backend:status'),
  getCapabilities: () => ipcRenderer.invoke('backend:capabilities'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  updateSettings: (payload) => ipcRenderer.invoke('settings:update', payload),
  getDiagnostics: () => ipcRenderer.invoke('diagnostics:get'),
  listWorkspace: (payload) => ipcRenderer.invoke('workspace:list', payload),
  ensureWorkspace: (payload) => ipcRenderer.invoke('workspace:ensure', payload),
  openAuth: () => ipcRenderer.invoke('backend:open-auth'),
  runPrompt: (payload) => ipcRenderer.invoke('agent:run', payload),
  stopPrompt: (sessionId) => ipcRenderer.invoke('agent:stop', sessionId),
  getAgentSessions: () => ipcRenderer.invoke('agent:sessions'),
  onAgentEvent: (handler) => {
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on('agent:event', listener);
    return () => ipcRenderer.removeListener('agent:event', listener);
  },
  platform: process.platform,
  windowControl: (action) => ipcRenderer.send('window:control', action)
});
