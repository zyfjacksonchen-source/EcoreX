import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, FolderOpen } from "lucide-react";

import type { ProjectProjection } from "../api/contracts.ts";

interface NewConversationProjectSelectorProps {
  projects: ProjectProjection[];
  selectedProject: ProjectProjection | null;
  pickerBusy: boolean;
  onSelect: (project: ProjectProjection) => void;
  onPick: () => Promise<ProjectProjection | null>;
}

export default function NewConversationProjectSelector({
  projects,
  selectedProject,
  pickerBusy,
  onSelect,
  onPick,
}: NewConversationProjectSelectorProps) {
  const description = selectedProject
    ? `使用 ${selectedProject.name} 项目文件夹开启会话。`
    : projects.length
      ? `从 ${projects.length.toLocaleString("zh-CN")} 个已有项目中选择。`
      : "选择已有目录，作为会话的项目上下文。";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          className={`ex-new-project-trigger${selectedProject ? " is-selected" : ""}`}
          type="button"
          aria-label="选择项目会话"
          aria-pressed={Boolean(selectedProject)}
        >
          <FolderOpen aria-hidden="true" />
          <span>
            <strong>{selectedProject?.name || "项目会话"}</strong>
            <small>{description}</small>
          </span>
          <ChevronDown className="ex-new-project-chevron" aria-hidden="true" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="ex-menu ex-project-menu" side="bottom" align="start" sideOffset={8}>
          <DropdownMenu.Label className="ex-model-menu-label">已有项目</DropdownMenu.Label>
          {projects.length ? (
            <DropdownMenu.RadioGroup
              value={selectedProject?.project_id || ""}
              onValueChange={(projectId) => {
                const project = projects.find((candidate) => candidate.project_id === projectId);
                if (project) onSelect(project);
              }}
            >
              {projects.map((project) => (
                <DropdownMenu.RadioItem
                  className="ex-menu-item ex-project-menu-item"
                  value={project.project_id}
                  key={project.project_id}
                  title={project.project_path}
                >
                  <DropdownMenu.ItemIndicator className="ex-model-menu-check">
                    <Check aria-hidden="true" />
                  </DropdownMenu.ItemIndicator>
                  <span>{project.name}</span>
                </DropdownMenu.RadioItem>
              ))}
            </DropdownMenu.RadioGroup>
          ) : (
            <DropdownMenu.Label className="ex-menu-note">还没有已添加的项目。</DropdownMenu.Label>
          )}
          <DropdownMenu.Separator className="ex-menu-separator" />
          <DropdownMenu.Item
            className="ex-menu-item"
            disabled={pickerBusy}
            onSelect={() => void onPick().then((project) => {
              if (project) onSelect(project);
            })}
          >
            <FolderOpen className={pickerBusy ? "ex-spin" : ""} aria-hidden="true" />
            {pickerBusy ? "正在选择项目文件夹" : "添加项目文件夹…"}
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
