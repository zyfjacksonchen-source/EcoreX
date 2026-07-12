import type { ArtifactProjection } from "../api/contracts.ts";

export type ArtifactPreviewErrorCode =
  | "not_found"
  | "too_large"
  | "decode_failed"
  | "cancelled"
  | "unavailable";

interface ArtifactPreviewIdentityState {
  request_id: number;
  artifact_id: string;
  revision_id: string;
}

export type ArtifactPreviewLoadResult =
  | { status: "ready"; url: string }
  | {
      status: "error";
      code: ArtifactPreviewErrorCode;
      message: string;
      can_retry: boolean;
    };

export type ArtifactPreviewState =
  | (ArtifactPreviewIdentityState & { status: "loading" })
  | (ArtifactPreviewIdentityState & { status: "ready"; url: string })
  | (ArtifactPreviewIdentityState & {
      status: "error";
      code: ArtifactPreviewErrorCode;
      message: string;
      can_retry: boolean;
    });

export function loadingArtifactPreview(
  artifact: ArtifactProjection,
  requestId: number,
): ArtifactPreviewState {
  return {
    status: "loading",
    request_id: requestId,
    artifact_id: artifact.artifact_id,
    revision_id: artifact.revision_id,
  };
}

export function readyArtifactPreview(
  artifact: ArtifactProjection,
  requestId: number,
  url: string,
): ArtifactPreviewState {
  return {
    status: "ready",
    request_id: requestId,
    artifact_id: artifact.artifact_id,
    revision_id: artifact.revision_id,
    url,
  };
}

export function settleArtifactPreview(
  current: ArtifactPreviewState | null,
  requestId: number,
  result: ArtifactPreviewLoadResult,
): ArtifactPreviewState | null {
  if (!current || current.request_id !== requestId) return current;
  return {
    ...current,
    ...result,
  };
}

export function failArtifactPreviewDecode(
  current: ArtifactPreviewState | null,
  url: string,
): ArtifactPreviewState | null {
  if (!current || current.status !== "ready" || current.url !== url) return current;
  return {
    status: "error",
    request_id: current.request_id,
    artifact_id: current.artifact_id,
    revision_id: current.revision_id,
    code: "decode_failed",
    message: "这张图片无法正确显示，文件可能已损坏。你可以重试加载或保存原文件。",
    can_retry: true,
  };
}
