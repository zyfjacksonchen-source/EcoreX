import { useCallback, useEffect, useRef, useState } from "react";

import type {
  BootstrapResponse,
  ExtensionActionId,
  ExtensionCatalogSnapshot,
  ExtensionProjection,
  MCPOAuthStatusProjection,
  SkillHubCardProjection,
  SkillHubDetailProjection,
} from "../api/contracts.ts";
import { safeConnectorAuthorizationUrl } from "./connectors.ts";
import {
  createClientRequestId,
  RuntimeApiError,
  RuntimeClient,
} from "../api/runtimeClient.ts";
import {
  extensionAction,
  extensionActionDisabledReason,
  extensionRequestKey,
  type ExtensionLoadState,
  type ExtensionOperationState,
} from "./extensions.ts";

interface UseExtensionSessionOptions {
  client: RuntimeClient;
  bootstrap: BootstrapResponse | null;
  formatError: (error: unknown) => string;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function readFileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("无法读取技能安装包。"));
    reader.onload = () => {
      const value = reader.result;
      if (typeof value !== "string" || !value.startsWith("data:")) {
        reject(new Error("技能安装包的读取结果无效。"));
        return;
      }
      const separator = value.indexOf(",");
      if (separator < 0) {
        reject(new Error("技能安装包的读取结果无效。"));
        return;
      }
      resolve(value.slice(separator + 1));
    };
    reader.readAsDataURL(file);
  });
}

