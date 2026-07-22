import { CircleAlert, Link2, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ChangeEvent } from "react";

import type {
  ConnectorLoginBeginResponse,
  ConnectorLoginCheckResponse,
  InteractionAction,
  InteractionFormField,
  InteractionProjection,
  InteractionResponse,
} from "../api/contracts.ts";
import type { RuntimeClient } from "../api/runtimeClient.ts";
import { safeConnectorAuthorizationUrl } from "../state/connectors.ts";
import { userFacingError } from "../state/userLanguage.ts";

interface InteractionStackProps {
  interactions: InteractionProjection[];
  connectorRuntime: {
    client: Pick<
      RuntimeClient,
      "connectorLoginInteraction"
    >;
    refreshConnectors: () => Promise<unknown>;
    refreshProjection: (threadId: string) => Promise<unknown>;
  };
  onRespond: (
    interactionId: string,
    response: InteractionResponse,
    clientRequestId: string,
  ) => Promise<void>;
}

type DraftValues = Record<string, string | boolean>;
interface PendingResponse {
  fingerprint: string;
  response: InteractionResponse;
  clientRequestId: string;
}

interface ConnectorLoginFlow {
  connectorId: string;
  authorizationUrl: string | null;
  verificationUrl: string | null;
  userCode: string | null;
  expiresAt: string | null;
  message: string;
}

type ConnectorCheckOutcome = "connected" | "pending" | "reauthorize" | "retry";

const CONNECTOR_POLL_INTERVAL_MS = 2_000;
const CONNECTOR_POLL_ATTEMPTS = 45;

function responseFingerprint(response: InteractionResponse): string {
  return JSON.stringify([
    response.action_id,
    Object.entries(response.values).sort(([left], [right]) => left.localeCompare(right)),
  ]);
}

function iconFor(kind: InteractionProjection["kind"]) {
  if (kind === "permission_approval") return ShieldCheck;
  if (kind === "connector_login") return Link2;
  return CircleAlert;
}

function errorMessage(error: unknown): string {
  return userFacingError(error);
}

function connectorIdFor(interaction: InteractionProjection): string {
  if (interaction.kind !== "connector_login" || !interaction.contract.connector) {
    throw new Error("这条连接请求已失效，请重新发起任务。");
  }
  return interaction.contract.connector.connector_id;
}

function validateConnectorIdentity(
  response: { interaction_id: string; connector_id: string },
  interactionId: string,
  connectorId: string,
): void {
  if (
    response.interaction_id !== interactionId
    || response.connector_id !== connectorId
  ) {
    throw new Error("连接状态与当前任务不匹配，已停止继续操作。");
  }
}

function connectorLoginFlow(
  response: ConnectorLoginBeginResponse,
  interactionId: string,
  connectorId: string,
): ConnectorLoginFlow {
  validateConnectorIdentity(response, interactionId, connectorId);
  if (response.state !== "awaiting_callback") {
    throw new Error("连接服务返回了未知状态，请稍后重试。");
  }
  const expiresAt = Date.parse(response.expires_at);
  if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
    throw new Error("登录请求已经过期，请重新打开登录页。");
  }
  const authorizationUrl = response.authorization_url
    ? safeConnectorAuthorizationUrl(response.authorization_url)
    : null;
  const verificationUrl = response.verification_url
    ? safeConnectorAuthorizationUrl(response.verification_url)
    : null;
  if (!authorizationUrl && !verificationUrl) {
    throw new Error("连接服务没有返回可用的登录地址。");
  }
  return {
    connectorId,
    authorizationUrl,
    verificationUrl,
    userCode: typeof response.user_code === "string" && response.user_code.trim()
      ? response.user_code.trim()
      : null,
    expiresAt: response.expires_at,
    message: authorizationUrl
      ? "登录页已打开。完成后回到这里，e-Mate 会自动确认。"
      : "请在验证页完成登录，e-Mate 会自动确认。",
  };
}

