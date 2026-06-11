/// <reference types="vite/client" />

interface Window {
  ecorexDesktop?: {
    platform: string;
    shouldUseDarkColors: boolean;
    getSidecarStatus: () => Promise<{
      state: "starting" | "running" | "stopped" | "failed" | "skipped";
      message: string;
      pid?: number;
      webPort: number;
    }>;
    listCapabilityPacks: () => Promise<
      Array<{
        id: string;
        name: string;
        summary: string;
        installMode: "user-or-admin" | "admin-recommended";
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
    installCapabilityPack: (packId: string) => Promise<{
      id: string;
      name: string;
      summary: string;
      installMode: "user-or-admin" | "admin-recommended";
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
    }>;
    getPermissionState: () => Promise<{
      mode: "smart-ask" | "always-ask" | "read-only" | "custom";
      grantsCount: number;
      auditPath: string;
      updatedAt?: string;
    }>;
    setPermissionMode: (mode: "smart-ask" | "always-ask" | "read-only" | "custom") => Promise<{
      mode: "smart-ask" | "always-ask" | "read-only" | "custom";
      grantsCount: number;
      auditPath: string;
      updatedAt?: string;
    }>;
    resetPermissionGrants: () => Promise<{
      mode: "smart-ask" | "always-ask" | "read-only" | "custom";
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
      Array<{ file_path: string; file_name: string; file_type: "image" | "video" | "file" }>
    >;
    savePastedFile: (input: { fileName?: string; mimeType?: string; dataBase64: string }) => Promise<{
      file_path: string;
      file_name: string;
      file_type: "image" | "video" | "file" | "directory";
    }>;
    openPath: (filePath: string) => Promise<string>;
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
