const { contextBridge, ipcRenderer } = require('electron');

let authToken = '';

function publicError(error) {
  return error instanceof Error ? error.message : String(error || 'IPC request failed.');
}

function safeInvoke(channel, payload) {
  return ipcRenderer.invoke(channel, payload).catch((error) => ({
    ok: false,
    error: publicError(error),
    channel
  }));
}

function rememberAuth(result) {
  if (result?.ok && result?.loggedIn && typeof result.token === 'string') {
    authToken = result.token;
  }
  if (result?.ok && result?.loggedIn === false) {
    authToken = '';
  }
  if (!result || typeof result !== 'object') return result;
  const { token, authToken: _authToken, sessionToken: _sessionToken, ...safeResult } = result;
  return safeResult;
}

function withAuth(payload = {}) {
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    return { ...payload, authToken };
  }
  return { value: payload, authToken };
}

contextBridge.exposeInMainWorld('ecorex', {
  authLogin: (payload) => safeInvoke('auth:login', payload).then(rememberAuth),
  authLogout: () => safeInvoke('auth:logout', { authToken }).then(rememberAuth),
  getAuthStatus: () => safeInvoke('auth:status', { includeToken: !authToken, refresh: true }).then(rememberAuth),
  listUsers: (payload) => safeInvoke('auth:users:list', withAuth(payload)),
  createUser: (payload) => safeInvoke('auth:user:create', withAuth(payload)),
  updateUser: (payload) => safeInvoke('auth:user:update', withAuth(payload)),
  deleteUser: (payload) => safeInvoke('auth:user:delete', withAuth(typeof payload === 'string' ? { id: payload } : payload)),
  updateProfile: (payload) => safeInvoke('auth:profile:update', withAuth(payload)).then((result) => {
    if (result?.auth) rememberAuth(result.auth);
    return result;
  }),
  runEnterpriseAction: (payload) => safeInvoke('enterprise:action', withAuth(payload)),
  getBackendStatus: (payload) => safeInvoke('backend:status', payload),
  getCapabilities: () => safeInvoke('backend:capabilities'),
  getPermissionModes: () => safeInvoke('backend:capabilities').then((result) => result?.permissionModes || []),
  getSecretsStatus: () => safeInvoke('secrets:status', { authToken }),
  listSecrets: (payload) => safeInvoke('secrets:list', withAuth(payload)),
  setSecret: (payload) => safeInvoke('secrets:set', withAuth(payload)),
  deleteSecret: (payload) => safeInvoke('secrets:delete', withAuth(typeof payload === 'string' ? { key: payload } : payload)),
  listModelProfiles: (payload) => ipcRenderer.invoke('listModelProfiles', withAuth(payload)).catch((error) => ({ ok: false, error: publicError(error), channel: 'listModelProfiles' })),
  saveModelProfile: (payload) => ipcRenderer.invoke('saveModelProfile', withAuth(payload)).catch((error) => ({ ok: false, error: publicError(error), channel: 'saveModelProfile' })),
  deleteModelProfile: (payload) =>
    ipcRenderer.invoke('deleteModelProfile', withAuth(typeof payload === 'string' ? { name: payload } : payload)).catch((error) => ({ ok: false, error: publicError(error), channel: 'deleteModelProfile' })),
  activateModelProfile: (payload) =>
    ipcRenderer.invoke('activateModelProfile', withAuth(typeof payload === 'string' ? { name: payload } : payload)).catch((error) => ({ ok: false, error: publicError(error), channel: 'activateModelProfile' })),
  testModelProfile: (payload) => ipcRenderer.invoke('testModelProfile', withAuth(payload)).catch((error) => ({ ok: false, error: publicError(error), channel: 'testModelProfile' })),
  testModelAdapterProfile: (payload) => ipcRenderer.invoke('modelAdapter:testProfile', withAuth(payload)).catch((error) => ({ ok: false, error: publicError(error), channel: 'modelAdapter:testProfile' })),
  generateModelImage: (payload) => ipcRenderer.invoke('modelAdapter:generateImage', withAuth(payload)).catch((error) => ({ ok: false, error: publicError(error), channel: 'modelAdapter:generateImage' })),
  getSettings: () => safeInvoke('settings:get', { authToken }),
  updateSettings: (payload) => safeInvoke('settings:update', withAuth(payload)),
  listEvaluations: (payload) => safeInvoke('evaluation:list', withAuth(payload)),
  runEvaluations: (payload) => safeInvoke('evaluation:run', withAuth(payload)),
  getStartupHealth: (payload) => safeInvoke('startup:health', withAuth(payload)),
  getDiagnostics: () => safeInvoke('diagnostics:get', { authToken }),
  exportDiagnosticsPackage: (payload) => safeInvoke('diagnostics:export', withAuth(payload)),
  exportDiagnosticsBundle: (payload) => safeInvoke('diagnostics:export', withAuth(payload)),
  openDiagnosticsLocation: (payload) => safeInvoke('diagnostics:open-location', withAuth(payload)),
  openDiagnosticsFolder: (payload) => safeInvoke('diagnostics:open-location', withAuth(payload)),
  getCrashRecoveryStatus: (payload) => safeInvoke('diagnostics:crash-recovery', withAuth(payload)),
  getTelemetryStatus: (payload) => safeInvoke('telemetry:status', withAuth(payload)),
  flushTelemetry: (payload) => safeInvoke('telemetry:flush', withAuth(payload)),
  listWorkspace: (payload) => safeInvoke('workspace:list', withAuth(payload)),
  ensureWorkspace: (payload) => safeInvoke('workspace:ensure', withAuth(payload)),
  selectWorkspaceDirectory: (payload) => safeInvoke('workspace:select-directory', withAuth(payload)),
  previewFile: (payload) => safeInvoke('file:preview', withAuth(typeof payload === 'string' ? { path: payload } : payload)),
  openExternalUrl: (payload) => safeInvoke('shell:open-external', withAuth(typeof payload === 'string' ? { url: payload } : payload)),
  ingestAttachments: (payload) => safeInvoke('attachment:ingest', withAuth(payload)),
  selectAttachmentFiles: (payload) => safeInvoke('attachment:select-files', withAuth(payload)),
  openAttachmentFile: (payload) => safeInvoke('attachment:open-file', withAuth(typeof payload === 'string' ? { path: payload } : payload)),
  openArtifactFile: (payload) => safeInvoke('artifact:open-file', withAuth(typeof payload === 'string' ? { path: payload } : payload)),
  openGeneratedFile: (payload) => safeInvoke('artifact:open-file', withAuth(typeof payload === 'string' ? { path: payload } : payload)),
  listProjects: (payload) => safeInvoke('project:list', withAuth(payload)),
  createProject: (payload) => safeInvoke('project:create', withAuth(payload)),
  switchProject: (payload) => safeInvoke('project:switch', withAuth(typeof payload === 'string' ? { id: payload } : payload)),
  updateProject: (payload) => safeInvoke('project:update', withAuth(payload)),
  archiveProject: (payload) => safeInvoke('project:archive', withAuth(typeof payload === 'string' ? { id: payload } : payload)),
  deleteProject: (payload) => safeInvoke('project:delete', withAuth(typeof payload === 'string' ? { id: payload, confirmDelete: true } : payload)),
  getProjectStatus: (payload) => safeInvoke('project:status', withAuth(payload)),
  openProjectFolder: (payload) => safeInvoke('project:open-folder', withAuth(typeof payload === 'string' ? { id: payload } : payload)),
  listDataConnections: (payload) => safeInvoke('mcp:list', withAuth(payload)),
  getDataConnectionStatus: (payload) => safeInvoke('mcp:status', withAuth(payload)),
  refreshDataConnections: (payload) => safeInvoke('mcp:refresh', withAuth(payload)),
  getDataConnection: (payload) => safeInvoke('mcp:get', withAuth(payload)),
  updateDataConnection: (payload) => safeInvoke('mcp:update-config', withAuth(payload)),
  enableDataConnection: (payload) => safeInvoke('mcp:enable', withAuth(payload)),
  disableDataConnection: (payload) => safeInvoke('mcp:disable', withAuth(payload)),
  listMcpServers: (payload) => safeInvoke('mcp:list', withAuth(payload)),
  getMcpStatus: (payload) => safeInvoke('mcp:status', withAuth(payload)),
  refreshMcpStatus: (payload) => safeInvoke('mcp:refresh', withAuth(payload)),
  getMcpServer: (payload) => safeInvoke('mcp:get', withAuth(payload)),
  updateMcpConfig: (payload) => safeInvoke('mcp:update-config', withAuth(payload)),
  enableMcpServer: (payload) => safeInvoke('mcp:enable', withAuth(payload)),
  disableMcpServer: (payload) => safeInvoke('mcp:disable', withAuth(payload)),
  listSkills: (payload) => safeInvoke('skill:list', withAuth(payload)),
  getSkillStatus: (payload) => safeInvoke('skill:status', withAuth(payload)),
  refreshSkillStatus: (payload) => safeInvoke('skill:refresh', withAuth(payload)),
  installSkill: (payload) => safeInvoke('skill:install', withAuth(payload)),
  enableSkill: (payload) => safeInvoke('skill:enable', withAuth(payload)),
  disableSkill: (payload) => safeInvoke('skill:disable', withAuth(payload)),
  updateSkill: (payload) => safeInvoke('skill:update', withAuth(payload)),
  openAuth: () => safeInvoke('backend:open-auth', { authToken }),
  runPrompt: (payload) => safeInvoke('agent:run', withAuth(payload)),
  stopPrompt: (payload) => safeInvoke('agent:stop', withAuth(typeof payload === 'string' ? { sessionId: payload } : payload)),
  getAgentSessions: (payload) => safeInvoke('agent:sessions', withAuth(payload)),
  getAgentSessionHistory: (payload) => safeInvoke('agent:session-history', withAuth(payload)),
  onAgentEvents: (handler) => {
    if (typeof handler !== 'function') return () => {};
    const listener = (_event, payload) => {
      const events = Array.isArray(payload?.events) ? payload.events : Array.isArray(payload) ? payload : [];
      try {
        handler({
          ...(payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : {}),
          events
        });
      } catch {
        // Renderer callbacks must not break the preload bridge.
      }
    };
    ipcRenderer.on('agent:events', listener);
    return () => ipcRenderer.removeListener('agent:events', listener);
  },
  onAgentEvent: (handler) => {
    if (typeof handler !== 'function') return () => {};
    const batchListener = (_event, payload) => {
      const events = Array.isArray(payload?.events) ? payload.events : Array.isArray(payload) ? payload : [];
      for (const event of events) {
        try {
          handler(event);
        } catch {
          // Keep later streamed events flowing even if one callback fails.
        }
      }
    };
    const singleListener = (_event, payload) => {
      try {
        handler(payload);
      } catch {
        // Keep the bridge stable.
      }
    };
    ipcRenderer.on('agent:events', batchListener);
    ipcRenderer.on('agent:event', singleListener);
    return () => {
      ipcRenderer.removeListener('agent:events', batchListener);
      ipcRenderer.removeListener('agent:event', singleListener);
    };
  },
  platform: process.platform,
  windowControl: (action) => ipcRenderer.send('window:control', withAuth({ action }))
});
