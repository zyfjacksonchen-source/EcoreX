import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { ChevronDown, FileText, Image, Plus, Send, ShieldCheck, Square, Workflow, X } from "lucide-react";
import { useRef, useState } from "react";

import type { SendDisposition, TaskMode } from "../state/useRuntimeSession.ts";
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
import { ConnectorPopover } from "./ConnectorPopover.tsx";
import { IconButton } from "./IconButton.tsx";

interface ComposerProps {
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
  mode: TaskMode;
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
  onModeChange: (mode: TaskMode) => void;
  onSend: (
    input: string,
    disposition: SendDisposition,
    attachments: readonly InputAttachmentProjection[],
  ) => Promise<boolean>;
  onUploadAttachment: (file: File) => Promise<InputAttachmentProjection | null>;
  onInterrupt: () => void;
}

const dispositionLabel: Record<SendDisposition, string> = {
  steer: "追加到当前任务",
  queue: "排到下一轮",
  replace: "替换当前任务",
};

export function Composer({
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
  mode,
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
  onModeChange,
  onSend,
  onUploadAttachment,
  onInterrupt,
}: ComposerProps) {
  const [draft, setDraft] = useState("");
  const [disposition, setDisposition] = useState<SendDisposition>("steer");
  const [sendFailed, setSendFailed] = useState(false);
  const [attachments, setAttachments] = useState<InputAttachmentProjection[]>([]);
  const [attachmentUploading, setAttachmentUploading] = useState(false);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activeModels = mode === "image" ? imageModels : chatModels;
  const activeModel = mode === "image" ? imageModel : chatModel;
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
  const quotaUnit = quota?.unit === "managed_requests" ? "次" : (quota?.unit || "");
  const sendLabel = submitting ? "发送中" : sendFailed ? "重试发送" : "发送";

  const submit = async () => {
    if (!draft.trim() || !modelAvailable) return;
    const sent = await onSend(draft, active ? disposition : "steer", attachments);
    setSendFailed(!sent);
    if (sent) {
      setDraft("");
      setAttachments([]);
      setAttachmentError(null);
    }
  };

  const addFiles = async (files: FileList | null) => {
    if (!files?.length || attachmentUploading) return;
    const candidates = [...files].slice(0, Math.max(0, 20 - attachments.length));
    if (!candidates.length) {
      setAttachmentError("一次消息最多可添加 20 个文件。");
      return;
    }
    setAttachmentUploading(true);
    setAttachmentError(null);
    const uploaded: InputAttachmentProjection[] = [];
    for (const file of candidates) {
      const attachment = await onUploadAttachment(file);
      if (attachment) uploaded.push(attachment);
      else {
        setAttachmentError(`“${file.name}”未能添加，请重试。`);
        break;
      }
    }
    if (uploaded.length) setAttachments((current) => [...current, ...uploaded]);
    setAttachmentUploading(false);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  return (
    <div className="ex-composer-region">
      <div className="ex-composer" data-busy={submitting ? "true" : "false"}>
        <label className="ex-composer-label" htmlFor="ecorex-composer">给 EcoreX 发消息</label>
        {attachments.length ? (
          <div className="ex-composer-attachments" role="group" aria-label="已添加文件">
            {attachments.map((attachment) => (
              <span className="ex-composer-attachment" key={attachment.attachment_id}>
                {attachment.media_kind === "image" ? <Image aria-hidden="true" /> : <FileText aria-hidden="true" />}
                <span title={attachment.display_name}>{attachment.display_name}</span>
                <button
                  type="button"
                  aria-label={`移除文件：${attachment.display_name}`}
                  disabled={attachmentUploading || submitting}
                  onClick={() => setAttachments((current) => current.filter((item) => item.attachment_id !== attachment.attachment_id))}
                ><X aria-hidden="true" /></button>
              </span>
            ))}
          </div>
        ) : null}
        <textarea
          id="ecorex-composer"
          value={draft}
          rows={2}
          placeholder={mode === "image" ? "描述要生成或修改的图片…" : "描述要完成的办公任务…"}
          aria-describedby="ecorex-composer-note"
          onChange={(event) => {
            setDraft(event.target.value);
            setSendFailed(false);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void submit();
            }
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
            <div className="ex-mode-switch" role="group" aria-label="任务类型">
              <button
                type="button"
                aria-pressed={mode === "office"}
                onClick={() => onModeChange("office")}
              >
                <Workflow aria-hidden="true" />
                办公
              </button>
              <button
                type="button"
                aria-pressed={mode === "image"}
                onClick={() => onModeChange("image")}
              >
                <Image aria-hidden="true" />
                图片
              </button>
            </div>
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
            <label className="ex-composer-model">
              <span className="ex-visually-hidden">{mode === "image" ? "图片模型" : "对话模型"}</span>
              <select
                value={activeModel}
                disabled={!activeModels.length}
                aria-label={mode === "image" ? "图片模型" : "对话模型"}
                onChange={(event) => {
                  if (mode === "image") onImageModelChange(event.target.value);
                  else onChatModelChange(event.target.value);
                }}
              >
                {activeModels.map((model) => (
                  <option value={model.model_id} key={model.model_id}>{model.display_name}</option>
                ))}
              </select>
              <ChevronDown aria-hidden="true" />
            </label>
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
              disabled={!draft.trim() || submitting || !modelAvailable}
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
        <span className="ex-permission-inline" title={permissionDescription}>
          <ShieldCheck aria-hidden="true" />{permissionLabel}
        </span>
        <p className="ex-composer-note" id="ecorex-composer-note" role={!modelAvailable ? "status" : undefined}>
          {modelAvailable
            ? "需要权限或信息时会询问；长任务可排队，重启后继续。"
            : sendUnavailableReason || "模型服务未连接；可查看历史和本地产物。"}
        </p>
        <div className="ex-usage-meter" role="group" aria-label="额度与上下文用量">
          <span title="今日已完成模型响应中由服务返回的 Token 用量">今日 <b>{formatTokens(usage?.today.total_tokens)}</b></span>
          <i aria-hidden="true" />
          <span title="本周已完成模型响应中由服务返回的 Token 用量">本周 <b>{formatTokens(usage?.week.total_tokens)}</b></span>
          <i aria-hidden="true" />
          <span title={usage?.context.model_id ? `模型 ${usage.context.model_id} 最近一次服务端上下文用量` : "等待服务端返回上下文用量"}>上下文 <b>{contextLabel}</b></span>
          <i aria-hidden="true" />
          <span title={`托管服务剩余额度：${remainingLabel} ${quotaUnit}`}>额度 <b>{remainingLabel}{remainingLabel === "—" ? "" : quotaUnit}</b></span>
        </div>
      </div>
    </div>
  );
}
