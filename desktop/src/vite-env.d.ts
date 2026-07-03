/// <reference types="vite/client" />

interface Window {
  ecorexDesktop?: {
    platform: string;
    shouldUseDarkColors: boolean;
    setWindowTheme?: (theme: "light" | "dark") => Promise<unknown>;
    getSidecarStatus: () => Promise<{
      state: "starting" | "running" | "stopped" | "failed" | "skipped";
      phase?: "idle" | "spawning" | "probing" | "ready" | "degraded" | "restarting" | "failed" | "stopped" | "skipped";
      message: string;
      pid?: number;
      webPort: number;
      diagnostics?: {
        bootId: string;
        restartAttempts: number;
        consecutiveHealthFailures: number;
        startupInFlight: boolean;
        lastProbeOkAt?: string;
        lastProbeErrorAt?: string;
        recentEvents?: Array<{ ts: string; state: string; phase: string; message: string; reason?: string }>;
      };
    }>;
    listCapabilityPacks: () => Promise<
      Array<{
        id: string;
        name: string;
        summary: string;
        installMode: "user-or-admin" | "admin-recommended";
        discoveryOnly?: boolean;
        sourceUrl?: string;
        mirrorUrls?: string[];
        installHint?: string;
        estimatedSizeMb?: number;
        state: "installed" | "not-installed" | "checking" | "installing" | "busy" | "failed" | "unknown";
        message: string;
        installed: boolean;
        logPath?: string;
        missingModules?: string[];
        updatedAt?: string;
        policyMode?: "ask" | "preinstall" | "disabled";
        policyStatus?: string;
        policyUpdatedAt?: string;
      }>
    >;
    getPermissionState: () => Promise<{
      mode: "full-access" | "smart-ask" | "always-ask" | "read-only" | "custom";
      grantsCount: number;
      auditPath: string;
      updatedAt?: string;
    }>;
    setPermissionMode: (mode: "full-access" | "smart-ask" | "always-ask" | "read-only" | "custom") => Promise<{
      mode: "full-access" | "smart-ask" | "always-ask" | "read-only" | "custom";
      grantsCount: number;
      auditPath: string;
      updatedAt?: string;
    }>;
    resetPermissionGrants: () => Promise<{
      mode: "full-access" | "smart-ask" | "always-ask" | "read-only" | "custom";
      grantsCount: number;
      auditPath: string;
      updatedAt?: string;
    }>;
    getTelemetryState: () => Promise<{
      configured: boolean;
      eventsUrl: string;
      deviceId: string;
      userEmail?: string;
      orgId?: string;
      lastError?: string;
    }>;
    getEnterpriseSession: () => Promise<{
      expiresAt: string;
      deviceId: string;
      authenticated: true;
      user: {
        id: string;
        name: string;
        email: string;
        role: string;
        status: string;
        mustChangePassword?: boolean;
      };
      quota?: Record<string, unknown>;
    } | null>;
    enterpriseLogin: (input: { email: string; password: string }) => Promise<{
      expiresAt: string;
      deviceId: string;
      authenticated: true;
      user: {
        id: string;
        name: string;
        email: string;
        role: string;
        status: string;
        mustChangePassword?: boolean;
      };
      quota?: Record<string, unknown>;
    }>;
    enterpriseLogout: () => Promise<unknown>;
    enterpriseChangePassword: (input: { oldPassword: string; newPassword: string }) => Promise<{
      expiresAt: string;
      deviceId: string;
      authenticated: true;
      user: {
        id: string;
        name: string;
        email: string;
        role: string;
        status: string;
        mustChangePassword?: boolean;
      };
      quota?: Record<string, unknown>;
    }>;
    checkEnterpriseQuota: (estimatedTokens: number) => Promise<{
      ok: boolean;
      quota?: {
        allowed: boolean;
        reason?: string;
        dailyUsed?: number;
        weeklyUsed?: number;
        dailyLimit?: number;
        weeklyLimit?: number;
      };
    }>;
    refreshEnterprisePolicy: () => Promise<{
      configured: boolean;
      changed: boolean;
      restarted: boolean;
      message: string;
      model?: string;
      provider?: string;
      updatedAt?: string;
    }>;
    getEnterpriseModelConfig?: () => Promise<{
      ok?: boolean;
      configured?: boolean;
      provider?: string;
      model?: string;
      name?: string;
      updatedAt?: string;
      modelCredentials?: Array<{
        id?: string;
        name?: string;
        provider?: string;
        model?: string;
        enabled?: boolean;
      }>;
    }>;
    reportTelemetry: (event: {
      type: "usage" | "error" | "warn" | "info";
      source?: string;
      message?: string;
      category?: string;
      label?: string;
      amount?: number;
      sessionId?: string;
      tool?: string;
      detail?: Record<string, unknown>;
    }) => Promise<unknown>;
    chooseFiles: () => Promise<
      Array<{ file_path: string; file_name: string; file_type: "image" | "video" | "audio" | "file" }>
    >;
    chooseProjectFolder: () => Promise<{
      id: string;
      name: string;
      path: string;
      memoryPath?: string;
      dreamsPath?: string;
      updatedAt: string;
    } | null>;
    savePastedFile: (input: { fileName?: string; mimeType?: string; dataBase64: string }) => Promise<{
      file_path: string;
      file_name: string;
      file_type: "image" | "video" | "audio" | "file" | "directory";
    }>;
    statPath?: (filePath: string) => Promise<{
      status?: string;
      message?: string;
      path: string;
      exists: boolean;
      isFile?: boolean;
      isDirectory?: boolean;
      mimeType?: string;
      sizeBytes?: number;
    }>;
    openPath: (filePath: string, action?: "open" | "reveal" | "openWith") => Promise<string>;
    apiJson: (request: {
      path: string;
      method?: "GET" | "POST" | "PUT" | "DELETE";
      body?: unknown;
    }) => Promise<unknown>;
    onSidecarStatus: (
      listener: (status: {
        state: "starting" | "running" | "stopped" | "failed" | "skipped";
        message: string;
        pid?: number;
        webPort: number;
      }) => void
    ) => () => void;
  };
}
