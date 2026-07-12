import { CheckCircle2, ExternalLink, LoaderCircle, LogIn, RefreshCw, RotateCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { DeviceLoginProjection } from "../api/contracts.ts";
import { createClientRequestId } from "../api/runtimeClient.ts";
import {
  devicePollSeconds,
  deviceStatusRefreshDelay,
  safeDeviceVerificationUrl,
} from "../state/deviceLogin.ts";
import {
  serviceReasonMessage,
  technicalErrorCode,
  userFacingError,
} from "../state/userLanguage.ts";
import { TechnicalDetails } from "./TechnicalDetails.tsx";

interface DeviceLoginCardProps {
  unavailableReason: string;
  serviceState: "ready" | "unavailable";
  serviceReason: string | null;
  onBegin: (clientRequestId?: string) => Promise<DeviceLoginProjection>;
  onGet: (flowId: string, signal?: AbortSignal) => Promise<DeviceLoginProjection>;
  onPoll: (flowId: string, clientRequestId?: string) => Promise<DeviceLoginProjection>;
}

function errorMessage(error: unknown): string {
  return userFacingError(error);
}

export function DeviceLoginCard({
  unavailableReason,
  serviceState,
  serviceReason,
  onBegin,
  onGet,
  onPoll,
}: DeviceLoginCardProps) {
  const [flow, setFlow] = useState<DeviceLoginProjection | null>(null);
  const [busy, setBusy] = useState<"begin" | "refresh" | "poll" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const busyRef = useRef<typeof busy>(null);
  const beginRequestId = useRef<string | null>(null);
  const pollRequestId = useRef<string | null>(null);
  const reloadTimer = useRef<number | null>(null);
  const verificationUrl = useMemo(
    () => flow ? safeDeviceVerificationUrl(flow.verification_url) : null,
    [flow],
  );
  const secondsToPoll = flow ? devicePollSeconds(flow.next_poll_at, now) : 0;

  const setOperation = useCallback((operation: typeof busy) => {
    busyRef.current = operation;
    setBusy(operation);
  }, []);

  const accept = useCallback((next: DeviceLoginProjection) => {
    setFlow(next);
    const safeUrl = safeDeviceVerificationUrl(next.verification_url);
    setError(safeUrl ? null : "验证地址未通过安全检查，EcoreX 已停止打开。请联系管理员。");
    setErrorCode(safeUrl ? null : "unsafe_device_verification_url");
    return next;
  }, []);

  const begin = useCallback(async () => {
    if (busyRef.current) return;
    const requestId = beginRequestId.current ?? createClientRequestId("device_login");
    beginRequestId.current = requestId;
    setOperation("begin");
    setError(null);
    setErrorCode(null);
    try {
      accept(await onBegin(requestId));
    } catch (beginError) {
      setError(errorMessage(beginError));
      setErrorCode(technicalErrorCode(beginError));
    } finally {
      setOperation(null);
    }
  }, [accept, onBegin, setOperation]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!flow || busyRef.current) return;
    setOperation("refresh");
    try {
      const current = accept(await onGet(flow.flow_id, signal));
      if (current.status === "authorized" && current.restart_required && !current.restart_scheduled) {
        const requestId = pollRequestId.current ?? createClientRequestId("device_poll");
        pollRequestId.current = requestId;
        accept(await onPoll(current.flow_id, requestId));
        pollRequestId.current = null;
      }
    } catch (refreshError) {
      if (!(refreshError instanceof DOMException && refreshError.name === "AbortError")) {
        setError(errorMessage(refreshError));
        setErrorCode(technicalErrorCode(refreshError));
      }
    } finally {
      setOperation(null);
    }
  }, [accept, flow, onGet, onPoll, setOperation]);

  const poll = useCallback(async () => {
    if (!flow || flow.status !== "pending" || busyRef.current || secondsToPoll > 0) return;
    const requestId = pollRequestId.current ?? createClientRequestId("device_poll");
    pollRequestId.current = requestId;
    setOperation("poll");
    setError(null);
    setErrorCode(null);
    try {
      accept(await onPoll(flow.flow_id, requestId));
      pollRequestId.current = null;
    } catch (pollError) {
      setError(errorMessage(pollError));
      setErrorCode(technicalErrorCode(pollError));
    } finally {
      setOperation(null);
    }
  }, [accept, flow, onPoll, secondsToPoll, setOperation]);

  useEffect(() => {
    if (flow?.status !== "pending") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [flow?.status]);

  useEffect(() => {
    if (!flow || flow.status !== "pending") return;
    const controller = new AbortController();
    const delay = deviceStatusRefreshDelay(
      flow.next_poll_at,
      flow.poll_interval_seconds,
      Date.now(),
    );
    const timer = window.setTimeout(() => void refresh(controller.signal), delay);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [flow, refresh]);

  useEffect(() => {
    if (flow?.status !== "authorized" || !flow.restart_scheduled || reloadTimer.current !== null) return;
    reloadTimer.current = window.setTimeout(() => window.location.reload(), 2_500);
    return () => {
      if (reloadTimer.current !== null) window.clearTimeout(reloadTimer.current);
      reloadTimer.current = null;
    };
  }, [flow?.restart_scheduled, flow?.status]);

  const restart = () => {
    setFlow(null);
    setError(null);
    setErrorCode(null);
    setOperation(null);
    beginRequestId.current = null;
    pollRequestId.current = null;
  };

  return (
    <section className="ex-device-login" aria-labelledby="ex-device-login-title">
      <LogIn aria-hidden="true" />
      <div className="ex-device-login-body">
        <strong id="ex-device-login-title">需要登录 EcoreX 账号</strong>
        {!flow ? (
          <>
            <p>
              {serviceState === "ready"
                ? unavailableReason
                : serviceReasonMessage(
                    serviceReason,
                    "暂时无法开始登录。请检查网络；仍无法使用时请联系管理员。",
                  )}
            </p>
            {serviceState !== "ready" ? (
              <TechnicalDetails entries={[
                { label: "服务状态", value: serviceReason },
              ]} />
            ) : null}
            {serviceState === "ready" ? (
              <button
                className="ex-button is-primary"
                type="button"
                disabled={busy !== null}
                aria-busy={busy === "begin"}
                onClick={() => void begin()}
              >
                {busy === "begin" ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <LogIn aria-hidden="true" />}
                {busy === "begin" ? "正在开始" : "开始登录"}
              </button>
            ) : null}
          </>
        ) : flow.status === "pending" ? (
          <>
            <p>在安全验证页输入下方代码。登录信息会直接进入安全存储，不会显示在聊天记录中。</p>
            <div className="ex-device-login-code">
              <span>一次性代码</span>
              <code tabIndex={0}>{flow.user_code}</code>
            </div>
            <div className="ex-device-login-actions">
              {verificationUrl ? (
                <a className="ex-button is-primary" href={verificationUrl} target="_blank" rel="noreferrer">
                  <ExternalLink aria-hidden="true" />
                  打开验证页
                </a>
              ) : null}
              <button
                className="ex-button"
                type="button"
                disabled={busy !== null || secondsToPoll > 0}
                aria-busy={busy === "poll"}
                onClick={() => void poll()}
              >
                {busy === "poll" ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <RefreshCw aria-hidden="true" />}
                {secondsToPoll > 0 ? `${secondsToPoll} 秒后检查` : busy === "poll" ? "正在检查" : "检查授权"}
              </button>
              <button
                className="ex-button"
                type="button"
                disabled={busy !== null}
                aria-busy={busy === "refresh"}
                onClick={() => void refresh()}
              >
                <RotateCw aria-hidden="true" />
                刷新状态
              </button>
            </div>
            <p className="ex-device-login-status" aria-live="polite">
              等待授权 · 代码有效至 {new Intl.DateTimeFormat("zh-CN", { timeStyle: "short" }).format(new Date(flow.expires_at))}
            </p>
          </>
        ) : flow.status === "authorized" ? (
          <div className="ex-device-login-terminal is-success" role="status">
            <CheckCircle2 aria-hidden="true" />
            <div>
              <strong>登录已完成</strong>
              <p>
                {flow.restart_scheduled
                  ? "EcoreX 正在重新载入账号，页面会自动刷新。"
                  : flow.restart_required
                    ? "登录信息已安全保存。请关闭并重新打开 EcoreX 以载入账号。"
                    : "正在载入账号。"}
              </p>
            </div>
          </div>
        ) : (
          <div className="ex-device-login-terminal is-error" role="alert">
            <div>
              <strong>{flow.status === "denied" ? "登录已拒绝" : flow.status === "expired" ? "登录代码已过期" : "登录未完成"}</strong>
              <p>{flow.status === "denied"
                ? "授权没有完成。你可以确认账号后重新开始。"
                : flow.status === "expired"
                  ? "一次性代码已失效，请获取新代码。"
                  : serviceReasonMessage(flow.error_code, "请重新开始登录。")}</p>
              <TechnicalDetails entries={[
                { label: "错误代码", value: flow.error_code },
              ]} />
            </div>
            <button className="ex-button" type="button" onClick={restart}>重新开始</button>
          </div>
        )}
        {error ? (
          <>
            <p className="ex-device-login-error" role="alert">{error}</p>
            <TechnicalDetails entries={[
              { label: "错误代码", value: errorCode },
            ]} />
          </>
        ) : null}
      </div>
    </section>
  );
}
