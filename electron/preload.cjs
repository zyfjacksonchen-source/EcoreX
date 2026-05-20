const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ecorex', {
  getBackendStatus: () => ipcRenderer.invoke('backend:status'),
  getCapabilities: () => ipcRenderer.invoke('backend:capabilities'),
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
