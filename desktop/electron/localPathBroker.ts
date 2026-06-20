import fsp from "node:fs/promises";
import path from "node:path";

export type OpenPathAction = "open" | "reveal" | "openWith";

type PermissionDecision = {
  allowed: boolean;
  reason: string;
};

export type LocalPathPermissionBroker = {
  authorizeStatPath(filePath: string): Promise<PermissionDecision>;
  authorizeOpenPath(event: unknown, filePath: string): Promise<PermissionDecision>;
  writeOpenResult(filePath: string, result: string): Promise<void>;
};

export type LocalPathShellBroker = {
  showItemInFolder(filePath: string): void;
  openPath(filePath: string): Promise<string>;
  openWith(filePath: string): Promise<void>;
};

export type LocalPathStatResult = {
  status: "success" | "denied" | "missing" | "error";
  path: string;
  exists: boolean;
  isFile?: boolean;
  isDirectory?: boolean;
  mimeType?: string;
  sizeBytes?: number;
  message?: string;
};

export const dangerousOpenExtensions = new Set([
  ".app",
  ".bat",
  ".cmd",
  ".command",
  ".com",
  ".exe",
  ".js",
  ".jse",
  ".lnk",
  ".msi",
  ".ps1",
  ".reg",
  ".scr",
  ".sh",
  ".vbe",
  ".vbs",
  ".wsf"
]);

export function mimeTypeForLocalPath(filePath: string) {
  const ext = path.extname(filePath).toLowerCase();
  const imageMime: Record<string, string> = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml"
  };
  const videoMime: Record<string, string> = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v"
  };
  return imageMime[ext] || videoMime[ext] || "";
}

function normalizedExtension(filePath: string) {
  return path.extname(filePath.replace(/[\\/]+$/g, "")).toLowerCase();
}

export async function statLocalPath(
  permissions: Pick<LocalPathPermissionBroker, "authorizeStatPath">,
  filePath: string
): Promise<LocalPathStatResult> {
  const target = String(filePath || "").trim();
  if (!target || !path.isAbsolute(target)) {
    return { status: "error", path: target, exists: false, message: "invalid path" };
  }
  const requestedPath = path.resolve(target);
  const initialDecision = await permissions.authorizeStatPath(requestedPath);
  if (!initialDecision.allowed) {
    return { status: "denied", path: target, exists: false, message: initialDecision.reason };
  }
  try {
    const realPath = await fsp.realpath(requestedPath).catch(() => requestedPath);
    if (realPath !== requestedPath) {
      const realDecision = await permissions.authorizeStatPath(realPath);
      if (!realDecision.allowed) {
        return { status: "denied", path: target, exists: false, message: realDecision.reason };
      }
    }
    const stat = await fsp.stat(realPath);
    return {
      status: "success",
      path: realPath,
      exists: true,
      isFile: stat.isFile(),
      isDirectory: stat.isDirectory(),
      mimeType: stat.isFile() ? mimeTypeForLocalPath(realPath) : "",
      sizeBytes: stat.isFile() ? stat.size : undefined
    };
  } catch {
    return { status: "missing", path: target, exists: false, message: "path not found" };
  }
}

export async function openLocalPath(
  permissions: LocalPathPermissionBroker,
  shellBroker: LocalPathShellBroker,
  event: unknown,
  filePath: string,
  action: OpenPathAction = "open"
) {
  if (!filePath || !path.isAbsolute(filePath)) {
    return "invalid path";
  }
  const requestedPath = path.resolve(filePath);
  let realPath = requestedPath;
  try {
    realPath = await fsp.realpath(requestedPath);
  } catch {
    return "path not found";
  }
  const requestedExt = normalizedExtension(requestedPath);
  const realExt = normalizedExtension(realPath);
  if (action !== "reveal" && (dangerousOpenExtensions.has(requestedExt) || dangerousOpenExtensions.has(realExt))) {
    return "blocked: executable or script-like files can only be revealed in folder";
  }
  const decision = await permissions.authorizeOpenPath(event, realPath);
  if (!decision.allowed) {
    return `denied: ${decision.reason}`;
  }
  let result = "";
  if (action === "reveal") {
    shellBroker.showItemInFolder(realPath);
  } else if (action === "openWith") {
    await shellBroker.openWith(realPath);
  } else {
    result = await shellBroker.openPath(realPath);
  }
  await permissions.writeOpenResult(realPath, result);
  return result;
}
