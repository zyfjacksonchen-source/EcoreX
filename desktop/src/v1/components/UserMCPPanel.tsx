import {
  AlertCircle,
  CheckCircle2,
  LoaderCircle,
  Pencil,
  Play,
  Plus,
  Power,
  PowerOff,
  RefreshCw,
  Save,
  Server,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";

import type {
  MCPOAuthStatusProjection,
  UserMCPAuthKind,
  UserMCPServerProjection,
  UserMCPServerRequest,
} from "../api/contracts.ts";
import { RuntimeApiError, type RuntimeClient } from "../api/runtimeClient.ts";
import { IconButton } from "./IconButton.tsx";

type MCPBusyAction = "load" | "save" | "test" | "enable" | "disable" | "delete";

interface UserMCPPanelProps {
  client: RuntimeClient;
  projectId: string | null;
  oauthStatuses: Record<string, MCPOAuthStatusProjection>;
  oauthBusy: string | null;
  onRefreshOAuth: () => Promise<Record<string, MCPOAuthStatusProjection>>;
  onBeginOAuth: (serviceId: string) => Promise<boolean>;
  onClearOAuth: (serviceId: string) => Promise<boolean>;
  preset?: { key: number; displayName: string; endpoint: string; authKind: UserMCPAuthKind } | null;
}

interface MCPFormValue {
  displayName: string;
  endpoint: string;
  authKind: UserMCPAuthKind;
  credential: string;
  oauthClientId: string;
  oauthScope: string;
  authorizationHosts: string;
}

const EMPTY_FORM: MCPFormValue = {
  displayName: "",
  endpoint: "",
  authKind: "none",
  credential: "",
  oauthClientId: "",
  oauthScope: "",
  authorizationHosts: "",
};

function formFor(server: UserMCPServerProjection): MCPFormValue {
  return {
    displayName: server.display_name,
    endpoint: server.endpoint,
    authKind: server.auth_kind,
    credential: "",
    oauthClientId: server.oauth_client_id ?? "",
    oauthScope: server.oauth_scope,
    authorizationHosts: server.authorization_hosts.join(", "),
  };
}

function safeHttpsEndpoint(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:"
      && !url.username
      && !url.password
      && !url.search
      && !url.hash
      && (!url.port || url.port === "443");
  } catch {
    return false;
  }
}

function requestFor(form: MCPFormValue): UserMCPServerRequest {
  const authorizationHosts = [...new Set(
    form.authorizationHosts.split(",").map((value) => value.trim().toLowerCase()).filter(Boolean),
  )].sort();
  const request: UserMCPServerRequest = {
    display_name: form.displayName.trim(),
    endpoint: form.endpoint.trim(),
    auth_kind: form.authKind,
    oauth_scope: form.authKind === "oauth2" ? form.oauthScope.trim() : "",
    authorization_hosts: form.authKind === "oauth2" ? authorizationHosts : [],
  };
  if (form.authKind === "bearer" && form.credential) request.credential = form.credential;
  if (form.authKind === "oauth2" && form.oauthClientId.trim()) {
    request.oauth_client_id = form.oauthClientId.trim();
  }
  return request;
}

function errorCode(error: unknown): string {
  return error instanceof RuntimeApiError
    ? error.code ?? `http_${error.status}`
    : "mcp_request_failed";
}

function errorMessage(code: string): string {
  if (code === "mcp_endpoint_invalid" || code === "mcp_endpoint_not_public") {
    return "请输入不含账号、查询参数或片段的公开 HTTPS 地址。";
  }
  if (code === "mcp_bearer_credential_required") return "Bearer 令牌尚未配置。";
  if (code === "mcp_server_test_required") return "请先完成真实连接测试，再启用这个服务。";
  if (code === "mcp_server_restart_required") return "服务配置已保存，Runtime 重载后再测试。";
  if (code === "mcp_oauth_authorization_required") return "请先完成 OAuth 授权，再测试连接。";
  return "操作未完成，请检查配置或稍后重试。";
}

