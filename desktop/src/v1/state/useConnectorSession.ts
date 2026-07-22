import { useCallback, useEffect, useRef, useState } from "react";

import type {
  BootstrapResponse,
  ConnectorCatalogItem,
  ConnectorCatalogResponse,
  ConnectorInstanceProjection,
} from "../api/contracts.ts";
import {
  createClientRequestId,
  RuntimeClient,
} from "../api/runtimeClient.ts";
import {
  connectorAuthorizationCompleted,
  preferredConnectorAuthKind,
  safeConnectorAuthorizationUrl,
  type ConnectorCatalogLoadState,
  type ConnectorOperationKind,
  type ConnectorOperationState,
} from "./connectors.ts";

interface UseConnectorSessionOptions {
  client: RuntimeClient;
  bootstrapped: boolean;
  refreshBootstrap: (signal?: AbortSignal) => Promise<BootstrapResponse>;
  formatError: (error: unknown) => string;
}

function connectorDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    let timer = 0;
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function useConnectorSession({
  client,
  bootstrapped,
  refreshBootstrap,
  formatError,
}: UseConnectorSessionOptions) {
  const [connectorCatalog, setConnectorCatalog] = useState<ConnectorCatalogItem[]>([]);
  const [connectorCatalogState, setConnectorCatalogState] = useState<ConnectorCatalogLoadState>("idle");
  const [connectorError, setConnectorError] = useState<string | null>(null);
  const [connectorNotice, setConnectorNotice] = useState<string | null>(null);
  const [connectorOperations, setConnectorOperations] = useState<Record<string, ConnectorOperationState>>({});
  const operationLocks = useRef(new Set<string>());
  const requestIds = useRef(new Map<string, string>());
  const pollers = useRef(new Map<string, AbortController>());

  const refreshConnectors = useCallback(async (
    signal?: AbortSignal,
    quiet = false,
  ): Promise<ConnectorCatalogResponse | null> => {
    if (!quiet) setConnectorCatalogState("loading");
    try {
      const response = await client.connectorCatalog(signal);
      if (signal?.aborted) return null;
      setConnectorCatalog(response.items);
      setConnectorCatalogState("ready");
      if (!quiet) setConnectorError(null);
      return response;
    } catch (error) {
      if (isAbortError(error)) return null;
      setConnectorCatalogState("error");
      setConnectorError(
        `连接目录加载失败：${formatError(error)} 请稍后重试。`,
      );
      return null;
    }
  }, [client, formatError]);

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    void refreshConnectors(controller.signal);
    return () => controller.abort();
  }, [bootstrapped, refreshConnectors]);

  useEffect(() => () => {
    for (const controller of pollers.current.values()) controller.abort();
    pollers.current.clear();
  }, []);

  const beginOperation = useCallback((
    connectorId: string,
    instanceId: string | null,
    kind: ConnectorOperationKind,
  ): ConnectorOperationState | null => {
    if (operationLocks.current.has(connectorId)) return null;
    const requestKey = `${kind}:${connectorId}:${instanceId ?? "new"}`;
    const clientRequestId = requestIds.current.get(requestKey)
      ?? createClientRequestId(`connector_${kind}`);
    requestIds.current.set(requestKey, clientRequestId);
    const operation = { connectorId, instanceId, kind, clientRequestId };
    operationLocks.current.add(connectorId);
    setConnectorOperations((current) => ({ ...current, [connectorId]: operation }));
    return operation;
  }, []);

  const finishOperation = useCallback((
    operation: ConnectorOperationState,
    succeeded: boolean,
  ) => {
    operationLocks.current.delete(operation.connectorId);
    if (succeeded) {
      const requestKey = `${operation.kind}:${operation.connectorId}:${operation.instanceId ?? "new"}`;
      requestIds.current.delete(requestKey);
    }
    setConnectorOperations((current) => {
      const next = { ...current };
      delete next[operation.connectorId];
      return next;
    });
  }, []);

  const removeInstance = useCallback((connectorId: string, instanceId: string) => {
    setConnectorCatalog((current) => current.map((candidate) => (
      candidate.definition.connector_id === connectorId
        ? {
            ...candidate,
            instances: candidate.instances.filter(
              (instance) => instance.instance_id !== instanceId,
            ),
          }
        : candidate
    )));
  }, []);

  const replaceInstance = useCallback((
    connectorId: string,
    projection: ConnectorInstanceProjection,
  ) => {
    setConnectorCatalog((current) => current.map((candidate) => (
      candidate.definition.connector_id === connectorId
        ? {
            ...candidate,
            instances: candidate.instances.map((instance) => (
              instance.instance_id === projection.instance_id ? projection : instance
            )),
          }
        : candidate
    )));
  }, []);

  const pollAuthorization = useCallback(async (
    connectorId: string,
    previousInstanceIds: ReadonlySet<string>,
    reauthorizedInstanceId: string | null,
    signal: AbortSignal,
  ): Promise<boolean> => {
    let sawCatalog = false;
    let lastError: unknown = null;
    for (let attempt = 0; attempt < 45 && !signal.aborted; attempt += 1) {
      if (attempt > 0) await connectorDelay(1_000, signal);
      let response: ConnectorCatalogResponse | null = null;
      try {
        response = await client.connectorCatalog(signal);
        sawCatalog = true;
        setConnectorCatalog(response.items);
        setConnectorCatalogState("ready");
      } catch (error) {
        if (isAbortError(error)) throw error;
        lastError = error;
      }
      if (attempt % 5 === 0) {
        try {
          await refreshBootstrap(signal);
        } catch (error) {
          if (isAbortError(error)) throw error;
        }
      }
      if (response && connectorAuthorizationCompleted(
        response,
        connectorId,
        previousInstanceIds,
        reauthorizedInstanceId,
      )) {
        await refreshBootstrap(signal).catch(() => undefined);
        return true;
      }
    }
    if (!sawCatalog && lastError) throw lastError;
    return false;
  }, [client, refreshBootstrap]);

  const authorize = useCallback(async (
    item: ConnectorCatalogItem,
    reconnectInstance: ConnectorInstanceProjection | null = null,
  ) => {
    const connectorId = item.definition.connector_id;
    const authKind = preferredConnectorAuthKind(item);
    if (!item.adapter_available || !authKind) {
      setConnectorError("此连接器不能在页面授权；页面不会收集密钥。");
      return false;
    }
    const operation = beginOperation(
      connectorId,
      reconnectInstance?.instance_id ?? null,
      reconnectInstance ? "reconnecting" : "connecting",
    );
    if (!operation) return false;

    let succeeded = false;
    let popup: Window | null = null;
    const previousInstanceIds = new Set(
      item.instances.map((instance) => instance.instance_id),
    );
    setConnectorError(null);
    setConnectorNotice(null);
    try {
      const challenge = reconnectInstance
        ? await client.reauthorizeConnector(
            reconnectInstance.instance_id,
            authKind,
            operation.clientRequestId,
          )
        : await client.beginConnectorAuth(
            connectorId,
            authKind,
            operation.clientRequestId,
          );
      if (challenge.connector_id !== connectorId || challenge.auth_kind !== authKind) {
        throw new Error("连接服务返回了不匹配的授权会话。");
      }
      const expiresAt = Date.parse(challenge.expires_at);
      if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
        throw new Error("连接服务返回的授权会话已过期。");
      }
      const authorizationUrl = safeConnectorAuthorizationUrl(challenge.authorization_url);
      popup = window.open(
        authorizationUrl,
        "_blank",
        "popup,width=720,height=780",
      );
      if (!popup) {
        throw new Error("浏览器阻止了登录窗口，请允许弹窗后重试。");
      }
      try {
        popup.opener = null;
        popup.focus();
      } catch {
        // Cross-origin authorization windows are intentionally opaque to WebUI.
      }

      setConnectorNotice(
        `${item.definition.display_name} 授权窗口已打开。完成授权后返回 e-Mate；页面会自动同步状态。`,
      );
      const controller = new AbortController();
      pollers.current.get(connectorId)?.abort();
      pollers.current.set(connectorId, controller);
      const completed = await pollAuthorization(
        connectorId,
        previousInstanceIds,
        reconnectInstance?.instance_id ?? null,
        controller.signal,
      );
      if (completed) {
        succeeded = true;
        setConnectorError(null);
        setConnectorNotice(
          `${item.definition.display_name} 已连接。授权窗口现在可以关闭。`,
        );
      } else {
        setConnectorNotice(
          `${item.definition.display_name} 的授权状态尚未同步。完成授权后点击“刷新状态”。`,
        );
      }
      return completed;
    } catch (error) {
      if (isAbortError(error)) return false;
      setConnectorError(
        `${item.definition.display_name} 授权未完成：${formatError(error)} 现有连接没有变化，请修正后重试。`,
      );
      return false;
    } finally {
      pollers.current.delete(connectorId);
      finishOperation(operation, succeeded);
    }
  }, [
    beginOperation,
    client,
    finishOperation,
    formatError,
    pollAuthorization,
  ]);

  const refreshConnectorHealth = useCallback(async (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => {
    const operation = beginOperation(
      item.definition.connector_id,
      instance.instance_id,
      "checking",
    );
    if (!operation) return false;
    let succeeded = false;
    setConnectorError(null);
    try {
      const projection = await client.refreshConnectorHealth(
        instance.instance_id,
        operation.clientRequestId,
      );
      replaceInstance(item.definition.connector_id, projection);
      await refreshConnectors(undefined, true);
      succeeded = true;
      return true;
    } catch (error) {
      setConnectorError(
        `${item.definition.display_name} 状态检查失败：${formatError(error)} 请稍后重试。`,
      );
      return false;
    } finally {
      finishOperation(operation, succeeded);
    }
  }, [
    beginOperation,
    client,
    finishOperation,
    formatError,
    refreshConnectors,
    replaceInstance,
  ]);

  const disconnectConnector = useCallback(async (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => {
    const operation = beginOperation(
      item.definition.connector_id,
      instance.instance_id,
      "disconnecting",
    );
    if (!operation) return false;
    let succeeded = false;
    setConnectorError(null);
    try {
      await client.disconnectConnector(instance.instance_id, operation.clientRequestId);
      removeInstance(item.definition.connector_id, instance.instance_id);
      await Promise.all([
        refreshConnectors(undefined, true),
        refreshBootstrap().catch(() => null),
      ]);
      succeeded = true;
      return true;
    } catch (error) {
      setConnectorError(
        `${item.definition.display_name} 未能断开：${formatError(error)} 连接仍保持原状态，请重试。`,
      );
      return false;
    } finally {
      finishOperation(operation, succeeded);
    }
  }, [
    beginOperation,
    client,
    finishOperation,
    formatError,
    refreshBootstrap,
    refreshConnectors,
    removeInstance,
  ]);

  return {
    connectorCatalog,
    connectorCatalogState,
    connectorError,
    connectorNotice,
    connectorOperations,
    clearConnectorError: () => setConnectorError(null),
    clearConnectorNotice: () => setConnectorNotice(null),
    refreshConnectors: () => refreshConnectors(),
    connectConnector: (item: ConnectorCatalogItem) => authorize(item),
    reconnectConnector: (
      item: ConnectorCatalogItem,
      instance: ConnectorInstanceProjection,
    ) => authorize(item, instance),
    refreshConnectorHealth,
    disconnectConnector,
  };
}
