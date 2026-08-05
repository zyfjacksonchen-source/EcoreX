import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleAlert,
  FileText,
  Link2,
  LoaderCircle,
  MessageCircle,
  RefreshCw,
  RotateCw,
  Unplug,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type {
  ConnectorCatalogItem,
  ConnectorHealth,
  ConnectorInstanceProjection,
} from "../api/contracts.ts";
import {
  connectorHealthLabel,
  connectorOverallHealth,
  connectorSections,
  connectorUnavailableMessage,
  type ConnectorCatalogLoadState,
  type ConnectorOperationState,
} from "../state/connectors.ts";
import { serviceReasonMessage } from "../state/userLanguage.ts";
import { IconButton } from "./IconButton.tsx";
import { TechnicalDetails } from "./TechnicalDetails.tsx";

export interface ConnectorCatalogPanelProps {
  catalog: ConnectorCatalogItem[];
  loadState: ConnectorCatalogLoadState;
  error: string | null;
  notice: string | null;
  operations: Record<string, ConnectorOperationState>;
  onRefresh: () => Promise<unknown>;
  onConnect: (item: ConnectorCatalogItem) => Promise<boolean>;
  onReconnect: (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => Promise<boolean>;
  onHealthCheck: (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => Promise<boolean>;
  onDisconnect: (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => Promise<boolean>;
  onClearError: () => void;
  onClearNotice: () => void;
}

const busyLabels: Record<ConnectorOperationState["kind"], string> = {
  connecting: "等待授权",
  reconnecting: "重新授权中",
  checking: "检查中",
  disconnecting: "断开中",
};

function HealthIcon({ health }: { health: ConnectorHealth }) {
  if (health === "connected") return <CheckCircle2 aria-hidden="true" />;
  if (health === "degraded") return <AlertTriangle aria-hidden="true" />;
  if (health === "error") return <CircleAlert aria-hidden="true" />;
  if (health === "disabled") return <Ban aria-hidden="true" />;
  if (health === "authenticating") return <LoaderCircle aria-hidden="true" />;
  return <Unplug aria-hidden="true" />;
}

function ConnectorStatus({ health }: { health: ConnectorHealth }) {
  return (
    <span className={`ex-connector-status is-${health}`}>
      <HealthIcon health={health} />
      {connectorHealthLabel(health)}
    </span>
  );
}

function ConnectorIcon({ item }: { item: ConnectorCatalogItem }) {
  if (item.definition.icon_key === "tencent-docs") return <FileText aria-hidden="true" />;
  if (item.definition.icon_key === "feishu") return <Link2 aria-hidden="true" />;
  return <MessageCircle aria-hidden="true" />;
}

interface InstanceRowProps {
  item: ConnectorCatalogItem;
  instance: ConnectorInstanceProjection;
  operation: ConnectorOperationState | null;
  confirmDisconnectId: string | null;
  onReconnect: ConnectorCatalogPanelProps["onReconnect"];
  onHealthCheck: ConnectorCatalogPanelProps["onHealthCheck"];
  onRequestDisconnect: (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => void;
}

function InstanceRow({
  item,
  instance,
  operation,
  confirmDisconnectId,
  onReconnect,
  onHealthCheck,
  onRequestDisconnect,
}: InstanceRowProps) {
  const busy = Boolean(operation);
  const activeOperation = operation?.instanceId === instance.instance_id ? operation : null;
  const needsReconnect = ["degraded", "error", "disabled"].includes(instance.health);
  const confirming = confirmDisconnectId === instance.instance_id;
  return (
    <div className="ex-connector-instance" data-health={instance.health}>
      <div className="ex-connector-instance-copy">
        <strong>{instance.account_display_name || "已授权账号"}</strong>
        <ConnectorStatus health={instance.health} />
        {instance.last_error_code ? (
          <>
            <span className="ex-connector-error-code">
              {serviceReasonMessage(
                instance.last_error_code,
                "最近一次连接未成功。你可以重新连接或检查状态。",
              )}
            </span>
            <TechnicalDetails entries={[
              { label: "错误代码", value: instance.last_error_code },
            ]} />
          </>
        ) : null}
      </div>
      <div className="ex-connector-actions" aria-label={`${item.definition.display_name} 连接操作`}>
        {needsReconnect ? (
          <button
            className="ex-button is-primary ex-connector-action"
            type="button"
            disabled={busy}
            onClick={() => void onReconnect(item, instance)}
          >
            <RotateCw aria-hidden="true" />
            {activeOperation?.kind === "reconnecting" ? busyLabels.reconnecting : "重新连接"}
          </button>
        ) : null}
        <button
          className="ex-button ex-connector-action"
          type="button"
          disabled={busy || instance.health === "authenticating"}
          onClick={() => void onHealthCheck(item, instance)}
        >
          <RefreshCw aria-hidden="true" />
          {activeOperation?.kind === "checking" ? busyLabels.checking : "检查状态"}
        </button>
        <button
          className={`ex-button ex-connector-action${confirming ? " is-danger" : ""}`}
          type="button"
          disabled={busy}
          onClick={() => onRequestDisconnect(item, instance)}
        >
          <Unplug aria-hidden="true" />
          {activeOperation?.kind === "disconnecting"
            ? busyLabels.disconnecting
            : confirming
              ? "确认断开"
              : "断开"}
        </button>
      </div>
    </div>
  );
}

export function ConnectorCatalogPanel({
  catalog,
  loadState,
  error,
  notice,
  operations,
  onRefresh,
  onConnect,
  onReconnect,
  onHealthCheck,
  onDisconnect,
  onClearError,
  onClearNotice,
}: ConnectorCatalogPanelProps) {
  const [confirmDisconnectId, setConfirmDisconnectId] = useState<string | null>(null);
  const confirmTimer = useRef<number | null>(null);
  const sections = useMemo(() => connectorSections(catalog), [catalog]);

  const clearDisconnectConfirmation = () => {
    if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    confirmTimer.current = null;
    setConfirmDisconnectId(null);
  };

  useEffect(() => {
    void onRefresh();
    return () => {
      if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    };
  }, [onRefresh]);

  const requestDisconnect = (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => {
    if (confirmDisconnectId === instance.instance_id) {
      clearDisconnectConfirmation();
      void onDisconnect(item, instance);
      return;
    }
    clearDisconnectConfirmation();
    setConfirmDisconnectId(instance.instance_id);
    confirmTimer.current = window.setTimeout(clearDisconnectConfirmation, 5_000);
  };

  return (
    <section className="ex-connector-catalog-panel" aria-label="协作连接" aria-busy={loadState === "loading"}>
        <div className="ex-connector-catalog">
          <div className="ex-popover-heading">
            <div>
              <strong>协作连接</strong>
              <span>统一连接飞书、腾讯文档和外部消息渠道，并随时检查或撤销授权</span>
            </div>
            <div className="ex-connector-heading-actions">
              <IconButton
                label="刷新协作连接状态"
                tooltipSide="bottom"
                disabled={loadState === "loading"}
                onClick={() => void onRefresh()}
              >
                <RefreshCw aria-hidden="true" />
              </IconButton>
            </div>
          </div>

          {error ? (
            <div className="ex-connector-message is-error" role="alert">
              <CircleAlert aria-hidden="true" />
              <span>{error}</span>
              <button className="ex-button" type="button" onClick={() => void onRefresh()}>
                重新加载
              </button>
              <IconButton label="关闭协作连接错误" tooltipSide="bottom" onClick={onClearError}>
                <X aria-hidden="true" />
              </IconButton>
            </div>
          ) : null}
          {notice ? (
            <div className="ex-connector-message is-info" role="status">
              <Link2 aria-hidden="true" />
              <span>{notice}</span>
              <IconButton label="关闭授权提示" tooltipSide="bottom" onClick={onClearNotice}>
                <X aria-hidden="true" />
              </IconButton>
            </div>
          ) : null}

          <div className="ex-connector-list">
            {loadState === "loading" && !catalog.length ? (
              <div className="ex-connector-loading" role="status">
                <LoaderCircle aria-hidden="true" />
                <span>正在加载连接器目录…</span>
              </div>
            ) : null}
            {sections.map((section) => (
              <section className="ex-connector-section" key={section.tier}>
                <div className="ex-connector-section-heading">
                  <strong>{section.label}</strong>
                  <span>{section.description}</span>
                </div>
                {section.items.map((item) => {
                  const connectorId = item.definition.connector_id;
                  const operation = operations[connectorId] ?? null;
                  const unavailable = connectorUnavailableMessage(item);
                  const overallHealth = connectorOverallHealth(item);
                  return (
                    <article className="ex-connector-row" key={connectorId} data-health={overallHealth}>
                      <span className="ex-connector-icon" aria-hidden="true">
                        <ConnectorIcon item={item} />
                      </span>
                      <div className="ex-connector-body">
                        <div className="ex-connector-title">
                          <strong>{item.definition.display_name}</strong>
                          {item.definition.tier === "beta" ? (
                            <span className="ex-connector-tier">Beta</span>
                          ) : null}
                          {!item.instances.length ? <ConnectorStatus health={overallHealth} /> : null}
                        </div>
                        <p>{item.definition.description}</p>
                        {item.instances.map((instance) => (
                          <InstanceRow
                            key={instance.instance_id}
                            item={item}
                            instance={instance}
                            operation={operation}
                            confirmDisconnectId={confirmDisconnectId}
                            onReconnect={onReconnect}
                            onHealthCheck={onHealthCheck}
                            onRequestDisconnect={requestDisconnect}
                          />
                        ))}
                        {unavailable ? (
                          <>
                            <p
                              id={`connector-unavailable-${connectorId}`}
                              className="ex-connector-unavailable"
                            >
                              {unavailable}
                            </p>
                            <div className="ex-connector-connect-row">
                              <button
                                className="ex-button ex-connector-action"
                                type="button"
                                disabled
                                aria-describedby={`connector-unavailable-${connectorId}`}
                              >
                                <Ban aria-hidden="true" />
                                暂不可连接
                              </button>
                            </div>
                          </>
                        ) : null}
                        {!unavailable ? (
                          <div className="ex-connector-connect-row">
                            <button
                              className={`ex-button ex-connector-action${item.instances.length ? "" : " is-primary"}`}
                              type="button"
                              disabled={Boolean(operation)}
                              onClick={() => void onConnect(item)}
                            >
                              <Link2 aria-hidden="true" />
                              {operation?.kind === "connecting"
                                ? busyLabels.connecting
                                : item.instances.length
                                  ? "添加账号"
                                  : "连接"}
                            </button>
                          </div>
                        ) : null}
                      </div>
                    </article>
                  );
                })}
              </section>
            ))}
            {loadState !== "loading" && !catalog.length ? (
              <div className="ex-popover-empty">
                <Unplug aria-hidden="true" />
                <strong>协作连接目录为空</strong>
                <span>当前安装中没有可用连接。刷新后仍为空时，请检查是否已安装完整组件。</span>
                <button className="ex-button" type="button" onClick={() => void onRefresh()}>
                  刷新目录
                </button>
              </div>
            ) : null}
          </div>
        </div>
    </section>
  );
}
