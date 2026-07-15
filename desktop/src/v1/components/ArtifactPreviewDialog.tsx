import * as Dialog from "@radix-ui/react-dialog";
import { Download, Maximize2, RefreshCw, X, ZoomIn, ZoomOut } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { ArtifactProjection } from "../api/contracts.ts";
import {
  ARTIFACT_PREVIEW_MAX_BYTES,
  ArtifactPreviewLimitError,
} from "../state/artifactPreviewCache.ts";
import { artifactPreviewLoadFailure } from "../state/artifactPreviewFailure.ts";
import {
  failArtifactPreviewDecode,
  loadingArtifactPreview,
  settleArtifactPreview,
} from "../state/artifactPreviewState.ts";
import type { ArtifactPreviewState } from "../state/artifactPreviewState.ts";
import { artifactFamilyLabel, formatFileSize } from "../state/userLanguage.ts";
import { IconButton } from "./IconButton.tsx";

const IMAGE_ZOOM_STEPS = [1, 1.25, 1.5, 2, 3, 4] as const;

interface ArtifactPreviewDialogProps {
  artifact: ArtifactProjection | null;
  onClose: () => void;
  onRestoreFocus: () => void;
  onDownload: (artifact: ArtifactProjection) => void;
  onLoadPreview: (artifact: ArtifactProjection, signal: AbortSignal) => Promise<Blob>;
}