export function UserMCPPanel({
  client,
  projectId,
  oauthStatuses,
  oauthBusy,
  onRefreshOAuth,
  onBeginOAuth,
  onClearOAuth,
  preset = null,
}: UserMCPPanelProps) {
  const [items, setItems] = useState<UserMCPServerProjection[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<{ id: string; action: MCPBusyAction } | null>({ id: "catalog", action: "load" });
  const [editingId, setEditingId] = useState<string | "new" | null>(null);
  const [form, setForm] = useState<MCPFormValue>(EMPTY_FORM);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const load = async (signal?: AbortSignal) => {
    setBusy({ id: "catalog", action: "load" });
    try {
      const response = await client.userMcpServers(signal, projectId);
      if (signal?.aborted) return;
      setItems(response.items);
      setErrors((current) => {
        const next = { ...current };
        delete next.catalog;
        return next;
      });
      setLoaded(true);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setErrors((current) => ({ ...current, catalog: errorCode(error) }));
    } finally {
      if (!signal?.aborted) setBusy(null);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [client, projectId]);

  const upsert = (server: UserMCPServerProjection) => {
    setItems((current) => [...current.filter((item) => item.server_id !== server.server_id), server]
      .sort((left, right) => left.display_name.localeCompare(right.display_name, "zh-CN")));
  };

  const startEdit = (server?: UserMCPServerProjection) => {
    setEditingId(server?.server_id ?? "new");
    setForm(server ? formFor(server) : EMPTY_FORM);
    setNotice(null);
  };

  useEffect(() => {
    if (!preset || !loaded) return;
    const existing = items.find((item) => item.endpoint === preset.endpoint);
    if (existing) {
      setEditingId(existing.server_id);
      setForm({
        ...formFor(existing),
        displayName: preset.displayName,
        endpoint: preset.endpoint,
        authKind: preset.authKind,
        credential: "",
      });
      setNotice(null);
    } else {
      setEditingId("new");
      setForm({
        ...EMPTY_FORM,
        displayName: preset.displayName,
        endpoint: preset.endpoint,
        authKind: preset.authKind,
      });
      setNotice(null);
    }
    window.requestAnimationFrame(() => document.querySelector(".ex-mcp-self-service")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }, [loaded, preset?.key]);

  const save = async () => {
    if (!editingId) return;
    const id = editingId;
    setBusy({ id, action: "save" });
    setErrors((current) => ({ ...current, [id]: "" }));
    try {
      const response = id === "new"
        ? await client.createUserMcpServer(requestFor(form), projectId)
        : await client.updateUserMcpServer(id, requestFor(form), projectId);
      upsert(response.server);
      setForm((current) => ({ ...current, credential: "" }));
      setEditingId(null);
      setNotice(response.restart_scheduled
        ? "配置已安全保存，e-Mate 正在重载 Runtime。"
        : "配置已安全保存；重启 e-Mate 后生效。");
    } catch (error) {
      setErrors((current) => ({ ...current, [id]: errorCode(error) }));
    } finally {
      setBusy(null);
    }
  };

  const mutate = async (server: UserMCPServerProjection, action: "test" | "enable" | "disable") => {
    setBusy({ id: server.server_id, action });
    setErrors((current) => ({ ...current, [server.server_id]: "" }));
    try {
      const response = await client.mutateUserMcpServer(server.server_id, action, projectId);
      upsert(response.server);
      setNotice(action === "test"
        ? `真实测试通过，已冻结 ${response.server.tool_count} 个工具。`
        : response.restart_scheduled
          ? `服务已${action === "enable" ? "启用" : "停用"}，e-Mate 正在重载 Runtime。`
          : `服务已${action === "enable" ? "启用" : "停用"}；重启 e-Mate 后生效。`);
    } catch (error) {
      setErrors((current) => ({ ...current, [server.server_id]: errorCode(error) }));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (server: UserMCPServerProjection) => {
    if (confirmDeleteId !== server.server_id) {
      setConfirmDeleteId(server.server_id);
      return;
    }
    setBusy({ id: server.server_id, action: "delete" });
    try {
      await client.deleteUserMcpServer(server.server_id, projectId);
      setItems((current) => current.filter((item) => item.server_id !== server.server_id));
      setConfirmDeleteId(null);
      setNotice("远程 MCP 及其本地凭据已删除。Runtime 重载后完成断开。");
    } catch (error) {
      setErrors((current) => ({ ...current, [server.server_id]: errorCode(error) }));
    } finally {
      setBusy(null);
    }
  };

  const editingServer = editingId && editingId !== "new"
    ? items.find((item) => item.server_id === editingId) ?? null
    : null;
  const invalidEndpoint = Boolean(form.endpoint) && !safeHttpsEndpoint(form.endpoint.trim());
  const bearerMissing = form.authKind === "bearer"
    && !form.credential
    && !editingServer?.credential_configured;
  const saveDisabled = Boolean(busy)
    || !form.displayName.trim()
    || !form.endpoint.trim()
    || invalidEndpoint
    || bearerMissing;

  return (
    <section className="ex-connector-catalog-panel ex-mcp-self-service" aria-label="远程 MCP" aria-busy={Boolean(busy)}>
      <div className="ex-popover-heading">
        <div>
          <strong>远程 MCP</strong>
          <span>连接你自己的公开 HTTPS MCP 服务；工具只有在真实测试后才会启用。</span>
        </div>
        <div className="ex-connector-heading-actions">
          <IconButton label="刷新远程 MCP" disabled={Boolean(busy)} onClick={() => void load()}>
            <RefreshCw className={busy?.action === "load" ? "ex-spin" : ""} aria-hidden="true" />
          </IconButton>
          <button className="ex-button is-primary" type="button" disabled={Boolean(busy) || editingId === "new"} onClick={() => startEdit()}>
            <Plus aria-hidden="true" />添加远程 MCP
          </button>
        </div>
      </div>

      {notice ? (
        <div className="ex-connector-message is-info" role="status">
          <CheckCircle2 aria-hidden="true" /><span>{notice}</span>
          <IconButton label="关闭远程 MCP 提示" onClick={() => setNotice(null)}><X aria-hidden="true" /></IconButton>
        </div>
      ) : null}
      {errors.catalog ? (
        <div className="ex-connector-message is-error" role="alert">
          <AlertCircle aria-hidden="true" />
          <span>{errorMessage(errors.catalog)}<code>错误码：{errors.catalog}</code></span>
          <button className="ex-button" type="button" disabled={Boolean(busy)} onClick={() => void load()}>重新加载</button>
        </div>
      ) : null}

      {editingId ? (
        <form className="ex-channel-configuration-form ex-mcp-configuration-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label>
            <span>显示名称</span>
            <input value={form.displayName} maxLength={128} required disabled={Boolean(busy)} onChange={(event) => setForm((current) => ({ ...current, displayName: event.target.value }))} />
          </label>
          <label>
            <span>HTTPS 地址</span>
            <input type="url" inputMode="url" placeholder="https://mcp.example.com/mcp" value={form.endpoint} maxLength={2048} required disabled={Boolean(busy)} aria-invalid={invalidEndpoint} onChange={(event) => setForm((current) => ({ ...current, endpoint: event.target.value }))} />
            {invalidEndpoint ? <small className="ex-field-error">仅支持不含账号、查询参数或片段的 HTTPS 地址。</small> : null}
          </label>
          <label>
            <span>认证方式</span>
            <select value={form.authKind} disabled={Boolean(busy)} onChange={(event) => setForm((current) => ({ ...current, authKind: event.target.value as UserMCPAuthKind, credential: "" }))}>
              <option value="none">无需认证</option>
              <option value="bearer">Bearer 令牌</option>
              <option value="oauth2">OAuth 2.0</option>
            </select>
          </label>
          {form.authKind === "bearer" ? (
            <label>
              <span>Bearer 令牌{editingServer?.credential_configured ? <small>已配置</small> : null}</span>
              <input type="password" autoComplete="new-password" value={form.credential} required={!editingServer?.credential_configured} disabled={Boolean(busy)} placeholder={editingServer?.credential_configured ? "已安全保存；留空不修改" : "输入令牌"} onChange={(event) => setForm((current) => ({ ...current, credential: event.target.value }))} />
            </label>
          ) : null}
          {form.authKind === "oauth2" ? (
            <>
              <label><span>OAuth Client ID（可选）</span><input value={form.oauthClientId} maxLength={512} disabled={Boolean(busy)} onChange={(event) => setForm((current) => ({ ...current, oauthClientId: event.target.value }))} /></label>
              <label><span>授权范围（空格分隔）</span><input value={form.oauthScope} maxLength={2048} disabled={Boolean(busy)} placeholder="mcp.read mcp.write" onChange={(event) => setForm((current) => ({ ...current, oauthScope: event.target.value }))} /></label>
              <label><span>额外授权域名（逗号分隔）</span><input value={form.authorizationHosts} disabled={Boolean(busy)} placeholder="login.example.com" onChange={(event) => setForm((current) => ({ ...current, authorizationHosts: event.target.value }))} /></label>
            </>
          ) : null}
          <p className="ex-channel-secret-note">令牌只在本次保存时提交，保存后立即清空，页面不会读取或回显。</p>
          {errors[editingId] ? <p className="ex-mcp-error" role="alert">{errorMessage(errors[editingId]!)} <code>错误码：{errors[editingId]}</code></p> : null}
          <div className="ex-channel-configuration-actions">
            <button className="ex-button is-primary" type="submit" disabled={saveDisabled}>
              {busy?.action === "save" ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Save aria-hidden="true" />}
              {busy?.action === "save" ? "正在保存" : "保存配置"}
            </button>
            <button className="ex-button" type="button" disabled={Boolean(busy)} onClick={() => { setEditingId(null); setForm(EMPTY_FORM); }}><X aria-hidden="true" />取消</button>
          </div>
        </form>
      ) : null}

      <div className="ex-connector-list">
        {!loaded && busy?.action === "load" ? <div className="ex-connector-loading" role="status"><LoaderCircle aria-hidden="true" /><span>正在读取远程 MCP…</span></div> : null}
        {loaded && !items.length ? <div className="ex-popover-empty"><Server aria-hidden="true" /><strong>尚未添加远程 MCP</strong><span>点击“添加远程 MCP”，测试通过后即可启用工具。</span></div> : null}
        {items.map((server) => {
          const active = busy?.id === server.server_id;
          const confirming = confirmDeleteId === server.server_id;
          const code = errors[server.server_id];
          const oauth = server.auth_kind === "oauth2" ? oauthStatuses[server.server_id] ?? null : null;
          const oauthRunning = oauthBusy === server.server_id;
          return (
            <article className="ex-connector-row ex-mcp-server-row" key={server.server_id} data-enabled={server.enabled}>
              <span className="ex-connector-icon" aria-hidden="true"><Server /></span>
              <div className="ex-connector-body">
                <div className="ex-connector-title">
                  <strong>{server.display_name}</strong>
                  <span className={`ex-connector-status is-${server.enabled ? "connected" : "disabled"}`}>
                    {server.enabled ? <CheckCircle2 aria-hidden="true" /> : <PowerOff aria-hidden="true" />}
                    {server.enabled ? "已启用" : "未启用"}
                  </span>
                </div>
                <p className="ex-mcp-endpoint">{server.endpoint}</p>
                <div className="ex-mcp-tool-facts">
                  <span>{server.tested_at ? `上次真实测试：${new Date(server.tested_at * 1000).toLocaleString("zh-CN")}` : "尚未测试"}</span>
                  <span>已冻结工具：{server.tool_count}</span>
                  {server.tool_names.length ? <ul>{server.tool_names.map((name) => <li key={name}><code>{name}</code></li>)}</ul> : null}
                </div>
                {server.auth_kind === "oauth2" ? (
                  <div className="ex-mcp-oauth-row" role="status">
                    <span>OAuth 状态：{oauth
                      ? {
                          authorization_required: "待授权",
                          authorizing: "授权中",
                          authorized: "已授权",
                          reauthorization_required: "需要重新授权",
                        }[oauth.state]
                      : "等待 Runtime 重载"}</span>
                    {oauth?.scope ? <span>授权范围：{oauth.scope}</span> : null}
                    <div className="ex-connector-actions">
                      <button className="ex-button" type="button" disabled={Boolean(busy) || Boolean(oauthBusy)} onClick={() => void onRefreshOAuth()}><RefreshCw aria-hidden="true" />刷新授权状态</button>
                      {oauth?.state === "authorized" ? (
                        <button className="ex-button" type="button" disabled={Boolean(busy) || Boolean(oauthBusy)} onClick={() => void onClearOAuth(server.server_id)}>{oauthRunning ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <PowerOff aria-hidden="true" />}{oauthRunning ? "正在取消授权" : "取消授权"}</button>
                      ) : (
                        <button className="ex-button is-primary" type="button" disabled={Boolean(busy) || Boolean(oauthBusy) || !oauth} title={!oauth ? "Runtime 重载后即可开始授权" : undefined} onClick={() => void onBeginOAuth(server.server_id)}>{oauthRunning ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Power aria-hidden="true" />}{oauthRunning ? "等待授权" : oauth?.state === "reauthorization_required" ? "重新授权" : "开始授权"}</button>
                      )}
                    </div>
                  </div>
                ) : null}
                {code ? <p className="ex-mcp-error" role="alert">{errorMessage(code)} <code>错误码：{code}</code></p> : null}
                <div className="ex-connector-actions" aria-label={`${server.display_name} 远程 MCP 操作`}>
                  <button className="ex-button" type="button" disabled={Boolean(busy) || server.enabled} title={server.enabled ? "请先停用，再编辑连接配置" : undefined} onClick={() => startEdit(server)}><Pencil aria-hidden="true" />编辑</button>
                  <button className="ex-button" type="button" disabled={Boolean(busy)} onClick={() => void mutate(server, "test")}>
                    {active && busy.action === "test" ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Play aria-hidden="true" />}
                    {active && busy.action === "test" ? "正在真实测试" : "真实测试"}
                  </button>
                  <button className="ex-button" type="button" disabled={Boolean(busy) || (!server.enabled && !server.tested_at)} title={!server.enabled && !server.tested_at ? "请先完成真实测试" : undefined} onClick={() => void mutate(server, server.enabled ? "disable" : "enable")}>
                    {server.enabled ? <PowerOff aria-hidden="true" /> : <Power aria-hidden="true" />}{server.enabled ? "停用" : "启用"}
                  </button>
                  <button className={`ex-button${confirming ? " is-danger" : ""}`} type="button" disabled={Boolean(busy)} onClick={() => void remove(server)}>
                    {active && busy.action === "delete" ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
                    {active && busy.action === "delete" ? "正在删除" : confirming ? "确认删除并清除凭据" : "删除"}
                  </button>
                  {confirming ? <button className="ex-button" type="button" disabled={Boolean(busy)} onClick={() => setConfirmDeleteId(null)}>取消删除</button> : null}
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
