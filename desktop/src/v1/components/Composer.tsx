import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Tooltip from "@radix-ui/react-tooltip";
import { ChevronDown, FileText, LoaderCircle, Plus, Send, ShieldCheck, Square } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState } from "react";

import type { SendDisposition } from "../state/useRuntimeSession.ts";
import type {
  ConnectorCatalogItem,
  ConnectorInstanceProjection,
  ConversationUsageProjection,
  InputAttachmentProjection,
  ModelDescriptor,
} from "../api/contracts.ts";
import type {
  ConnectorCatalogLoadState,
  ConnectorOperationState,
} from "../state/connectors.ts";
import { IconButton } from "./IconButton.tsx";
import { InputAttachmentPreview, type InputAttachmentBlobLoader } from "./InputAttachmentPreview.tsx";

const loadConnectorPopover = () => import("./ConnectorPopover.tsx");
const loadComposerModelSelector = () => import("./ComposerModelSelector.tsx");
const ConnectorPopover = lazy(async () => ({
  default: (await loadConnectorPopover()).ConnectorPopover,
}));
const ComposerModelSelector = lazy(loadComposerModelSelector);

interface ComposerProps {
  prefillRequest?: { key: string; text: string } | null;
  draft: string;
  onDraftChange: (draft: string) => void;
  connectors: ConnectorCatalogItem[];
  connectorLoadState: ConnectorCatalogLoadState;
  connectorError: string | null;
  connectorNotice: string | null;
  connectorOperations: Record<string, ConnectorOperationState>;
  onRefreshConnectors: () => Promise<unknown>;
  onConnectConnector: (item: ConnectorCatalogItem) => Promise<boolean>;
  onReconnectConnector: (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => Promise<boolean>;
  onCheckConnector: (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => Promise<boolean>;
  onDisconnectConnector: (
    item: ConnectorCatalogItem,
    instance: ConnectorInstanceProjection,
  ) => Promise<boolean>;
  onClearConnectorError: () => void;
  onClearConnectorNotice: () => void;
  active: boolean;
  submitting: boolean;
  modelAvailable: boolean;
  sendUnavailableReason: string | null;
  chatModels: ModelDescriptor[];
  imageModels: ModelDescriptor[];
  chatModel: string;
  imageModel: string;
  quota: {
    remaining: number | null;
    unit: string;
    resets_at: string | null;
    limits: Record<string, number>;
  } | null;
  usage: ConversationUsageProjection | null;
  permissionLabel: string;
  permissionDescription: string;
  onChatModelChange: (modelId: string) => void;
  onImageModelChange: (modelId: string) => void;
  onOpenPermissionSettings: () => void;
  onSend: (
    input: string,
    disposition: SendDisposition,
    attachments: readonly InputAttachmentProjection[],
  ) => Promise<boolean>;
  onUploadAttachment: (file: File) => Promise<InputAttachmentProjection | null>;
  onLoadAttachment: InputAttachmentBlobLoader;
  onLoadAttachmentThumbnail: InputAttachmentBlobLoader;
  onInterrupt: () => void;
}

const dispositionLabel: Record<SendDisposition, string> = {
  steer: "追加到当前任务",
  queue: "排到下一轮",
  replace: "替换当前任务",
};

export function Composer({
  prefillRequest = null,
  draft,
  onDraftChange,
  connectors,
  connectorLoadState,
  connectorError,
  connectorNotice,
  connectorOperations,
  onRefreshConnectors,
  onConnectConnector,
  onReconnectConnector,
  onCheckConnector,
  onDisconnectConnector,
  onClearConnectorError,
  onClearConnectorNotice,
  active,
  submitting,
  modelAvailable,
  sendUnavailableReason,
  chatModels,
  imageModels,
  chatModel,
  imageModel,
  quota,
  usage,
  permissionLabel,
  permissionDescription,
  onChatModelChange,
  onImageModelChange,
  onOpenPermissionSettings,
  onSend,
  onUploadAttachment,
  onLoadAttachment,
  onLoadAttachmentThumbnail,
  onInterrupt,
}: ComposerProps) {
  const [disposition, setDisposition] = useState<SendDisposition>("steer");
  const [sendFailed, setSendFailed] = useState(false);
  const [attachments, setAttachments] = useState<InputAttachmentProjection[]>([]);
  const [attachmentUploading, setAttachmentUploading] = useState(false);
  const [pendingAttachments, setPendingAttachments] = useState<ReadonlyArray<{
    key: string;
    name: string;
    imageUrl: string | null;
  }>>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const consumedPrefillRef = useRef<string | null>(null);
  const remaining = quota?.remaining;
  const remainingLabel = typeof remaining === "number" ? remaining.toLocaleString("zh-CN") : "—";
  const formatTokens = (value: number | null | undefined) => {
    if (typeof value !== "number") return "—";
    if (value < 1_000) return value.toLocaleString("zh-CN");
    if (value < 1_000_000) return `${(value / 1_000).toLocaleString("zh-CN", { maximumFractionDigits: 1 })}k`;
    return `${(value / 1_000_000).toLocaleString("zh-CN", { maximumFractionDigits: 1 })}m`;
  };
  const contextLabel = usage?.context.used_tokens == null
    ? (usage?.context.window_tokens ? `— / ${formatTokens(usage.context.window_tokens)}` : "—")
    : usage.context.window_tokens
      ? `${formatTokens(usage.context.used_tokens)} / ${formatTokens(usage.context.window_tokens)}`
      : formatTokens(usage.context.used_tokens);
  const contextPercent = usage?.context.used_tokens != null && usage.context.window_tokens
    ? Math.max(0, Math.min(100, (usage.context.used_tokens / usage.context.window_tokens) * 100))
    : 0;
  const quotaUnit = quota?.unit === "managed_requests" ? "次" : (quota?.unit || "");
  const usageScopeLabel = usage?.complete_across_devices
    ? "账号累计"
    : "仅此设备";
  const sendLabel = submitting ? "发送中" : sendFailed ? "重试发送" : "发送";
  const selectedChatModel = chatModels.find((model) => model.model_id === chatModel);

  useEffect(() => {
    if (!prefillRequest || consumedPrefillRef.current === prefillRequest.key) return;
    consumedPrefillRef.current = prefillRequest.key;
    if (!draft.trim()) onDraftChange(prefillRequest.text);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  }, [draft, onDraftChange, prefillRequest]);

  const submit = async () => {
    if ((!draft.trim() && attachments.length === 0) || !modelAvailable) return;
    const sent = await onSend(draft, active ? disposition : "steer", attachments);
    setSendFailed(!sent);
    if (sent) {
      onDraftChange("");
      setAttachments([]);
      setAttachmentError(null);
    }
  };

  const addFiles = async (files: FileList | readonly File[] | null) => {
    if (!files?.length || attachmentUploading) return;
    const remainingFiles = Math.max(0, 20 - attachments.length);
    const remainingImages = Math.max(
      0,
      4 - attachments.filter((item) => item.media_kind === "image").length,
    );
    let acceptedImages = 0;
    let rejectedImages = false;
    let rejectedFiles = false;
    const candidates: File[] = [];
    for (const file of files) {
      if (candidates.length >= remainingFiles) {
        rejectedFiles = true;
        continue;
      }
      if (file.type.startsWith("image/")) {
        if (acceptedImages >= remainingImages) {
          rejectedImages = true;
          continue;
        }
        acceptedImages += 1;
      }
      candidates.push(file);
    }
    if (!candidates.length) {
      setAttachmentError(
        remainingFiles === 0
          ? "一次消息最多可添加 20 个文件。"
          : "一次消息最多可添加 4 张图片。",
      );
      return;
    }
    const capacityMessage = rejectedImages
      ? "一次消息最多可添加 4 张图片，其余图片未加入。"
      : rejectedFiles
        ? "一次消息最多可添加 20 个文件，其余文件未加入。"
        : null;
    setAttachmentUploading(true);
    setAttachmentError(capacityMessage);
    const pending = candidates.map((file) => ({
      key: crypto.randomUUID(),
      name: file.name,
      imageUrl: file.type.startsWith("image/") ? URL.createObjectURL(file) : null,
    }));
    setPendingAttachments(pending);
    const uploaded: InputAttachmentProjection[] = [];
    try {
      for (const file of candidates) {
        const attachment = await onUploadAttachment(file);
        if (attachment) uploaded.push(attachment);
        else {
          setAttachmentError(`“${file.name}”未能添加，请重试。`);
          break;
        }
      }
    } finally {
      pending.forEach((item) => {
        if (item.imageUrl) URL.revokeObjectURL(item.imageUrl);
      });
      setPendingAttachments([]);
    }
    if (uploaded.length) setAttachments((current) => [...current, ...uploaded]);
    setAttachmentUploading(false);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  return (
    <div className="ex-composer-region">
      <div className="ex-composer" data-busy={submitting ? "true" : "false"}>
        <label className="ex-composer-label" htmlFor="ecorex-composer">给 e-Mate 发消息</label>
        {attachments.length || pendingAttachments.length ? (
          <div className="ex-composer-attachments" role="group" aria-label="已添加文件">
            {pendingAttachments.map((pending) => (
              <span className="ex-input-attachment is-uploading" key={pending.key} aria-busy="true">
                {pending.imageUrl ? <img src={pending.imageUrl} alt="" /> : <FileText aria-hidden="true" />}
                <span className="ex-input-attachment-details">
                  <span title={pending.name}>{pending.name}</span>
                  <span className="ex-input-attachment-status" role="status">正在上传</span>
                </span>
                <LoaderCircle className="ex-attachment-spinner" aria-hidden="true" />
              </span>
            ))}
            {attachments.map((attachment) => (
              <InputAttachmentPreview
                key={attachment.attachment_id}
                attachment={attachment}
                loadBlob={onLoadAttachment}
                loadThumbnailBlob={onLoadAttachmentThumbnail}
                removable
                removeDisabled={attachmentUploading || submitting}
                onRemove={() => setAttachments((current) => current.filter((item) => item.attachment_id !== attachment.attachment_id))}
              />
            ))}
          </div>
        ) : null}
        <textarea
          ref={textareaRef}
          id="ecorex-composer"
          value={draft}
          rows={1}
          placeholder="给小芯发送消息，支持粘贴图片或文件"
          aria-describedby={!modelAvailable ? "ecorex-composer-note" : undefined}
          onChange={(event) => {
            onDraftChange(event.target.value);
            setSendFailed(false);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void submit();
            }
          }}
          onPaste={(event) => {
            const pastedFiles = [...event.clipboardData.items]
              .filter((item) => item.kind === "file")
              .map((item) => item.getAsFile())
              .filter((file): file is File => file !== null);
            if (!pastedFiles.length) return;
            event.preventDefault();
            void addFiles(pastedFiles);
          }}
        />
        <div className="ex-composer-toolbar">
          <div className="ex-composer-tools">
            <input
              ref={fileInputRef}
              className="ex-visually-hidden"
              type="file"
              multiple
              accept="image/*,text/*,application/pdf,application/json,application/zip,.doc,.docx,.xls,.xlsx,.ppt,.pptx"
              aria-label="选择要添加的文件"
              onChange={(event) => void addFiles(event.target.files)}
            />
            <button
              className="ex-composer-tool ex-attach-trigger"
              type="button"
              aria-label={attachmentUploading ? "正在添加文件" : "添加文件"}
              title={attachmentUploading ? "正在添加文件" : "添加文件"}
              disabled={attachmentUploading || attachments.length >= 20}
              onClick={() => fileInputRef.current?.click()}
            >
              <Plus aria-hidden="true" />
            </button>
            <Suspense fallback={(
              <button className="ex-composer-tool" type="button" aria-label="正在准备连接器" disabled>
                <span>连接器</span>
              </button>
            )}>
              <ConnectorPopover
                catalog={connectors}
                loadState={connectorLoadState}
                error={connectorError}
                notice={connectorNotice}
                operations={connectorOperations}
                onRefresh={onRefreshConnectors}
                onConnect={onConnectConnector}
                onReconnect={onReconnectConnector}
                onHealthCheck={onCheckConnector}
                onDisconnect={onDisconnectConnector}
                onClearError={onClearConnectorError}
                onClearNotice={onClearConnectorNotice}
              />
            </Suspense>
            <Suspense fallback={(
                <button
                  className="ex-composer-model-trigger"
                  type="button"
                  aria-label="选择模型"
                  disabled
                >
                  <span className="ex-model-trigger-label">{selectedChatModel?.display_name || "选择模型"}</span>
                  <ChevronDown aria-hidden="true" />
                </button>
            )}>
              <ComposerModelSelector
                chatModels={chatModels}
                imageModels={imageModels}
                chatModel={chatModel}
                imageModel={imageModel}
                onChatModelChange={(modelId) => {
                  onChatModelChange(modelId);
                  if (active && modelId !== chatModel) setDisposition("queue");
                }}
                onImageModelChange={(modelId) => {
                  onImageModelChange(modelId);
                  if (active && modelId !== imageModel) setDisposition("queue");
                }}
              />
            </Suspense>
          </div>
          <div className="ex-send-tools" data-active={active ? "true" : "false"}>
            {active ? (
              <DropdownMenu.Root>
                <DropdownMenu.Trigger asChild>
                  <button className="ex-disposition" type="button">
                    {dispositionLabel[disposition]}
                    <ChevronDown aria-hidden="true" />
                  </button>
                </DropdownMenu.Trigger>
                <DropdownMenu.Portal>
                  <DropdownMenu.Content className="ex-menu" side="top" align="end" sideOffset={8}>
                    {(Object.keys(dispositionLabel) as SendDisposition[]).map((value) => (
                      <DropdownMenu.Item
                        className="ex-menu-item"
                        key={value}
                        onSelect={() => {
                          setDisposition(value);
                          setSendFailed(false);
                        }}
                      >
                        {dispositionLabel[value]}
                      </DropdownMenu.Item>
                    ))}
                  </DropdownMenu.Content>
                </DropdownMenu.Portal>
              </DropdownMenu.Root>
            ) : null}
            {active ? (
              <IconButton label="停止当前任务" onClick={onInterrupt}>
                <Square aria-hidden="true" />
              </IconButton>
            ) : null}
            <button
              className="ex-send-button"
              type="button"
              disabled={(!draft.trim() && attachments.length === 0) || submitting || !modelAvailable}
              aria-label={sendLabel}
              aria-busy={submitting}
              aria-describedby={!modelAvailable ? "ecorex-composer-note" : undefined}
              onClick={() => void submit()}
            >
              <Send aria-hidden="true" />
              <span className="ex-send-button-label">{sendLabel}</span>
            </button>
          </div>
        </div>
        {attachmentError ? <p className="ex-composer-attachment-error" role="status">{attachmentError}</p> : null}
      </div>
      <div className="ex-composer-meta">
        <Tooltip.Root delayDuration={900}>
          <Tooltip.Trigger asChild>
            <button
              type="button"
              className="ex-permission-inline"
              onClick={onOpenPermissionSettings}
            >
              <ShieldCheck aria-hidden="true" />{permissionLabel}
            </button>
          </Tooltip.Trigger>
          <Tooltip.Portal>
            <Tooltip.Content className="ex-tooltip ex-permission-tooltip" side="top" sideOffset={8}>
              <span>{permissionDescription}</span>
              <span>需要权限或信息时会询问；长任务可排队，重启后继续。</span>
              <Tooltip.Arrow className="ex-tooltip-arrow" />
            </Tooltip.Content>
          </Tooltip.Portal>
        </Tooltip.Root>
        {!modelAvailable ? (
          <p className="ex-composer-note is-error" id="ecorex-composer-note" role="status">
            {sendUnavailableReason || "模型服务未连接；可查看历史和本地产物。"}
          </p>
        ) : null}
        <Tooltip.Root delayDuration={500}>
          <Tooltip.Trigger asChild>
            <button className="ex-usage-summary" type="button" aria-label={`查看用量。上下文 ${contextLabel}`}>
              <svg className="ex-context-ring" viewBox="0 0 20 20" aria-hidden="true">
                <circle className="ex-context-ring-track" cx="10" cy="10" r="7" pathLength="100" />
                <circle
                  className="ex-context-ring-value"
                  cx="10"
                  cy="10"
                  r="7"
                  pathLength="100"
                  strokeDasharray={`${contextPercent} 100`}
                />
              </svg>
              <span className="ex-visually-hidden">额度与上下文用量</span>
            </button>
          </Tooltip.Trigger>
          <Tooltip.Portal>
            <Tooltip.Content className="ex-tooltip ex-usage-tooltip" side="top" sideOffset={8}>
              <span><b>今日</b>{formatTokens(usage?.today.total_tokens)}</span>
              <span><b>本周</b>{formatTokens(usage?.week.total_tokens)}</span>
              <span><b>上下文</b>{contextLabel}</span>
              <span><b>额度</b>{remainingLabel}{remainingLabel === "—" ? "" : quotaUnit}</span>
              <span><b>范围</b>{usageScopeLabel}</span>
              <Tooltip.Arrow className="ex-tooltip-arrow" />
            </Tooltip.Content>
          </Tooltip.Portal>
        </Tooltip.Root>
      </div>
    </div>
  );
}