function validateConnectorCheck(
  response: ConnectorLoginCheckResponse,
  interactionId: string,
  connectorId: string,
): void {
  validateConnectorIdentity(response, interactionId, connectorId);
  if (
    (response.connected && response.state !== "connected")
    || (
      !response.connected
      && response.state !== "awaiting_callback"
      && response.state !== "authorization_required"
      && response.state !== "reauthorization_required"
    )
  ) {
    throw new Error("连接服务返回了矛盾状态，请稍后重试。");
  }
}

function validateDraft(
  fields: InteractionFormField[],
  action: InteractionAction,
  draft: DraftValues,
): string | null {
  if (!action.submits_form) return null;
  for (const field of fields) {
    const value = draft[field.field_id];
    if (field.control === "checkbox") {
      if (field.required && value !== true) return `请确认“${field.label}”。`;
      continue;
    }
    const text = typeof value === "string" ? value : "";
    if (field.required && !text) return `请填写“${field.label}”。`;
    if (text && (text.length < field.min_length || text.length > field.max_length)) {
      return `“${field.label}”需为 ${field.min_length}–${field.max_length} 个字符。`;
    }
    if (
      text
      && field.control === "select"
      && !field.options.some((option) => option.option_id === text)
    ) return `请重新选择“${field.label}”。`;
  }
  return null;
}

function InteractionField({
  field,
  inputId,
  value,
  disabled,
  onChange,
}: {
  field: InteractionFormField;
  inputId: string;
  value: string | boolean | undefined;
  disabled: boolean;
  onChange: (value: string | boolean) => void;
}) {
  const descriptionId = field.description ? `${inputId}-description` : undefined;
  if (field.control === "checkbox") {
    return (
      <label className="ex-interaction-checkbox">
        <input
          type="checkbox"
          id={inputId}
          checked={value === true}
          required={field.required}
          disabled={disabled}
          aria-describedby={descriptionId}
          onChange={(event) => onChange(event.currentTarget.checked)}
        />
        <span>
          {field.label}
          {field.required ? <span aria-hidden="true"> *</span> : null}
          {field.description
            ? <small id={descriptionId}>{field.description}</small>
            : null}
        </span>
      </label>
    );
  }
  const shared = {
    id: inputId,
    required: field.required,
    disabled,
    "aria-describedby": descriptionId,
    value: typeof value === "string" ? value : "",
    onChange: (
      event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
    ) => onChange(event.currentTarget.value),
  };
  return (
    <label className="ex-interaction-field" htmlFor={inputId}>
      <span>
        {field.label}
        {field.required ? <span aria-hidden="true"> *</span> : null}
      </span>
      {field.control === "textarea" ? (
        <textarea
          {...shared}
          placeholder={field.placeholder ?? undefined}
          minLength={field.min_length}
          maxLength={field.max_length}
          rows={3}
        />
      ) : field.control === "select" ? (
        <select {...shared}>
          <option value="">请选择</option>
          {field.options.map((option) => (
            <option key={option.option_id} value={option.option_id}>
              {option.label}
            </option>
          ))}
        </select>
      ) : (
        <input
          {...shared}
          type="text"
          autoComplete="off"
          placeholder={field.placeholder ?? undefined}
          minLength={field.min_length}
          maxLength={field.max_length}
        />
      )}
      {field.description
        ? <small id={descriptionId}>{field.description}</small>
        : null}
    </label>
  );
}

