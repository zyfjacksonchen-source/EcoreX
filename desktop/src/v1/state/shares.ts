export interface ClipboardWriter {
  writeText(value: string): Promise<void>;
}

export async function writeShareUrl(
  value: string,
  clipboard: ClipboardWriter | null | undefined,
): Promise<"copied"> {
  if (!clipboard?.writeText) {
    throw new Error("当前浏览器未提供剪贴板权限，请手动选择并复制链接。");
  }
  await clipboard.writeText(value);
  return "copied";
}
