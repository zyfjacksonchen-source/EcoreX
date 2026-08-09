import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleAlert,
  Copy,
  ExternalLink,
  FileText,
  Link2,
  LoaderCircle,
  MessageCircle,
  Play,
  Power,
  PowerOff,
  RefreshCw,
  RotateCw,
  Save,
  Unplug,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type {
  ChannelConnectorCatalogItem,
  ChannelDeviceAuthorizationProjection,
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
  channelCatalog: ChannelConnectorCatalogItem[];
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
  onSaveConfiguration: (
    item: ChannelConnectorCatalogItem,
    values: Readonly<{
      display_name: string;
      config: Record<string, string | number>;
      secrets: Record<string, string>;
    }>,
  ) => Promise<boolean>;
  onChannelAction: (
    item: ChannelConnectorCatalogItem,
    action: "test" | "enable" | "disable" | "retry",
  ) => Promise<boolean>;
  onChannelDisconnect: (item: ChannelConnectorCatalogItem) => Promise<boolean>;
  deviceAuthorizations: Record<string, ChannelDeviceAuthorizationProjection>;
  onBeginDeviceAuthorization: (item: ChannelConnectorCatalogItem) => Promise<boolean>;
  onDeviceAuthorizationAction: (
    item: ChannelConnectorCatalogItem,
    action: "cancel" | "refresh",
  ) => Promise<boolean>;
  onClearError: () => void;
  onClearNotice: () => void;
}

const busyLabels: Record<ConnectorOperationState["kind"], string> = {
  connecting: "等待授权",
  reconnecting: "重新授权中",
  checking: "检查中",
  disconnecting: "断开中",
  saving: "保存中",
  testing: "测试中",
  enabling: "启用中",
  disabling: "停用中",
  retrying: "重试中",
  authorizing: "登录中",
};

function safeDeviceVerificationUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if (
      !["https:", "weixin:"].includes(parsed.protocol)
      || parsed.username
      || parsed.password
    ) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