export function ArtifactPreviewDialog({
  artifact,
  onClose,
  onRestoreFocus,
  onDownload,
  onLoadPreview,
}: ArtifactPreviewDialogProps) {
  const mediaType = artifact?.mime_type ?? "";
  const previewAllowed = artifact?.actions.includes("preview") ?? false;
  const [preview, setPreview] = useState<ArtifactPreviewState | null>(null);
  const requestSequence = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const ownedUrl = useRef<string | null>(null);
  const currentPreview = artifact
    && preview?.artifact_id === artifact.artifact_id
    && preview.revision_id === artifact.revision_id
    ? preview
    : null;
  const url = currentPreview?.status === "ready" ? currentPreview.url : null;
  const [zoomIndex, setZoomIndex] = useState(0);
  const zoom = IMAGE_ZOOM_STEPS[zoomIndex];

  const releaseOwnedUrl = useCallback(() => {
    if (!ownedUrl.current) return;
    URL.revokeObjectURL(ownedUrl.current);
    ownedUrl.current = null;
  }, []);

  const requestPreview = useCallback(() => {
    requestController.current?.abort();
    releaseOwnedUrl();
    const requestId = requestSequence.current + 1;
    requestSequence.current = requestId;
    if (!artifact) {
      requestController.current = null;
      setPreview(null);
      return;
    }
    const controller = new AbortController();
    requestController.current = controller;
    const loading = loadingArtifactPreview(artifact, requestId);
    if (!artifact.actions.includes("preview")) {
      setPreview(settleArtifactPreview(loading, requestId, {
        status: "error",
        code: "unavailable",
        message: "这份产物当前未开放页面预览，你仍可使用后台提供的其他操作。",
        can_retry: false,
      }));
      return;
    }
    setPreview(loading);
    void onLoadPreview(artifact, controller.signal).then((blob) => {
      if (controller.signal.aborted || requestSequence.current !== requestId) return;
      if (blob.size > ARTIFACT_PREVIEW_MAX_BYTES) throw new ArtifactPreviewLimitError();
      const url = URL.createObjectURL(blob);
      if (controller.signal.aborted || requestSequence.current !== requestId) {
        URL.revokeObjectURL(url);
        return;
      }
      ownedUrl.current = url;
      setPreview((current) => settleArtifactPreview(current, requestId, {
        status: "ready",
        url,
      }));
    }).catch((error: unknown) => {
      if (controller.signal.aborted || requestSequence.current !== requestId) return;
      setPreview((current) => settleArtifactPreview(
        current,
        requestId,
        artifactPreviewLoadFailure(error),
      ));
    });
  }, [
    artifact?.artifact_id,
    artifact?.revision_id,
    onLoadPreview,
    previewAllowed,
    releaseOwnedUrl,
  ]);

  useEffect(() => {
    requestPreview();
    return () => {
      requestSequence.current += 1;
      requestController.current?.abort();
      requestController.current = null;
      releaseOwnedUrl();
    };
  }, [releaseOwnedUrl, requestPreview]);

  useEffect(() => {
    setZoomIndex(0);
  }, [artifact?.artifact_id, artifact?.revision_id, url]);

  const zoomIn = () => setZoomIndex((value) => Math.min(IMAGE_ZOOM_STEPS.length - 1, value + 1));
  const zoomOut = () => setZoomIndex((value) => Math.max(0, value - 1));
  return (
    <Dialog.Root open={artifact !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="ex-dialog-overlay" />
        <Dialog.Content
          className="ex-dialog ex-preview-dialog"
          aria-describedby={undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            onRestoreFocus();
          }}
        >
          <div className="ex-dialog-heading">
            <div>
              <Dialog.Title>{artifact?.display_name ?? "产物预览"}</Dialog.Title>
              {artifact ? (
                <span>{artifactFamilyLabel(artifact.family)} · {formatFileSize(artifact.size_bytes)}</span>
              ) : null}
            </div>
            <Dialog.Close asChild>
              <IconButton label="关闭预览"><X aria-hidden="true" /></IconButton>
            </Dialog.Close>
          </div>
          {url && mediaType.startsWith("image/") ? (
            <div className="ex-preview-toolbar" aria-label="图片缩放">
              <IconButton label="缩小图片" disabled={zoom <= 1} onClick={zoomOut}>
                <ZoomOut aria-hidden="true" />
              </IconButton>
              <span aria-live="polite">{zoom === 1 ? "适合窗口" : `${Math.round(zoom * 100)}%`}</span>
              <IconButton label="放大图片" disabled={zoom >= 4} onClick={zoomIn}>
                <ZoomIn aria-hidden="true" />
              </IconButton>
              <IconButton label="显示完整图片" disabled={zoom === 1} onClick={() => setZoomIndex(0)}>
                <Maximize2 aria-hidden="true" />
              </IconButton>
            </div>
          ) : null}
          <div
            className="ex-preview-body"
            aria-busy={!currentPreview || currentPreview.status === "loading"}
          >
            {!currentPreview || currentPreview.status === "loading" ? (
              <p role="status" aria-live="polite">正在安全加载预览…</p>
            ) : null}
            {currentPreview?.status === "error" ? (
              <div className="ex-preview-error" role="alert">
                <div>
                  <strong>暂时无法显示预览</strong>
                  <p>{currentPreview.message}</p>
                  {currentPreview.can_retry ? (
                    <button className="ex-button" type="button" onClick={requestPreview}>
                      <RefreshCw aria-hidden="true" />
                      重新加载
                    </button>
                  ) : null}
                </div>
              </div>
            ) : null}
            {url && mediaType.startsWith("image/") ? (
              <div
                className={`ex-preview-media-canvas is-zoom-${Math.round(zoom * 100)}`}
              >
                <img
                  src={url}
                  alt={artifact?.display_name ?? "产物预览"}
                  draggable={false}
                  onLoad={(event) => {
                    const image = event.currentTarget;
                    if (typeof image.decode === "function") {
                      void image.decode().catch(() => {
                        setPreview((current) => failArtifactPreviewDecode(current, url));
                        releaseOwnedUrl();
                      });
                    }
                  }}
                  onError={() => {
                    setPreview((current) => failArtifactPreviewDecode(current, url));
                    releaseOwnedUrl();
                  }}
                />
              </div>
            ) : null}
            {url && mediaType.startsWith("video/") ? (
              <video src={url} controls aria-label={artifact?.display_name} />
            ) : null}
            {url && mediaType.startsWith("audio/") ? (
              <audio src={url} controls aria-label={artifact?.display_name} />
            ) : null}
            {url && !mediaType.startsWith("image/") && !mediaType.startsWith("video/") && !mediaType.startsWith("audio/") ? (
              <iframe src={url} sandbox="" title={artifact?.display_name ?? "产物预览"} />
            ) : null}
          </div>
          {artifact?.actions.includes("download") ? (
            <div className="ex-dialog-actions">
              <button className="ex-button" type="button" onClick={() => onDownload(artifact)}>
                <Download aria-hidden="true" />
                保存到默认位置
              </button>
            </div>
          ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