export function InteractionStack({
  interactions,
  connectorRuntime,
  onRespond,
}: InteractionStackProps) {
  const [responding, setResponding] = useState<{
    interactionId: string;
    actionId: string;
  } | null>(null);
  const [drafts, setDrafts] = useState<Record<string, DraftValues>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [connectorFlows, setConnectorFlows] = useState<Record<string, ConnectorLoginFlow>>({});
  const pendingResponses = useRef(new Map<string, PendingResponse>());
  const connectorPollers = useRef(new Map<string, number>());
  const connectorChecks = useRef(new Map<string, Promise<ConnectorCheckOutcome>>());
  const interactionsRef = useRef(interactions);
  interactionsRef.current = interactions;

  function clearConnectorPoll(interactionId: string): void {
    const timer = connectorPollers.current.get(interactionId);
    if (timer !== undefined) window.clearTimeout(timer);
    connectorPollers.current.delete(interactionId);
  }

  async function refreshConnectorCatalog(): Promise<void> {
    try {
      await connectorRuntime.refreshConnectors();
    } catch {
      // Authority remains server-owned; catalog recovery is independent.
    }
  }

  async function performConnectorCheck(
    interactionId: string,
    connectorId: string,
    threadId: string,
    manual: boolean,
  ): Promise<ConnectorCheckOutcome> {
    try {
      const response = await connectorRuntime.client.connectorLoginInteraction(
        interactionId,
        "check",
      );
      try {
        validateConnectorCheck(response, interactionId, connectorId);
      } catch (error) {
        clearConnectorPoll(interactionId);
        setConnectorFlows((current) => ({
          ...current,
          [interactionId]: {
            connectorId,
            authorizationUrl: null,
            verificationUrl: null,
            userCode: null,
            expiresAt: null,
            message: "连接状态异常，请重新开始登录。",
          },
        }));
        setErrors((current) => ({
          ...current,
          [interactionId]: errorMessage(error),
        }));
        return "reauthorize";
      }
      if (response.connected) {
        clearConnectorPoll(interactionId);
        await refreshConnectorCatalog();
        setConnectorFlows((current) => ({
          ...current,
          [interactionId]: {
            connectorId,
            authorizationUrl: current[interactionId]?.authorizationUrl ?? null,
            verificationUrl: current[interactionId]?.verificationUrl ?? null,
            userCode: current[interactionId]?.userCode ?? null,
            expiresAt: current[interactionId]?.expiresAt ?? null,
            message: "连接已确认，正在用新的权限快照恢复任务…",
          },
        }));
        setErrors((current) => ({ ...current, [interactionId]: "" }));
        try {
          await connectorRuntime.refreshProjection(threadId);
        } catch {
          // SSE remains authoritative and will remove the resolved card.
        }
        return "connected";
      }
      if (response.state !== "awaiting_callback") {
        clearConnectorPoll(interactionId);
        await refreshConnectorCatalog();
        setConnectorFlows((current) => ({
          ...current,
          [interactionId]: {
            connectorId,
            authorizationUrl: null,
            verificationUrl: null,
            userCode: null,
            expiresAt: null,
            message: response.state === "reauthorization_required"
              ? "当前登录缺少所需权限，请重新登录授权。"
              : "登录未完成，请重新开始登录。",
          },
        }));
        setErrors((current) => ({ ...current, [interactionId]: "" }));
        return "reauthorize";
      }
      setConnectorFlows((current) => ({
        ...current,
        [interactionId]: {
          connectorId,
          authorizationUrl: current[interactionId]?.authorizationUrl ?? null,
          verificationUrl: current[interactionId]?.verificationUrl ?? null,
          userCode: current[interactionId]?.userCode ?? null,
          expiresAt: current[interactionId]?.expiresAt ?? null,
          message: "尚未确认连接。完成登录后会自动重试，也可手动检查。",
        },
      }));
      if (manual) setErrors((current) => ({ ...current, [interactionId]: "" }));
      return "pending";
    } catch (error) {
      if (manual) {
        setErrors((current) => ({
          ...current,
          [interactionId]: errorMessage(error),
        }));
      } else {
        setConnectorFlows((current) => current[interactionId] ? ({
          ...current,
          [interactionId]: {
            ...current[interactionId],
            message: "连接状态暂未同步，e-Mate 会继续重试。",
          },
        }) : current);
      }
      return "retry";
    }
  }

  function checkConnectorLogin(
    interactionId: string,
    connectorId: string,
    threadId: string,
    manual: boolean,
  ): Promise<ConnectorCheckOutcome> {
    const existing = connectorChecks.current.get(interactionId);
    if (existing) return existing;
    const pending = performConnectorCheck(interactionId, connectorId, threadId, manual);
    connectorChecks.current.set(interactionId, pending);
    void pending.then(() => {
      if (connectorChecks.current.get(interactionId) === pending) {
        connectorChecks.current.delete(interactionId);
      }
    });
    return pending;
  }

  function scheduleConnectorCheck(
    interactionId: string,
    connectorId: string,
    threadId: string,
    attemptsLeft = CONNECTOR_POLL_ATTEMPTS,
  ): void {
    clearConnectorPoll(interactionId);
    if (attemptsLeft <= 0) {
      setConnectorFlows((current) => current[interactionId] ? ({
        ...current,
        [interactionId]: {
          ...current[interactionId],
          message: "仍在等待登录确认。完成后请点击“检查状态”。",
        },
      }) : current);
      return;
    }
    const timer = window.setTimeout(() => {
      connectorPollers.current.delete(interactionId);
      void checkConnectorLogin(interactionId, connectorId, threadId, false).then((outcome) => {
        const stillPending = interactionsRef.current.some(
          (candidate) => candidate.interaction_id === interactionId,
        );
        if ((outcome === "pending" || outcome === "retry") && stillPending) {
          scheduleConnectorCheck(interactionId, connectorId, threadId, attemptsLeft - 1);
        }
      });
    }, CONNECTOR_POLL_INTERVAL_MS);
    connectorPollers.current.set(interactionId, timer);
  }

  async function beginConnectorLogin(interaction: InteractionProjection): Promise<void> {
    const interactionId = interaction.interaction_id;
    const connectorId = connectorIdFor(interaction);
    clearConnectorPoll(interactionId);
    const popup = window.open("about:blank", "_blank", "popup,width=720,height=780");
    if (popup) {
      try {
        popup.opener = null;
      } catch {
        // The popup remains opaque after cross-origin navigation.
      }
    }
    try {
      const checkInFlight = connectorChecks.current.get(interactionId);
      if (checkInFlight && await checkInFlight === "connected") {
        popup?.close();
        return;
      }
      const response = await connectorRuntime.client.connectorLoginInteraction(
        interactionId,
        "begin",
      );
      const flow = connectorLoginFlow(response, interactionId, connectorId);
      let popupUnavailable = false;
      if (flow.authorizationUrl && popup) {
        try {
          popup.location.replace(flow.authorizationUrl);
          popup.focus();
        } catch {
          popupUnavailable = true;
          popup.close();
        }
      } else {
        popup?.close();
        popupUnavailable = Boolean(flow.authorizationUrl);
      }
      setConnectorFlows((current) => ({ ...current, [interactionId]: flow }));
      setErrors((current) => ({
        ...current,
        [interactionId]: popupUnavailable
          ? "浏览器未能打开登录页，请使用下方安全链接继续。"
          : "",
      }));
      scheduleConnectorCheck(interactionId, connectorId, interaction.thread_id);
    } catch (error) {
      popup?.close();
      setErrors((current) => ({
        ...current,
        [interactionId]: errorMessage(error),
      }));
    }
  }

  async function cancelConnectorLogin(interaction: InteractionProjection): Promise<void> {
    const interactionId = interaction.interaction_id;
    const connectorId = connectorIdFor(interaction);
    clearConnectorPoll(interactionId);
    try {
      const response = await connectorRuntime.client.connectorLoginInteraction(
        interactionId,
        "cancel",
      );
      validateConnectorIdentity(response, interactionId, connectorId);
      if (response.cancelled !== true) {
        throw new Error("连接服务未确认取消，请重试。");
      }
      setConnectorFlows((current) => ({
        ...current,
        [interactionId]: {
          connectorId,
          authorizationUrl: null,
          verificationUrl: null,
          userCode: null,
          expiresAt: null,
          message: "登录已安全取消，正在同步任务状态…",
        },
      }));
      setErrors((current) => ({ ...current, [interactionId]: "" }));
      try {
        await connectorRuntime.refreshProjection(interaction.thread_id);
      } catch {
        setErrors((current) => ({
          ...current,
          [interactionId]: "登录已取消，任务状态会在连接恢复后自动同步。",
        }));
      }
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [interactionId]: errorMessage(error),
      }));
    }
  }

  useEffect(() => {
    const active = new Set(interactions.map((interaction) => interaction.interaction_id));
    for (const interactionId of connectorPollers.current.keys()) {
      if (!active.has(interactionId)) clearConnectorPoll(interactionId);
    }
  }, [interactions]);

  useEffect(() => () => {
    for (const timer of connectorPollers.current.values()) window.clearTimeout(timer);
    connectorPollers.current.clear();
  }, []);

  if (!interactions.length) return null;
  return (
    <section className="ex-interaction-stack" aria-label="等待你的操作">
      {interactions.map((interaction) => {
        const Icon = iconFor(interaction.kind);
        const busy = responding?.interactionId === interaction.interaction_id;
        const draft = drafts[interaction.interaction_id] ?? {};
        const connectorFlow = connectorFlows[interaction.interaction_id];
        const errorId = `${interaction.interaction_id}-error`;
        return (
          <article
            className="ex-interaction"
            key={interaction.interaction_id}
            aria-busy={busy}
            aria-describedby={errors[interaction.interaction_id] ? errorId : undefined}
          >
            <Icon aria-hidden="true" />
            <div className="ex-interaction-copy">
              <strong>{interaction.contract.title}</strong>
              <p>{interaction.prompt}</p>
              {interaction.contract.connector ? (
                <p className="ex-interaction-connector-status" role="status">
                  {interaction.contract.connector.display_name} · {
                    interaction.contract.connector.state === "awaiting_callback"
                      ? "等待登录确认"
                      : interaction.contract.connector.state === "verifying"
                        ? "正在检查连接"
                        : "需要安全登录"
                  }
                </p>
              ) : null}
              {connectorFlow ? (
                <div className="ex-connector-login-flow" role="status" aria-live="polite">
                  <p>{connectorFlow.message}</p>
                  {connectorFlow.userCode ? (
                    <p className="ex-connector-login-code">
                      验证码 <code>{connectorFlow.userCode}</code>
                    </p>
                  ) : null}
                  <div className="ex-connector-login-links">
                    {connectorFlow.authorizationUrl ? (
                      <a
                        className="ex-button"
                        href={connectorFlow.authorizationUrl}
                        target="_blank"
                        rel="noreferrer noopener"
                      >
                        打开登录页
                      </a>
                    ) : null}
                    {connectorFlow.verificationUrl ? (
                      <a
                        className="ex-button"
                        href={connectorFlow.verificationUrl}
                        target="_blank"
                        rel="noreferrer noopener"
                      >
                        打开验证页
                      </a>
                    ) : null}
                  </div>
                </div>
              ) : null}
              {interaction.contract.fields.length ? (
                <fieldset className="ex-interaction-fields" disabled={busy}>
                  <legend className="ex-visually-hidden">{interaction.contract.title}</legend>
                  {interaction.contract.fields.map((field) => (
                    <InteractionField
                      key={field.field_id}
                      field={field}
                      inputId={`${interaction.interaction_id}-${field.field_id}`}
                      value={draft[field.field_id]}
                      disabled={busy}
                      onChange={(value) => {
                        setDrafts((current) => ({
                          ...current,
                          [interaction.interaction_id]: {
                            ...(current[interaction.interaction_id] ?? {}),
                            [field.field_id]: value,
                          },
                        }));
                        setErrors((current) => ({
                          ...current,
                          [interaction.interaction_id]: "",
                        }));
                      }}
                    />
                  ))}
                </fieldset>
              ) : null}
              {errors[interaction.interaction_id] ? (
                <p className="ex-interaction-error" id={errorId} role="alert">
                  {errors[interaction.interaction_id]}
                </p>
              ) : null}
              <div className="ex-interaction-actions">
                {interaction.contract.actions.map((action) => (
                  <button
                    className="ex-button ex-interaction-action"
                    data-style={action.style}
                    type="button"
                    key={action.action_id}
                    disabled={responding !== null}
                    onClick={async () => {
                      if (responding !== null) return;
                      if (interaction.kind === "connector_login") {
                        setResponding({
                          interactionId: interaction.interaction_id,
                          actionId: action.action_id,
                        });
                        setErrors((current) => ({
                          ...current,
                          [interaction.interaction_id]: "",
                        }));
                        try {
                          const connectorId = connectorIdFor(interaction);
                          if (action.action_type === "connector_begin_login") {
                            await beginConnectorLogin(interaction);
                          } else if (action.action_type === "connector_check_status") {
                            clearConnectorPoll(interaction.interaction_id);
                            const outcome = await checkConnectorLogin(
                              interaction.interaction_id,
                              connectorId,
                              interaction.thread_id,
                              true,
                            );
                            if (outcome === "pending" || outcome === "retry") {
                              scheduleConnectorCheck(
                                interaction.interaction_id,
                                connectorId,
                                interaction.thread_id,
                              );
                            }
                          } else if (action.action_type === "cancel") {
                            await cancelConnectorLogin(interaction);
                          } else {
                            setErrors((current) => ({
                              ...current,
                              [interaction.interaction_id]:
                                "当前版本不支持这项连接操作，请刷新后重试。",
                            }));
                          }
                        } catch (error) {
                          setErrors((current) => ({
                            ...current,
                            [interaction.interaction_id]: errorMessage(error),
                          }));
                        } finally {
                          setResponding(null);
                        }
                        return;
                      }
                      const validationError = validateDraft(
                        interaction.contract.fields,
                        action,
                        draft,
                      );
                      if (validationError) {
                        setErrors((current) => ({
                          ...current,
                          [interaction.interaction_id]: validationError,
                        }));
                        return;
                      }
                      setResponding({
                        interactionId: interaction.interaction_id,
                        actionId: action.action_id,
                      });
                      setErrors((current) => ({
                        ...current,
                        [interaction.interaction_id]: "",
                      }));
                      try {
                        const response: InteractionResponse = {
                          action_id: action.action_id,
                          values: action.submits_form ? draft : {},
                        };
                        const fingerprint = responseFingerprint(response);
                        const existing = pendingResponses.current.get(
                          interaction.interaction_id,
                        );
                        if (existing && existing.fingerprint !== fingerprint) {
                          throw new Error(
                            "上一次提交结果尚未确认，请保持原选择并重试。",
                          );
                        }
                        const pending = existing ?? {
                          fingerprint,
                          response,
                          clientRequestId: `interaction_${crypto.randomUUID().replaceAll("-", "")}`,
                        };
                        pendingResponses.current.set(interaction.interaction_id, pending);
                        await onRespond(
                          interaction.interaction_id,
                          pending.response,
                          pending.clientRequestId,
                        );
                        pendingResponses.current.delete(interaction.interaction_id);
                      } catch (error) {
                        if (
                          typeof error === "object"
                          && error !== null
                          && "status" in error
                          && error.status === 422
                        ) {
                          pendingResponses.current.delete(interaction.interaction_id);
                        }
                        setErrors((current) => ({
                          ...current,
                          [interaction.interaction_id]: errorMessage(error),
                        }));
                      } finally {
                        setResponding(null);
                      }
                    }}
                  >
                    {busy && responding?.actionId === action.action_id
                      ? action.action_type === "connector_begin_login"
                        ? "正在打开…"
                        : action.action_type === "connector_check_status"
                          ? "正在检查…"
                          : interaction.kind === "connector_login"
                            && action.action_type === "cancel"
                            ? "正在取消…"
                          : "正在提交…"
                      : action.label}
                  </button>
                ))}
              </div>
            </div>
          </article>
        );
      })}
    </section>
  );
}
