import * as Dialog from "@radix-ui/react-dialog";
import { FileText, Image as ImageIcon, X } from "lucide-react";
import { useEffect, useState } from "react";

import type { InputAttachmentProjection } from "../api/contracts.ts";

export type InputAttachmentBlobLoader = (
  attachmentId: string,
  signal?: AbortSignal,
) => Promise<Blob>;

interface InputAttachmentPreviewProps {
  attachment: InputAttachmentProjection;
  loadBlob: InputAttachmentBlobLoader;
  loadThumbnailBlob: InputAttachmentBlobLoader;
  removable?: boolean;
  removeDisabled?: boolean;
  onRemove?: () => void;
}

export function InputAttachmentPreview({
  attachment,
  loadBlob,
  loadThumbnailBlob,
  removable = false,
  removeDisabled = false,
  onRemove,
}: InputAttachmentPreviewProps) {
  const isImage = attachment.media_kind === "image";
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewFailed, setPreviewFailed] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [fullPreviewUrl, setFullPreviewUrl] = useState<string | null>(null);
  const [fullPreviewFailed, setFullPreviewFailed] = useState(false);

  useEffect(() => {
    if (!isImage) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setPreviewUrl(null);
    setPreviewFailed(false);
    void loadThumbnailBlob(attachment.attachment_id, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setPreviewUrl(objectUrl);
      })
      .catch(() => {
        if (!controller.signal.aborted) setPreviewFailed(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attachment.attachment_id, isImage, loadThumbnailBlob]);

  useEffect(() => {
    if (!isImage || !dialogOpen) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setFullPreviewUrl(null);
    setFullPreviewFailed(false);
    void loadBlob(attachment.attachment_id, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setFullPreviewUrl(objectUrl);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFullPreviewFailed(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attachment.attachment_id, dialogOpen, isImage, loadBlob]);

  const details = (
    <span className="ex-input-attachment-details">
      <span title={attachment.display_name}>{attachment.display_name}</span>
      <span className="ex-input-attachment-status" role="status">
        {isImage && !previewUrl && !previewFailed ? "正在读取预览" : previewFailed ? "预览不可用" : "已就绪"}
      </span>
    </span>
  );

  if (!isImage) {
    return (
      <span className="ex-input-attachment is-file">
        <FileText aria-hidden="true" />
        {details}
        {removable ? (
          <button className="ex-icon-button ex-attachment-remove" type="button" aria-label={`移除文件：${attachment.display_name}`} disabled={removeDisabled} onClick={onRemove}>
            <X aria-hidden="true" />
          </button>
        ) : null}
      </span>
    );
  }

  return (
    <span className="ex-input-attachment is-image">
      <Dialog.Root open={dialogOpen} onOpenChange={setDialogOpen}>
        <Dialog.Trigger asChild>
          <button
            className="ex-input-attachment-preview-trigger"
            type="button"
            aria-label={`完整预览：${attachment.display_name}`}
          >
            {previewUrl ? <img src={previewUrl} alt="" /> : <ImageIcon aria-hidden="true" />}
          </button>
        </Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Overlay className="ex-attachment-preview-overlay" />
          <Dialog.Content className="ex-attachment-preview-dialog" aria-describedby={undefined}>
            <Dialog.Title>{attachment.display_name}</Dialog.Title>
            {fullPreviewUrl ? <img src={fullPreviewUrl} alt={attachment.display_name} /> : null}
            {!fullPreviewUrl && !fullPreviewFailed ? <span role="status">正在读取完整图片</span> : null}
            {fullPreviewFailed ? <span role="alert">完整图片暂时无法预览</span> : null}
            <Dialog.Close asChild>
              <button className="ex-icon-button ex-attachment-preview-close" type="button" aria-label="关闭图片预览">
                <X aria-hidden="true" />
              </button>
            </Dialog.Close>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      {details}
      {removable ? (
        <button className="ex-icon-button ex-attachment-remove" type="button" aria-label={`移除文件：${attachment.display_name}`} disabled={removeDisabled} onClick={onRemove}>
          <X aria-hidden="true" />
        </button>
      ) : null}
    </span>
  );
}
