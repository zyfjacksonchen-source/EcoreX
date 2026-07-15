import type {
  ArtifactExternalActionProjection,
  ArtifactListResponse,
  ArtifactProjection,
  RetouchAnnotation,
  RetouchJobProjection,
  RetouchViewState,
  RetouchWorkspaceProjection,
} from "./contracts.ts";
import { validateArtifactBoundary } from "./artifactRuntimeContract.ts";

export type ArtifactOperationKind =
  | "action"
  | "feedback"
  | "get"
  | "list"
  | "open_workspace"
  | "request_retouch"
  | "reopen_workspace"
  | "save_workspace"
  | "submit_workspace"
  | "workspace";

export type ArtifactJsonTransport = <T>(
  path: string,
  init: RequestInit,
  mutation: boolean,
  validate: (value: unknown) => T | Promise<T>,
) => Promise<T>;

type ArtifactOperationInput = Readonly<Record<string, unknown>>;

function artifact(value: unknown): ArtifactProjection {
  return value as ArtifactProjection;
}

function workspace(value: unknown): RetouchWorkspaceProjection {
  return value as RetouchWorkspaceProjection;
}

export function executeArtifactOperation(
  request: ArtifactJsonTransport,
  operation: ArtifactOperationKind,
  input: ArtifactOperationInput,
): Promise<unknown> {
  switch (operation) {
    case "list": {
      const query = new URLSearchParams();
      if (typeof input.threadId === "string" && input.threadId) {
        query.set("thread_id", input.threadId);
      }
      const suffix = query.size ? `?${query}` : "";
      return request<ArtifactListResponse>(
        `/api/v1/artifacts${suffix}`,
        { signal: input.signal as AbortSignal | undefined },
        false,
        (value) => validateArtifactBoundary(value, "list") as ArtifactListResponse,
      );
    }
    case "get": {
      const artifactId = String(input.artifactId);
      return request<ArtifactProjection>(
        `/api/v1/artifacts/${encodeURIComponent(artifactId)}`,
        { signal: input.signal as AbortSignal | undefined },
        false,
        (value) => validateArtifactBoundary(value, "projection", { artifact_id: artifactId }) as ArtifactProjection,
      );
    }
    case "feedback": {
      const target = artifact(input.artifact);
      const clientRequestId = String(input.clientRequestId);
      return request<NonNullable<ArtifactProjection["feedback"]>>(
        `/api/v1/artifacts/${encodeURIComponent(target.artifact_id)}/feedback`,
        {
          method: "POST",
          body: JSON.stringify({
            revision_id: target.revision_id,
            signal: input.signal,
            client_request_id: clientRequestId,
          }),
        },
        true,
        (value) => validateArtifactBoundary(value, "feedback", {
          artifact_id: target.artifact_id,
          revision_id: target.revision_id,
          client_request_id: clientRequestId,
        }) as NonNullable<ArtifactProjection["feedback"]>,
      );
    }
    case "action": {
      const artifactId = String(input.artifactId);
      const action = input.action as "open" | "reveal";
      const clientRequestId = String(input.clientRequestId);
      return request<ArtifactExternalActionProjection>(
        `/api/v1/artifacts/${encodeURIComponent(artifactId)}/actions/${action}`,
        { method: "POST", body: JSON.stringify({ client_request_id: clientRequestId }) },
        true,
        (value) => validateArtifactBoundary(value, "action", {
          artifact_id: artifactId,
          action,
          client_request_id: clientRequestId,
        }) as ArtifactExternalActionProjection,
      );
    }
    case "request_retouch": {
      const target = artifact(input.artifact);
      return request<RetouchJobProjection>(
        `/api/v1/artifacts/${encodeURIComponent(target.artifact_id)}/retouch`,
        {
          method: "POST",
          body: JSON.stringify({
            base_revision_id: target.revision_id,
            selected_artifact_ids: [target.artifact_id],
            agent_model_id: input.agentModelId,
            image_model_id: input.imageModelId,
            annotations: input.annotations as RetouchAnnotation[],
            reference_artifact_ids: [],
            global_instruction: input.globalInstruction,
            client_request_id: input.clientRequestId,
          }),
        },
        true,
        (value) => validateArtifactBoundary(value, "job", {
          artifact_id: target.artifact_id,
          revision_id: target.revision_id,
        }) as RetouchJobProjection,
      );
    }
    case "open_workspace": {
      const target = artifact(input.artifact);
      return request<RetouchWorkspaceProjection>(
        `/api/v1/artifacts/${encodeURIComponent(target.artifact_id)}/retouch-workspaces`,
        {
          method: "POST",
          body: JSON.stringify({
            base_revision_id: target.revision_id,
            client_request_id: input.clientRequestId,
          }),
        },
        true,
        (value) => validateArtifactBoundary(value, "workspace", {
          artifact_id: target.artifact_id,
          revision_id: target.revision_id,
        }) as RetouchWorkspaceProjection,
      );
    }
    case "workspace": {
      const workspaceId = String(input.workspaceId);
      return request<RetouchWorkspaceProjection>(
        `/api/v1/retouch-workspaces/${encodeURIComponent(workspaceId)}`,
        { signal: input.signal as AbortSignal | undefined },
        false,
        (value) => validateArtifactBoundary(value, "workspace", { workspace_id: workspaceId }) as RetouchWorkspaceProjection,
      );
    }
    case "save_workspace": {
      const target = workspace(input.workspace);
      return request<RetouchWorkspaceProjection>(
        `/api/v1/retouch-workspaces/${encodeURIComponent(target.workspace_id)}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            expected_version: target.version,
            annotations: input.annotations as RetouchAnnotation[],
            reference_artifact_ids: input.referenceArtifactIds,
            global_instruction: input.globalInstruction,
            view_state: input.viewState as Partial<RetouchViewState>,
            client_request_id: input.clientRequestId,
          }),
        },
        true,
        (value) => validateArtifactBoundary(value, "workspace", {
          workspace_id: target.workspace_id,
          artifact_id: target.artifact_id,
          revision_id: target.edit_surface.base_revision_id,
        }) as RetouchWorkspaceProjection,
      );
    }
    case "submit_workspace": {
      const target = workspace(input.workspace);
      return request<RetouchWorkspaceProjection>(
        `/api/v1/retouch-workspaces/${encodeURIComponent(target.workspace_id)}/submit`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_version: target.version,
            agent_model_id: input.agentModelId,
            image_model_id: input.imageModelId,
            client_request_id: input.clientRequestId,
          }),
        },
        true,
        (value) => validateArtifactBoundary(value, "workspace", {
          workspace_id: target.workspace_id,
          artifact_id: target.artifact_id,
          revision_id: target.edit_surface.base_revision_id,
        }) as RetouchWorkspaceProjection,
      );
    }
    case "reopen_workspace": {
      const target = workspace(input.workspace);
      return request<RetouchWorkspaceProjection>(
        `/api/v1/retouch-workspaces/${encodeURIComponent(target.workspace_id)}/reopen`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_version: target.version,
            client_request_id: input.clientRequestId,
          }),
        },
        true,
        (value) => validateArtifactBoundary(value, "workspace", {
          workspace_id: target.workspace_id,
          artifact_id: target.artifact_id,
          revision_id: target.edit_surface.base_revision_id,
        }) as RetouchWorkspaceProjection,
      );
    }
  }
}
