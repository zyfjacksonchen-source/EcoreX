import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown } from "lucide-react";

import type { ModelDescriptor } from "../api/contracts.ts";

interface ComposerModelSelectorProps {
  chatModels: ModelDescriptor[];
  imageModels: ModelDescriptor[];
  chatModel: string;
  imageModel: string;
  onChatModelChange: (modelId: string) => void;
  onImageModelChange: (modelId: string) => void;
}

export default function ComposerModelSelector({
  chatModels,
  imageModels,
  chatModel,
  imageModel,
  onChatModelChange,
  onImageModelChange,
}: ComposerModelSelectorProps) {
  const selectedChatModel = chatModels.find((model) => model.model_id === chatModel) ?? chatModels[0];
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          className="ex-composer-model-trigger"
          type="button"
          aria-label="选择模型"
          disabled={!chatModels.length && !imageModels.length}
        >
          <span>{selectedChatModel?.display_name || "选择模型"}</span>
          <ChevronDown aria-hidden="true" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="ex-menu ex-model-menu" side="top" align="start" sideOffset={8}>
          <DropdownMenu.Label className="ex-model-menu-label">Agent 模型</DropdownMenu.Label>
          <DropdownMenu.RadioGroup value={chatModel} onValueChange={onChatModelChange}>
            {chatModels.map((model) => (
              <DropdownMenu.RadioItem
                className="ex-menu-item ex-model-menu-item"
                value={model.model_id}
                key={model.model_id}
              >
                <DropdownMenu.ItemIndicator className="ex-model-menu-check">
                  <Check aria-hidden="true" />
                </DropdownMenu.ItemIndicator>
                <span>{model.display_name}</span>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
          <DropdownMenu.Separator className="ex-menu-separator" />
          <DropdownMenu.Label className="ex-model-menu-label">
            图片模型 <small>按意图自动调用</small>
          </DropdownMenu.Label>
          {imageModels.length ? (
            <DropdownMenu.RadioGroup value={imageModel} onValueChange={onImageModelChange}>
              {imageModels.map((model) => (
                <DropdownMenu.RadioItem
                  className="ex-menu-item ex-model-menu-item"
                  value={model.model_id}
                  key={model.model_id}
                >
                  <DropdownMenu.ItemIndicator className="ex-model-menu-check">
                    <Check aria-hidden="true" />
                  </DropdownMenu.ItemIndicator>
                  <span>{model.display_name}</span>
                </DropdownMenu.RadioItem>
              ))}
            </DropdownMenu.RadioGroup>
          ) : (
            <DropdownMenu.Item className="ex-menu-item" disabled>未提供图片模型</DropdownMenu.Item>
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
