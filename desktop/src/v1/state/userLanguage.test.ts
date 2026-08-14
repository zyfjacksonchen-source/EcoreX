import assert from "node:assert/strict";
import test from "node:test";

import { RuntimeApiError } from "../api/runtimeClient.ts";
import {
  artifactFamilyLabel,
  formatFileSize,
  serviceReasonMessage,
  technicalErrorCode,
  turnTerminalPresentation,
  userFacingError,
} from "./userLanguage.ts";

test("service reasons are translated without exposing an unknown backend code", () => {
  assert.equal(
    serviceReasonMessage("managed_image_edit_not_configured", "fallback"),
    "精准修图服务尚未配置，请联系管理员。",
  );
  assert.equal(
    serviceReasonMessage("vendor_internal_state_v9", "这项功能暂时不可用。"),
    "这项功能暂时不可用。",
  );
});

test("turn terminal reasons keep friendly guidance separate from the stable code", () => {
  assert.deepEqual(turnTerminalPresentation("provider_response_failed"), {
    message: "模型服务未能完成这次回复，已完成的步骤和结果仍然保留。",
    suggestion: "可以重试本次任务；e-Mate 不会重复执行已经完成的工具步骤。",
    technicalCode: "provider_response_failed",
  });
  assert.equal(turnTerminalPresentation("completed"), null);
  assert.equal(
    turnTerminalPresentation("vendor_private_failure")?.message,
    "e-Mate 未能完成这次任务，当前进度和结果已经保留。",
  );
});

test("primary error copy and collapsed technical code remain separate", () => {
  assert.equal(
    userFacingError(new RuntimeApiError(
      "blocked",
      409,
      "acceptance_preview_external_mutation_blocked",
    )),
    "候选验收窗口不会修改正式账号或外部服务，请在正式版本中执行这项操作。",
  );
  const error = new RuntimeApiError(
    "provider_timeout internal lease failed",
    503,
    "provider_timeout",
  );
  assert.equal(
    userFacingError(error),
    "扩展响应超时，e-Mate 已停止等待。你可以稍后重试。",
  );
  assert.equal(technicalErrorCode(error), "provider_timeout");
  assert.equal(
    userFacingError(new RuntimeApiError(
      "managed connector authorization could not be started",
      400,
      "connector_auth_error",
    )),
    "连接授权服务未能开始登录，请稍后重试。",
  );
  assert.equal(technicalErrorCode(new Error("ordinary")), null);
  const sharePreview = new RuntimeApiError(
    "internal path C:\\secret\\image.png",
    409,
    "share_image_preview_missing",
  );
  assert.equal(
    userFacingError(sharePreview),
    "有图片还没有可分享的预览图。请等待图片处理完成后重试。",
  );
  assert.equal(technicalErrorCode(sharePreview), "share_image_preview_missing");
  const unknown = new RuntimeApiError(
    "内部错误代码 vendor_private_failure，请检查 trace abc",
    503,
    "vendor_private_failure",
  );
  assert.equal(
    userFacingError(unknown),
    "e-Mate 暂时无法完成这项操作，当前数据已保留。请稍后重试。",
  );
  assert.equal(technicalErrorCode(unknown), "vendor_private_failure");
  const untrustedChinese = new RuntimeApiError(
    "操作失败，请把本机目录发给技术支持。",
    400,
  );
  assert.equal(
    userFacingError(untrustedChinese),
    "e-Mate 暂时没有响应。当前数据已保留，请稍后重试。",
  );
  assert.equal(
    userFacingError(new Error("请选择一张图片后重试。")),
    "请选择一张图片后重试。",
  );
});

test("artifact type and size use office-user labels", () => {
  assert.equal(artifactFamilyLabel("spreadsheet"), "表格");
  assert.equal(artifactFamilyLabel("web_report"), "网页报告");
  assert.equal(formatFileSize(0), "0 B");
  assert.equal(formatFileSize(1536), "1.5 KB");
  assert.equal(formatFileSize(5 * 1024 * 1024), "5 MB");
  assert.equal(formatFileSize(Number.NaN), "大小未知");
});
