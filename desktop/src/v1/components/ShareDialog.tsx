import * as Dialog from "@radix-ui/react-dialog";
import {
  Ban,
  Check,
  Copy,
  ExternalLink,
  Link2,
  LoaderCircle,
  RefreshCw,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type {
  ShareListResponse,
  ShareSnapshotProjection,
  ShareStatus,
  ThreadProjection,
} from "../api/contracts.ts";
import { createClientRequestId } from "../api/runtimeClient.ts";
import { writeShareUrl } from "../state/shares.ts";
import {
  serviceReasonMessage,
  technicalErrorCode,
  userFacingError,
} from "../state/userLanguage.ts";
import { IconButton } from "./IconButton.tsx";
import { TechnicalDetails } from "./TechnicalDetails.tsx";

type CopyState = "idle" | "copying" | "copied" | "error";

interface ShareDialogProps {
  open: boolean;
  thread: ThreadProjection | null;
  serviceState: "ready" | "unavailable";
  unavailableReason: string | null;
  onOpenChange: (open: boolean) => void;
  onList: (threadId: string, signal?: AbortSignal) => Promise<ShareListResponse>;
  onCreate: (
    threadId: string,
    expiresInHours: number,
    clientRequestId?: string,
  ) => Promise<ShareSnapshotProjection>;
  onGet: (shareId: string, signal?: AbortSignal) => Promise<ShareSnapshotProjection>;
  onRevoke: (
    shareId: string,
    clientRequestId?: string,
  ) => Promise<ShareSnapshotProjection>;
}

const STATUS_LABELS: Record<ShareStatus, string> = {
  publishing: "正在创建",
  published: "可访问",
  failed: "创建失败",
  revoking: "正在撤销",
  revoked: "已撤销",
  expired: "已过期",
};

function formatError(error: unknown): string {
  return userFacingError(error);
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function replaceShare(
  shares: ShareSnapshotProjection[],
  next: ShareSnapshotProjection,
): ShareSnapshotProjection[] {
  const existing = shares.findIndex((share) => share.share_id === next.share_id);
  if (existing < 0) return [next, ...shares];
  return shares.map((share, index) => index === existing ? next : share);
}

export function ShareDialog({
  open,
  thread,
  serviceState,
  unavailableReason,
  onOpenChange,
  onList,
  onCreate,
  onGet,
  onRevoke,
}: ShareDialogProps) {
  const [shares, setShares] = useState<ShareSnapshotProjection[]>([]);
  const [loadState, setLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [expiresInHours, setExpiresInHours] = useState(24 * 7);
  const [creating, setCreating] = useState(false);
  const [revokingShareId, setRevokingShareId] = useState<string | null>(null);
  const [confirmingRevokeId, setConfirmingRevokeId] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<Record<string, CopyState>>({});
  const [copyError, setCopyError] = useState<Record<string, string>>({});
  const expiresSelectRef = useRef<HTMLSelectElement>(null);
  const pendingCreate = useRef<{ fingerprint: string; clientRequestId: string } | null>(null);
  const pendingRevokes = useRef(new Map<string, string>());
  const copyTimers = useRef(new Map<string, number>());

  const loadShares = useCallback(async (signal?: AbortSignal) => {
    if (!thread || serviceState !== "ready") return;
    setLoadState("loading");
    setError(null);
    setErrorCode(null);
    try {
      const response = await onList(thread.thread_id, signal);
      if (signal?.aborted) return;
      setShares(response.items);
      setLoadState("ready");
    } catch (loadError) {
      if (loadError instanceof DOMException && loadError.name === "AbortError") return;
      setLoadState("error");
      setError(formatError(loadError));
      setErrorCode(technicalErrorCode(loadError));
    }
  }, [onList, serviceState, thread]);

  useEffect(() => {
    if (!open) {
      for (const timer of copyTimers.current.values()) window.clearTimeout(timer);
      copyTimers.current.clear();
      return;
    }
    setShares([]);
    setConfirmingRevokeId(null);
    setCopyState({});
    setCopyError({});
    if (!thread || serviceState !== "ready") {
      setLoadState("idle");
      return;
    }
    const controller = new AbortController();
    void loadShares(controller.signal);
    return () => controller.abort();
  }, [loadShares, open, serviceState, thread?.thread_id]);

  useEffect(() => {
    if (!open || !shares.some((share) => (
      share.status === "publishing" || share.status === "revoking"
    ))) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      const pending = shares.filter((share) => (
        share.status === "publishing" || share.status === "revoking"
      ));
      void Promise.all(pending.map((share) => onGet(share.share_id, controller.signal)))
        .then((updates) => {
          if (controller.signal.aborted) return;
          setShares((current) => updates.reduce(replaceShare, current));
        })
        .catch((pollError) => {
          if (!controller.signal.aborted) {
            setError(formatError(pollError));
            setErrorCode(technicalErrorCode(pollError));
          }
        });
    }, 2_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [onGet, open, shares]);

  useEffect(() => () => {
    for (const timer of copyTimers.current.values()) window.clearTimeout(timer);
    copyTimers.current.clear();
  }, []);

  const create = async () => {
    if (!thread || serviceState !== "ready" || creating) return;
    const fingerprint = `${thread.thread_id}:${expiresInHours}`;
    const pending = pendingCreate.current?.fingerprint === fingerprint
      ? pendingCreate.current
      : {
          fingerprint,
          clientRequestId: createClientRequestId("create_share"),
        };
    pendingCreate.current = pending;
    setCreating(true);
    setError(null);
    setErrorCode(null);
    try {
      const created = await onCreate(
        thread.thread_id,
        expiresInHours,
        pending.clientRequestId,
      );
      pendingCreate.current = null;
      setShares((current) => replaceShare(current, created));
      setLoadState("ready");
    } catch (createError) {
      setError(formatError(createError));
      setErrorCode(technicalErrorCode(createError));
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (share: ShareSnapshotProjection) => {
    if (revokingShareId) return;
    const clientRequestId = pendingRevokes.current.get(share.share_id)
      ?? createClientRequestId("revoke_share");
    pendingRevokes.current.set(share.share_id, clientRequestId);
    setRevokingShareId(share.share_id);
    setConfirmingRevokeId(null);
    setError(null);
    setErrorCode(null);
    try {
      const revoked = await onRevoke(share.share_id, clientRequestId);
      pendingRevokes.current.delete(share.share_id);
      setShares((current) => replaceShare(current, revoked));
    } catch (revokeError) {
      setError(formatError(revokeError));
      setErrorCode(technicalErrorCode(revokeError));
    } finally {
      setRevokingShareId(null);
    }
  };

  const copy = async (share: ShareSnapshotProjection) => {
    if (!share.public_url || copyState[share.share_id] === "copying") return;
    copyTimers.current.get(share.share_id) && window.clearTimeout(
      copyTimers.current.get(share.share_id),
    );
    setCopyState((current) => ({ ...current, [share.share_id]: "copying" }));
    setCopyError((current) => ({ ...current, [share.share_id]: "" }));
    try {
      await writeShareUrl(share.public_url, navigator.clipboard);
      setCopyState((current) => ({ ...current, [share.share_id]: "copied" }));
      const timer = window.setTimeout(() => {
        setCopyState((current) => ({ ...current, [share.share_id]: "idle" }));
        copyTimers.current.delete(share.share_id);
      }, 2_500);
      copyTimers.current.set(share.share_id, timer);
    } catch {
      setCopyState((current) => ({ ...current, [share.share_id]: "error" }));
      setCopyError((current) => ({
        ...current,
        [share.share_id]: "未能自动复制。请手动选择上方链接并复制。",
      }));
    }
  };

  const unavailable = serviceState !== "ready";

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="ex-dialog-overlay" />
        <Dialog.Content
          className="ex-dialog ex-share-dialog"
          aria-describedby="ex-share-description"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            expiresSelectRef.current?.focus();
          }}
        >
          <div className="ex-dialog-heading">
            <div>
              <Dialog.Title>分享任务</Dialog.Title>
              <Dialog.Description id="ex-share-description">
                链接只包含创建时已有的内容；之后的新消息不会自动加入。
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <IconButton label="关闭分享">
                <X aria-hidden="true" />
              </IconButton>
            </Dialog.Close>
          </div>

          {unavailable ? (
            <section className="ex-share-unavailable" role="status">
              <Ban aria-hidden="true" />
              <div>
                <strong>分享服务不可用</strong>
                <span>{serviceReasonMessage(
                  unavailableReason,
                  "暂时无法创建分享链接，请稍后重试。",
                )}</span>
                <TechnicalDetails entries={[
                  { label: "服务状态", value: unavailableReason },
                ]} />
              </div>
            </section>
          ) : (
            <>
              <section className="ex-share-create" aria-label="创建分享">
                <label htmlFor="ex-share-expiry">链接有效期</label>
                <div>
                  <select
                    ref={expiresSelectRef}
                    id="ex-share-expiry"
                    value={expiresInHours}
                    disabled={creating}
                    onChange={(event) => setExpiresInHours(Number(event.target.value))}
                  >
                    <option value={24}>24 小时</option>
                    <option value={24 * 7}>7 天</option>
                    <option value={24 * 30}>30 天</option>
                  </select>
                  <button
                    className="ex-button is-primary"
                    type="button"
                    disabled={!thread || creating}
                    aria-busy={creating}
                    onClick={() => void create()}
                  >
                    {creating ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Link2 aria-hidden="true" />}
                    {creating ? "正在创建" : "创建新链接"}
                  </button>
                </div>
              </section>

              {error ? (
                <section className="ex-share-error" role="alert">
                  <div>
                    <span>{error}</span>
                    <TechnicalDetails entries={[
                      { label: "错误代码", value: errorCode },
                    ]} />
                  </div>
                  <button className="ex-button" type="button" onClick={() => void loadShares()}>
                    <RefreshCw aria-hidden="true" />
                    重新加载
                  </button>
                </section>
              ) : null}

              <section className="ex-share-list" aria-label="已有分享" aria-busy={loadState === "loading"}>
                {loadState === "loading" ? (
                  <div className="ex-share-empty" role="status">
                    <LoaderCircle className="ex-spin" aria-hidden="true" />
                    <span>正在加载分享记录…</span>
                  </div>
                ) : shares.length === 0 ? (
                  <div className="ex-share-empty">
                    <Link2 aria-hidden="true" />
                    <strong>还没有分享链接</strong>
                    <span>选择有效期后，为当前内容创建一个单独链接。</span>
                  </div>
                ) : shares.map((share) => {
                  const state = copyState[share.share_id] ?? "idle";
                  const canCopy = share.status === "published" && Boolean(share.public_url);
                  const isRevoking = revokingShareId === share.share_id;
                  return (
                    <article className="ex-share-row" key={share.share_id}>
                      <div className="ex-share-row-heading">
                        <span className={`ex-share-status is-${share.status}`}>
                          {STATUS_LABELS[share.status]}
                        </span>
                        <time dateTime={share.expires_at}>到期：{formatDate(share.expires_at)}</time>
                      </div>
                      {share.public_url ? (
                        <label className="ex-share-url">
                          <span>分享链接</span>
                          <input value={share.public_url} readOnly onFocus={(event) => event.currentTarget.select()} />
                        </label>
                      ) : (
                        <>
                          <p className="ex-share-code">
                            {share.status === "failed"
                              ? serviceReasonMessage(
                                  share.error_code,
                                  "链接未创建成功，你可以稍后再试。",
                                )
                              : "链接正在准备，请稍候。"}
                          </p>
                          <TechnicalDetails entries={[
                            { label: "分享 ID", value: share.share_id },
                            { label: "错误代码", value: share.error_code },
                          ]} />
                        </>
                      )}
                      <div className="ex-share-actions">
                        <button
                          className="ex-button"
                          type="button"
                          disabled={!canCopy || state === "copying"}
                          data-state={state}
                          onClick={() => void copy(share)}
                        >
                          {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                          {state === "copying" ? "正在复制" : state === "copied" ? "已复制" : "复制链接"}
                        </button>
                        {share.public_url ? (
                          <a className="ex-button" href={share.public_url} target="_blank" rel="noreferrer">
                            <ExternalLink aria-hidden="true" />
                            打开链接
                          </a>
                        ) : null}
                        {share.status === "published" ? (
                          confirmingRevokeId === share.share_id ? (
                            <span className="ex-share-revoke-confirm">
                              <button className="ex-button" type="button" onClick={() => setConfirmingRevokeId(null)}>
                                取消
                              </button>
                              <button
                                className="ex-button is-danger"
                                type="button"
                                disabled={isRevoking}
                                onClick={() => void revoke(share)}
                              >
                                {isRevoking ? "正在撤销" : "确认撤销"}
                              </button>
                            </span>
                          ) : (
                            <button
                              className="ex-button is-danger"
                              type="button"
                              onClick={() => setConfirmingRevokeId(share.share_id)}
                            >
                              <Ban aria-hidden="true" />
                              撤销链接
                            </button>
                          )
                        ) : null}
                      </div>
                      {state === "error" ? (
                        <p className="ex-share-copy-error" role="alert">{copyError[share.share_id]}</p>
                      ) : state === "copied" ? (
                        <p className="ex-share-copy-success" aria-live="polite">链接已写入剪贴板。</p>
                      ) : null}
                    </article>
                  );
                })}
              </section>
            </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
