import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { ChevronDown, Image, Send, Square, Workflow } from "lucide-react";
import { useState } from "react";

import type { SendDisposition, TaskMode } from "../state/useRuntimeSession.ts";
import type {
  ConnectorCatalogItem,
  ConnectorInstanceProjection,
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
  onModeChange: (mode: TaskMode) => void;
  onSend: (input: string, disposition: SendDisposition) => Promise<boolean>;
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
  onModeChange,
  onSend,
  onInterrupt,
}: ComposerProps) {
  const [draft, setDraft] = useState("");
  const [disposition, setDisposition] = useState<SendDisposition>("steer");
  const [sendFailed, setSendFailed] = useState(false);

  const submit = async () => {
    if (!draft.trim() || !modelAvailable) return;
    const sent = await onSend(draft, active ? disposition : "steer");
    setSendFailed(!sent);
    if (sent) setDraft("");
  };

  return (
    <div className="ex-composer-region">
      <div className="ex-composer" data-busy={submitting ? "true" : "false"}>
        <label className="ex-composer-label" htmlFor="ecorex-composer">给 EcoreX 发消息</label>
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
          </div>
          <div className="ex-send-tools">
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
              aria-busy={submitting}
              aria-describedby={!modelAvailable ? "ecorex-composer-note" : undefined}
              onClick={() => void submit()}
            >
              <Send aria-hidden="true" />
              <span>{submitting ? "发送中" : sendFailed ? "重试发送" : "发送"}</span>
            </button>
          </div>
        </div>
      </div>
      <p className="ex-composer-note" id="ecorex-composer-note" role={!modelAvailable ? "status" : undefined}>
        {modelAvailable
          ? "需要权限或信息时会询问；长任务可排队，重启后继续。"
          : sendUnavailableReason || "模型服务未连接；可查看历史和本地产物。"}
      </p>
    </div>
  );
}
