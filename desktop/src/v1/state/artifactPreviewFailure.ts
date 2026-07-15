import { RuntimeApiError } from "../api/runtimeClient.ts";
import { ArtifactPreviewLimitError } from "./artifactPreviewCache.ts";
import type { ArtifactPreviewLoadResult } from "./artifactPreviewState.ts";
import { userFacingError } from "./userLanguage.ts";

export function artifactPreviewLoadFailure(error: unknown): ArtifactPreviewLoadResult {
  if (error instanceof ArtifactPreviewLimitError) {
    return {
      status: "error",
      code: "too_large",
      message: "这张预览图过大，无法在页面内安全加载。你仍可保存原文件。",
      can_retry: false,
    };
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return {
      status: "error",
      code: "cancelled",
      message: "预览加载已取消，请重试。",
      can_retry: true,
    };
  }
  if (error instanceof RuntimeApiError && error.status === 404) {
    return {
      status: "error",
      code: "not_found",
      message: "暂时找不到这份预览，它可能仍在生成或已经更新。请重试。",
      can_retry: true,
    };
  }
  if (error instanceof RuntimeApiError && error.status === 413) {
    return {
      status: "error",
      code: "too_large",
      message: "这张预览图过大，无法在页面内安全加载。你仍可保存原文件。",
      can_retry: false,
    };
  }
  return {
    status: "error",
    code: "unavailable",
    message: userFacingError(error),
    can_retry: true,
  };
}