export function useExtensionSession({
  client,
  bootstrap,
  formatError,
}: UseExtensionSessionOptions) {
  const [extensionSnapshot, setExtensionSnapshot] = useState<ExtensionCatalogSnapshot | null>(
    bootstrap?.extensions ?? null,
  );
  const [extensionCatalogState, setExtensionCatalogState] = useState<ExtensionLoadState>(
    bootstrap?.extensions ? "ready" : "idle",
  );
  const [extensionError, setExtensionError] = useState<string | null>(null);
  const [extensionInstallBusy, setExtensionInstallBusy] = useState(false);
  const [mcpOAuthStatuses, setMcpOAuthStatuses] = useState<Record<string, MCPOAuthStatusProjection>>({});
  const [mcpOAuthBusy, setMcpOAuthBusy] = useState<string | null>(null);
  const [skillHubItems, setSkillHubItems] = useState<SkillHubCardProjection[]>([]);
  const [skillHubState, setSkillHubState] = useState<ExtensionLoadState>("idle");
  const [skillHubError, setSkillHubError] = useState<string | null>(null);
  const [skillHubInstallingSlug, setSkillHubInstallingSlug] = useState<string | null>(null);
  const [skillHubDownloadingSlug, setSkillHubDownloadingSlug] = useState<string | null>(null);
  const [skillHubDetail, setSkillHubDetail] = useState<SkillHubDetailProjection | null>(null);
  const [skillHubDetailLoadingSlug, setSkillHubDetailLoadingSlug] = useState<string | null>(null);
  const [skillHubUploadBusy, setSkillHubUploadBusy] = useState(false);
  const [extensionOperations, setExtensionOperations] = useState<
    Record<string, ExtensionOperationState>
  >({});
  const catalogStateRef = useRef<ExtensionLoadState>(
    bootstrap?.extensions ? "ready" : "idle",
  );
  const catalogGeneration = useRef(0);
  const catalogAuthority = useRef<"bootstrap" | "extensions">("bootstrap");
  const lastBootstrapSnapshotId = useRef<string | null>(null);
  const lastSessionRevision = useRef<number | null>(bootstrap?.login.session_revision ?? null);
  const operationLocks = useRef(new Set<string>());
  const hubInstallLock = useRef(false);
  const hubUploadLock = useRef(false);
  const requestIds = useRef(new Map<string, string>());

  const transitionCatalogState = useCallback((state: ExtensionLoadState) => {
    catalogStateRef.current = state;
    setExtensionCatalogState(state);
  }, []);

  const acceptCatalog = useCallback((snapshot: ExtensionCatalogSnapshot) => {
    setExtensionSnapshot(snapshot);
    transitionCatalogState("ready");
    const validRequestKeys = new Set(snapshot.items.flatMap((extension) => (
      extension.actions.map((action) => extensionRequestKey(extension, action.action_id))
    )));
    for (const key of requestIds.current.keys()) {
      if (!validRequestKeys.has(key)) requestIds.current.delete(key);
    }
  }, [transitionCatalogState]);

  useEffect(() => {
    const incoming = bootstrap?.extensions;
    const sessionRevision = bootstrap?.login.session_revision ?? null;
    if (sessionRevision !== lastSessionRevision.current) {
      lastSessionRevision.current = sessionRevision;
      catalogAuthority.current = "bootstrap";
      catalogGeneration.current += 1;
      requestIds.current.clear();
    }
    if (!incoming || incoming.snapshot_id === lastBootstrapSnapshotId.current) return;
    lastBootstrapSnapshotId.current = incoming.snapshot_id;
    if (catalogAuthority.current !== "bootstrap" || operationLocks.current.size) return;
    acceptCatalog(incoming);
    setExtensionError(null);
  }, [acceptCatalog, bootstrap?.extensions, bootstrap?.login.session_revision]);

  const refreshExtensions = useCallback(async (
    signal?: AbortSignal,
  ): Promise<ExtensionCatalogSnapshot | null> => {
    if (operationLocks.current.size) return null;
    const generation = ++catalogGeneration.current;
    catalogAuthority.current = "extensions";
    transitionCatalogState("loading");
    try {
      const snapshot = await client.extensionCatalog(signal);
      if (signal?.aborted || generation !== catalogGeneration.current) return null;
      acceptCatalog(snapshot);
      setExtensionError(null);
      return snapshot;
    } catch (error) {
      if (isAbortError(error)) return null;
      if (generation !== catalogGeneration.current) return null;
      transitionCatalogState("error");
      setExtensionError(
        `扩展目录加载失败：${formatError(error)} 请稍后重试。`,
      );
      return null;
    }
  }, [acceptCatalog, client, formatError, transitionCatalogState]);

  const mutateExtension = useCallback(async (
    extension: ExtensionProjection,
    actionId: ExtensionActionId,
    configuration?: Record<string, string>,
  ): Promise<boolean> => {
    if (catalogStateRef.current !== "ready") {
      setExtensionError("扩展目录未验证，请刷新后再操作。");
      return false;
    }
    const projectedAction = extensionAction(extension, actionId);
    if (!projectedAction) {
      setExtensionError("此扩展未提供该操作，页面不会自行添加。");
      return false;
    }
    const disabledReason = extensionActionDisabledReason(projectedAction);
    if (disabledReason) {
      setExtensionError(disabledReason);
      return false;
    }
    if (operationLocks.current.size) return false;

    const requestKey = extensionRequestKey(extension, actionId);
    const clientRequestId = requestIds.current.get(requestKey)
      ?? createClientRequestId(`extension_${actionId}`);
    requestIds.current.set(requestKey, clientRequestId);
    const operation: ExtensionOperationState = {
      extensionId: extension.extension_id,
      actionId,
      expectedRevision: extension.revision,
      clientRequestId,
    };
    operationLocks.current.add(extension.extension_id);
    catalogAuthority.current = "extensions";
    catalogGeneration.current += 1;
    setExtensionOperations((current) => ({
      ...current,
      [extension.extension_id]: operation,
    }));
    setExtensionError(null);

    try {
      const response = actionId === "configure"
        ? await client.configureSkill(
            extension.extension_id,
            configuration ?? {},
            extension.revision,
            clientRequestId,
          )
        : await client.mutateExtension(
            extension.extension_id,
            actionId,
            extension.revision,
            clientRequestId,
          );
      if (response.extension.extension_id !== extension.extension_id) {
        throw new RuntimeApiError("扩展状态与当前操作不匹配。", 502, "extension_projection_mismatch");
      }
      catalogGeneration.current += 1;
      acceptCatalog(response.extensions);
      requestIds.current.delete(requestKey);
      return true;
    } catch (error) {
      if (error instanceof RuntimeApiError && error.status === 409) {
        requestIds.current.delete(requestKey);
        try {
          const generation = ++catalogGeneration.current;
          const current = await client.extensionCatalog();
          if (generation !== catalogGeneration.current) return false;
          acceptCatalog(current);
          setExtensionError(
            `扩展操作未执行：${formatError(error)} 目录已刷新，请核对当前提供的操作后重试。`,
          );
        } catch (refreshError) {
          transitionCatalogState("error");
          setExtensionError(
            `扩展操作未执行：${formatError(error)} 重新读取目录也失败：${formatError(refreshError)}`,
          );
        }
      } else {
        transitionCatalogState("error");
        setExtensionError(
          `扩展“${extension.display_name}”操作失败：${formatError(error)} 当前状态没有在页面中推断或改写，请重试。`,
        );
      }
      return false;
    } finally {
      operationLocks.current.delete(extension.extension_id);
      setExtensionOperations((current) => {
        const next = { ...current };
        delete next[extension.extension_id];
        return next;
      });
      // The same request ID is retained after a transport failure for an
      // identical revision/action retry. A 409 deletes it above because the
      // authoritative revision or available action changed.
    }
  }, [acceptCatalog, client, formatError, transitionCatalogState]);

  const installLocalSkill = useCallback(async (
    file: File,
  ): Promise<boolean> => {
    if (!file.name.toLocaleLowerCase("en-US").endsWith(".zip")) {
      setExtensionError("本地技能安装包必须是 .zip 文件。");
      return false;
    }
    if (file.size <= 0 || file.size > 10 * 1024 * 1024) {
      setExtensionError("本地技能安装包不能为空，且不能超过 10 MB。");
      return false;
    }
    if (catalogStateRef.current !== "ready" || operationLocks.current.size) {
      setExtensionError("扩展目录尚未就绪或正在执行其他操作。");
      return false;
    }
    const operationId = "local-skill-install";
    const requestKey = `local-install:${file.name}:${file.size}:${file.lastModified}`;
    const clientRequestId = requestIds.current.get(requestKey)
      ?? createClientRequestId("extension_install_local");
    requestIds.current.set(requestKey, clientRequestId);
    operationLocks.current.add(operationId);
    setExtensionInstallBusy(true);
    setExtensionError(null);
    catalogAuthority.current = "extensions";
    catalogGeneration.current += 1;
    try {
      const bundleBase64 = await readFileBase64(file);
      const response = await client.installLocalSkill(
        bundleBase64,
        clientRequestId,
      );
      catalogGeneration.current += 1;
      acceptCatalog(response.extensions);
      requestIds.current.delete(requestKey);
      return true;
    } catch (error) {
      if (error instanceof RuntimeApiError && error.status === 409) {
        requestIds.current.delete(requestKey);
        await refreshExtensions();
      }
      setExtensionError(`本地技能安装失败：${formatError(error)} 未通过检查的内容不会被执行或启用。`);
      return false;
    } finally {
      operationLocks.current.delete(operationId);
      setExtensionInstallBusy(false);
    }
  }, [acceptCatalog, client, formatError, refreshExtensions]);

  const refreshMcpOAuth = useCallback(async (
    signal?: AbortSignal,
  ): Promise<Record<string, MCPOAuthStatusProjection>> => {
    try {
      const response = await client.mcpOAuthStatuses(signal);
      const next = Object.fromEntries(response.items.map((item) => [item.service_id, item]));
      setMcpOAuthStatuses(next);
      return next;
    } catch (error) {
      if (isAbortError(error)) return {};
      if (error instanceof RuntimeApiError && error.status === 404) {
        setMcpOAuthStatuses({});
        return {};
      }
      setExtensionError(`扩展服务授权状态读取失败：${formatError(error)}`);
      return {};
    }
  }, [client, formatError]);

  const beginMcpOAuth = useCallback(async (serviceId: string): Promise<boolean> => {
    if (mcpOAuthBusy) return false;
    const popup = window.open("about:blank", "_blank", "popup,width=720,height=780");
    if (!popup) {
      setExtensionError("浏览器阻止了授权窗口，请允许弹窗后重试。");
      return false;
    }
    setMcpOAuthBusy(serviceId);
    setExtensionError(null);
    try {
      const challenge = await client.beginMcpOAuth(serviceId);
      if (challenge.service_id !== serviceId || challenge.expires_at * 1000 <= Date.now()) {
        throw new Error("扩展服务返回了无效或过期的授权会话。");
      }
      popup.location.href = safeConnectorAuthorizationUrl(challenge.authorization_url);
      try {
        popup.opener = null;
        popup.focus();
      } catch {
        // The cross-origin authorization window is intentionally opaque.
      }
      for (let attempt = 0; attempt < 90; attempt += 1) {
        await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 750));
        const current = await refreshMcpOAuth();
        if (current[serviceId]?.state === "authorized") return true;
      }
      setExtensionError("授权状态尚未同步。完成登录后可点击“刷新授权状态”。");
      return false;
    } catch (error) {
      try { popup.close(); } catch { /* ignored */ }
      setExtensionError(`扩展服务授权未完成：${formatError(error)}`);
      return false;
    } finally {
      setMcpOAuthBusy(null);
    }
  }, [client, formatError, mcpOAuthBusy, refreshMcpOAuth]);

  const clearMcpOAuth = useCallback(async (serviceId: string): Promise<boolean> => {
    if (mcpOAuthBusy) return false;
    setMcpOAuthBusy(serviceId);
    setExtensionError(null);
    try {
      await client.clearMcpOAuth(serviceId);
      await refreshMcpOAuth();
      return true;
    } catch (error) {
      setExtensionError(`取消扩展服务授权失败：${formatError(error)}`);
      return false;
    } finally {
      setMcpOAuthBusy(null);
    }
  }, [client, formatError, mcpOAuthBusy, refreshMcpOAuth]);

  const refreshSkillHub = useCallback(async (
    query = "",
    category: SkillHubCardProjection["category"] | null = null,
    tag: string | null = null,
    source: string | null = null,
    signal?: AbortSignal,
  ): Promise<boolean> => {
    setSkillHubState("loading");
    setSkillHubError(null);
    try {
      const response = await client.skillHubCatalog(query, category, tag, source, null, signal);
      if (signal?.aborted) return false;
      setSkillHubItems(response.items);
      setSkillHubState("ready");
      return true;
    } catch (error) {
      if (isAbortError(error)) return false;
      setSkillHubState("error");
      setSkillHubError(formatError(error));
      return false;
    }
  }, [client, formatError]);

  const installHubSkill = useCallback(async (card: SkillHubCardProjection): Promise<boolean> => {
    if (hubInstallLock.current || operationLocks.current.size) return false;
    hubInstallLock.current = true;
    setSkillHubInstallingSlug(card.slug);
    setSkillHubError(null);
    try {
      const response = await client.installHubSkill(card);
      acceptCatalog(response.extensions);
      await refreshSkillHub();
      return true;
    } catch (error) {
      setSkillHubError(formatError(error));
      return false;
    } finally {
      hubInstallLock.current = false;
      setSkillHubInstallingSlug(null);
    }
  }, [acceptCatalog, client, formatError, refreshSkillHub]);

  const downloadHubSkill = useCallback(async (card: SkillHubCardProjection): Promise<boolean> => {
    if (skillHubDownloadingSlug) return false;
    setSkillHubDownloadingSlug(card.slug);
    setSkillHubError(null);
    try {
      const blob = await client.downloadHubSkillPackage(card);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${card.slug}-${card.version}.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
      return true;
    } catch (error) {
      setSkillHubError(formatError(error));
      return false;
    } finally {
      setSkillHubDownloadingSlug(null);
    }
  }, [client, formatError, skillHubDownloadingSlug]);

  const loadHubSkillDetail = useCallback(async (card: SkillHubCardProjection): Promise<boolean> => {
    setSkillHubDetailLoadingSlug(card.slug);
    setSkillHubError(null);
    try {
      setSkillHubDetail(await client.skillHubDetail(card.slug));
      return true;
    } catch (error) {
      setSkillHubError(formatError(error));
      return false;
    } finally {
      setSkillHubDetailLoadingSlug(null);
    }
  }, [client, formatError]);

  const publishHubSkill = useCallback(async (
    slug: string,
    category: SkillHubCardProjection["category"],
    file: File,
  ): Promise<boolean> => {
    const normalizedSlug = slug.trim();
    if (!/^[a-z0-9][a-z0-9-]{1,95}$/.test(normalizedSlug)) {
      setSkillHubError("Skill slug 需以小写字母或数字开头，只能包含小写字母、数字和连字符。");
      return false;
    }
    if (!file.name.toLocaleLowerCase("en-US").endsWith(".zip") || file.size <= 0 || file.size > 10 * 1024 * 1024) {
      setSkillHubError("发布包必须是不超过 10 MB 的 ZIP 文件。");
      return false;
    }
    if (hubUploadLock.current || operationLocks.current.size) return false;
    hubUploadLock.current = true;
    setSkillHubUploadBusy(true);
    setSkillHubError(null);
    try {
      await client.publishHubSkill(normalizedSlug, category, await readFileBase64(file));
      await refreshSkillHub();
      return true;
    } catch (error) {
      setSkillHubError(formatError(error));
      return false;
    } finally {
      hubUploadLock.current = false;
      setSkillHubUploadBusy(false);
    }
  }, [client, formatError, refreshSkillHub]);

  return {
    extensionSnapshot,
    extensionCatalogState,
    extensionError,
    extensionOperations,
    extensionInstallBusy,
    mcpOAuthStatuses,
    mcpOAuthBusy,
    clearExtensionError: () => setExtensionError(null),
    refreshExtensions,
    mutateExtension,
    configureSkill: (extension: ExtensionProjection, values: Record<string, string>) => (
      mutateExtension(extension, "configure", values)
    ),
    installLocalSkill,
    refreshMcpOAuth,
    beginMcpOAuth,
    clearMcpOAuth,
    skillHubItems,
    skillHubState,
    skillHubError,
    skillHubInstallingSlug,
    skillHubDownloadingSlug,
    skillHubDetail,
    skillHubDetailLoadingSlug,
    skillHubUploadBusy,
    refreshSkillHub,
    installHubSkill,
    downloadHubSkill,
    loadHubSkillDetail,
    clearHubSkillDetail: () => setSkillHubDetail(null),
    publishHubSkill,
  };
}