function DeviceAuthorization({
  item,
  flow,
  operation,
  onBegin,
  onAction,
}: {
  item: ChannelConnectorCatalogItem;
  flow: ChannelDeviceAuthorizationProjection | null;
  operation: ConnectorOperationState | null;
  onBegin: ConnectorCatalogPanelProps["onBeginDeviceAuthorization"];
  onAction: ConnectorCatalogPanelProps["onDeviceAuthorizationAction"];
}) {
  const [copied, setCopied] = useState(false);
  const verificationUrl = flow?.verification_url ?? null;
  const externalUrl = safeDeviceVerificationUrl(verificationUrl);
  const active = flow?.status === "pending" || flow?.status === "scanned";
  const expired = flow?.status === "expired" || flow?.status === "cancelled";
  const busy = operation?.kind === "authorizing";

  const copy = async () => {
    if (!verificationUrl) return;
    try {
      await navigator.clipboard.writeText(verificationUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2_000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="ex-channel-device-auth">
      {active ? (
        <div className="ex-channel-device-code" role="status">
          <strong>{flow.status === "scanned" ? "已扫码，请在手机上确认" : "微信扫码登录"}</strong>
          {flow.qr_image_data_url ? (
            <img
              src={flow.qr_image_data_url}
              alt="微信扫码登录二维码"
              data-testid="weixin-device-qr"
            />
          ) : null}
          <code data-testid="weixin-device-code">{verificationUrl}</code>
          <span>登录码为短期公开信息；确认后只在系统凭据库保存登录令牌。</span>
          <span>微信中的发送者名称来自所登录账号，请先将账号名称设为 e-Mate。</span>
          <div className="ex-channel-configuration-actions">
            <button className="ex-button" type="button" onClick={() => void copy()}>
              <Copy aria-hidden="true" />
              {copied ? "已复制" : "复制登录码"}
            </button>
            {externalUrl ? (
              <button
                className="ex-button"
                type="button"
                onClick={() => window.open(externalUrl, "_blank", "noopener,noreferrer")}
              >
                <ExternalLink aria-hidden="true" />
                在微信中打开
              </button>
            ) : null}
            <button className="ex-button" type="button" disabled={busy} onClick={() => void onAction(item, "cancel")}>
              <X aria-hidden="true" />
              取消登录
            </button>
          </div>
        </div>
      ) : null}
      {!active ? (
        <div className="ex-connector-connect-row">
          {expired ? (
            <button className="ex-button is-primary" type="button" disabled={busy} onClick={() => void onAction(item, "refresh")}>
              <RefreshCw aria-hidden="true" />
              {busy ? busyLabels.authorizing : "重新获取登录码"}
            </button>
          ) : (
            <button className="ex-button is-primary" type="button" disabled={busy} onClick={() => void onBegin(item)}>
              <Link2 aria-hidden="true" />
              {busy ? busyLabels.authorizing : item.instance ? "重新扫码登录" : "扫码登录"}
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}

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

interface ChannelConfigurationProps {
  item: ChannelConnectorCatalogItem;
  configurationEnabled: boolean;
  operation: ConnectorOperationState | null;
  onSave: ConnectorCatalogPanelProps["onSaveConfiguration"];
  onAction: ConnectorCatalogPanelProps["onChannelAction"];
  onDisconnect: ConnectorCatalogPanelProps["onChannelDisconnect"];
}

function ChannelConfiguration({
  item,
  configurationEnabled,
  operation,
  onSave,
  onAction,
  onDisconnect,
}: ChannelConfigurationProps) {
  const [open, setOpen] = useState(false);
  const [displayName, setDisplayName] = useState(item.instance?.display_name ?? item.label);
  const [values, setValues] = useState<Record<string, string>>({});
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const busy = Boolean(operation);
  const configurationEditable = configurationEnabled && !item.instance?.enabled;

  useEffect(() => {
    setDisplayName(item.instance?.display_name ?? item.label);
  }, [item.instance?.display_name, item.label]);

  useEffect(() => {
    if (!confirmDisconnect) return;
    const timer = window.setTimeout(() => setConfirmDisconnect(false), 5_000);
    return () => window.clearTimeout(timer);
  }, [confirmDisconnect]);

  const missingRequired = item.fields.some((field) => (
    field.required && !field.configured && !(values[field.key] ?? "").trim()
  ));

  const save = async () => {
    const config: Record<string, string | number> = {};
    const secrets: Record<string, string> = {};
    for (const field of item.fields) {
      const value = (values[field.key] ?? "").trim();
      if (!value) continue;
      if (field.secret || field.type === "secret") secrets[field.key] = value;
      else config[field.key] = field.type === "number" ? Number(value) : value;
    }
    if (await onSave(item, { display_name: displayName.trim(), config, secrets })) {
      setValues({});
    }
  };

  return (
    <div className="ex-channel-configuration">
      <button
        className="ex-button ex-connector-action"
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((current) => !current);
          if (open) setValues({});
        }}
      >
        {open
          ? "收起"
          : !configurationEnabled
            ? "管理已保存配置"
            : item.instance
              ? "管理配置"
              : "配置账号"}
      </button>
      {open ? (
        <form
          className="ex-channel-configuration-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          {configurationEnabled ? (
            <label>
              <span>连接名称</span>
              <input
                type="text"
                value={displayName}
                required
                maxLength={120}
                disabled={busy || !configurationEditable}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
          ) : null}
          {configurationEnabled
            ? item.fields.map((field) => {
                const isSecret = field.secret || field.type === "secret";
                return (
                  <label key={field.key}>
                    <span>
                      {field.label}
                      {field.required ? "（必填）" : ""}
                      {field.configured ? <small>已配置</small> : null}
                    </span>
                    <input
                      type={isSecret ? "password" : field.type === "number" ? "number" : "text"}
                      value={values[field.key] ?? ""}
                      required={field.required && !field.configured}
                      disabled={busy || !configurationEditable}
                      autoComplete={isSecret ? "new-password" : "off"}
                      placeholder={field.configured
                        ? "已安全保存；留空不修改"
                        : field.default === undefined
                          ? ""
                          : String(field.default)}
                      onChange={(event) => setValues((current) => ({
                        ...current,
                        [field.key]: event.target.value,
                      }))}
                    />
                  </label>
                );
              })
            : null}
          {configurationEnabled ? (
            <p className="ex-channel-secret-note">
              {configurationEditable
                ? "密钥只在本次保存时提交，保存后立即清空，页面不会读取或回显。"
                : "通道运行中不能修改配置；请先停用，再编辑并重新启用。"}
            </p>
          ) : null}
          {configurationEnabled ? (
            <p className="ex-channel-secret-note">
              外部软件显示名由对应平台的应用或机器人资料决定；连接前请将名称设为 e-Mate。
            </p>
          ) : null}
          <div className="ex-channel-configuration-actions">
            {configurationEditable && item.actions.save ? (
              <button
                className="ex-button is-primary"
                type="submit"
                disabled={busy || !displayName.trim() || missingRequired}
              >
                <Save aria-hidden="true" />
                {operation?.kind === "saving" ? busyLabels.saving : "保存配置"}
              </button>
            ) : null}
            {item.instance && item.actions.test ? (
              <button className="ex-button" type="button" disabled={busy} onClick={() => void onAction(item, "test")}>
                <Play aria-hidden="true" />
                {operation?.kind === "testing" ? busyLabels.testing : "测试连接"}
              </button>
            ) : null}
            {item.instance?.enabled && item.actions.disable ? (
              <button className="ex-button" type="button" disabled={busy} onClick={() => void onAction(item, "disable")}>
                <PowerOff aria-hidden="true" />
                {operation?.kind === "disabling" ? busyLabels.disabling : "停用"}
              </button>
            ) : null}
            {item.instance && !item.instance.enabled && item.actions.enable ? (
              <button className="ex-button" type="button" disabled={busy} onClick={() => void onAction(item, "enable")}>
                <Power aria-hidden="true" />
                {operation?.kind === "enabling" ? busyLabels.enabling : "启用"}
              </button>
            ) : null}
            {item.instance && item.actions.retry && ["degraded", "error"].includes(item.instance.health) ? (
              <button className="ex-button" type="button" disabled={busy} onClick={() => void onAction(item, "retry")}>
                <RotateCw aria-hidden="true" />
                {operation?.kind === "retrying" ? busyLabels.retrying : "重试连接"}
              </button>
            ) : null}
            {item.instance && item.actions.disconnect ? (
              <button
                className={`ex-button${confirmDisconnect ? " is-danger" : ""}`}
                type="button"
                disabled={busy}
                onClick={() => {
                  if (!confirmDisconnect) {
                    setConfirmDisconnect(true);
                    return;
                  }
                  setConfirmDisconnect(false);
                  void onDisconnect(item);
                }}
              >
                <Unplug aria-hidden="true" />
                {operation?.kind === "disconnecting"
                  ? busyLabels.disconnecting
                  : confirmDisconnect
                    ? "确认断开并删除凭据"
                    : "断开"}
              </button>
            ) : null}
          </div>
          {item.instance?.last_error_code ? (
            <div className="ex-channel-instance-status" role="status">
              <span>{serviceReasonMessage(item.instance.last_error_code, "连接最近一次运行失败，请检查配置后重试。")}</span>
            </div>
          ) : null}
        </form>
      ) : null}
    </div>
  );
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
  channelCatalog,
  loadState,
  error,
  notice,
  operations,
  onRefresh,
  onConnect,
  onReconnect,
  onHealthCheck,
  onDisconnect,
  onSaveConfiguration,
  onChannelAction,
  onChannelDisconnect,
  deviceAuthorizations,
  onBeginDeviceAuthorization,
  onDeviceAuthorizationAction,
  onClearError,
  onClearNotice,
}: ConnectorCatalogPanelProps) {
  const [confirmDisconnectId, setConfirmDisconnectId] = useState<string | null>(null);
  const confirmTimer = useRef<number | null>(null);
  const sections = useMemo(() => connectorSections(catalog), [catalog]);
  const channels = useMemo(
    () => new Map(channelCatalog.map((item) => [item.channel_id, item])),
    [channelCatalog],
  );

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
    <section className="ex-connector-catalog-panel" aria-label="外部连接与通道" aria-busy={loadState === "loading"}>
        <div className="ex-connector-catalog">
          <div className="ex-popover-heading">
            <div>
              <strong>通道</strong>
              <span>统一连接飞书、腾讯文档和外部消息渠道，并随时检查或撤销授权</span>
            </div>
            <div className="ex-connector-heading-actions">
              <IconButton
                label="刷新通道状态"
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
              <IconButton label="关闭通道错误" tooltipSide="bottom" onClick={onClearError}>
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
                  const channel = channels.get(connectorId) ?? null;
                  const isFeishu = connectorId === "feishu";
                  const selfServiceChannel = channel
                    && ["app_credentials", "api_token"].includes(channel.auth_kind)
                    && (isFeishu || channel.adapter_available || channel.instance)
                      ? channel
                      : null;
                  const deviceChannel = channel?.auth_kind === "device_code"
                    && (channel.adapter_available || channel.instance)
                      ? channel
                      : null;
                  const channelUnavailable = channel
                    ? channel.adapter_available
                      ? null
                      : channel.unavailable_reason === "dependency_missing"
                        ? "当前安装缺少这个通道所需的运行组件。"
                        : "当前安装暂不支持这个通道。"
                    : null;
                  const connectorUnavailable = connectorUnavailableMessage(item);
                  const unavailable = isFeishu || deviceChannel
                    ? null
                    : channelUnavailable ?? (selfServiceChannel ? null : connectorUnavailable);
                  const overallHealth = connectorOverallHealth(item);
                  const visibleHealth = selfServiceChannel?.instance?.health
                    ?? deviceChannel?.instance?.health
                    ?? overallHealth;
                  return (
                    <article className="ex-connector-row" key={connectorId} data-health={visibleHealth}>
                      <span className="ex-connector-icon" aria-hidden="true">
                        <ConnectorIcon item={item} />
                      </span>
                      <div className="ex-connector-body">
                        <div className="ex-connector-title">
                          <strong>{item.definition.display_name}</strong>
                          {item.definition.tier === "beta" ? (
                            <span className="ex-connector-tier">Beta</span>
                          ) : null}
                          {selfServiceChannel || !item.instances.length ? <ConnectorStatus health={visibleHealth} /> : null}
                        </div>
                        <p>{item.definition.description}</p>
                        {isFeishu ? (
                          <div className="ex-connector-subsection-heading">
                            <strong>文档与云空间授权</strong>
                            <span>使用个人飞书账号授权文档和云空间能力</span>
                          </div>
                        ) : null}
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
                        {isFeishu && connectorUnavailable ? (
                          <p className="ex-connector-unavailable" role="status">
                            {connectorUnavailable}
                          </p>
                        ) : null}
                        {isFeishu && !connectorUnavailable ? (
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
                        {selfServiceChannel ? (
                          <>
                            {isFeishu ? (
                              <div className="ex-connector-subsection-heading">
                                <strong>消息 Bot</strong>
                                <span>使用企业自建应用的 App ID 与 App Secret 收发消息</span>
                              </div>
                            ) : null}
                            {selfServiceChannel.adapter_available || selfServiceChannel.instance ? (
                              <ChannelConfiguration
                                item={selfServiceChannel}
                                configurationEnabled={selfServiceChannel.adapter_available}
                                operation={operation}
                                onSave={onSaveConfiguration}
                                onAction={onChannelAction}
                                onDisconnect={onChannelDisconnect}
                              />
                            ) : null}
                            {isFeishu && channelUnavailable ? (
                              <p className="ex-connector-unavailable" role="status">
                                {channelUnavailable}
                              </p>
                            ) : null}
                          </>
                        ) : null}
                        {deviceChannel ? (
                          <>
                            <DeviceAuthorization
                              item={deviceChannel}
                              flow={deviceAuthorizations[deviceChannel.channel_id] ?? null}
                              operation={operation}
                              onBegin={onBeginDeviceAuthorization}
                              onAction={onDeviceAuthorizationAction}
                            />
                            {deviceChannel.instance ? (
                              <ChannelConfiguration
                                item={deviceChannel}
                                configurationEnabled={false}
                                operation={operation}
                                onSave={onSaveConfiguration}
                                onAction={onChannelAction}
                                onDisconnect={onChannelDisconnect}
                              />
                            ) : null}
                          </>
                        ) : null}
                        {unavailable ? (
                          <p className="ex-connector-unavailable" role="status">
                            {unavailable}
                          </p>
                        ) : null}
                        {!unavailable && !selfServiceChannel && !deviceChannel && !isFeishu ? (
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
                <strong>通道目录为空</strong>
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
