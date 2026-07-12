import { useCallback, useEffect, useRef, useState } from "react";

import type {
  BootstrapResponse,
  ExtensionActionId,
  ExtensionCatalogSnapshot,
  ExtensionProjection,
} from "../api/contracts.ts";
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
      const response = await client.mutateExtension(
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
    extensionId: string,
    file: File,
  ): Promise<boolean> => {
    const normalizedId = extensionId.trim();
    if (!/^[a-z][a-z0-9_.-]{1,127}$/.test(normalizedId)) {
      setExtensionError("扩展 ID 需以小写字母开头，只能包含小写字母、数字及 ._-。");
      return false;
    }
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
    const existing = extensionSnapshot?.items.find((item) => item.extension_id === normalizedId);
    const expectedRevision = existing?.revision ?? 0;
    const requestKey = `local-install:${normalizedId}:${expectedRevision}:${file.name}:${file.size}:${file.lastModified}`;
    const clientRequestId = requestIds.current.get(requestKey)
      ?? createClientRequestId("extension_install_local");
    requestIds.current.set(requestKey, clientRequestId);
    operationLocks.current.add(normalizedId);
    setExtensionInstallBusy(true);
    setExtensionError(null);
    catalogAuthority.current = "extensions";
    catalogGeneration.current += 1;
    try {
      const bundleBase64 = await readFileBase64(file);
      const response = await client.installLocalSkill(
        normalizedId,
        bundleBase64,
        expectedRevision,
        clientRequestId,
      );
      if (response.extension.extension_id !== normalizedId) {
        throw new RuntimeApiError("技能状态与当前安装操作不匹配。", 502, "extension_projection_mismatch");
      }
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
      operationLocks.current.delete(normalizedId);
      setExtensionInstallBusy(false);
    }
  }, [acceptCatalog, client, extensionSnapshot, formatError, refreshExtensions]);

  return {
    extensionSnapshot,
    extensionCatalogState,
    extensionError,
    extensionOperations,
    extensionInstallBusy,
    clearExtensionError: () => setExtensionError(null),
    refreshExtensions,
    mutateExtension,
    installLocalSkill,
  };
}
