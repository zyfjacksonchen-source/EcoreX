import assert from "node:assert/strict";
import test from "node:test";

import { writeShareUrl } from "./shares.ts";

test("clipboard success is returned only after the browser write resolves", async () => {
  let resolveWrite!: () => void;
  const write = new Promise<void>((resolve) => {
    resolveWrite = resolve;
  });
  let settled = false;
  const pending = writeShareUrl("https://share.ecorex.example/s/unique", {
    writeText: () => write,
  }).then((result) => {
    settled = true;
    return result;
  });

  await Promise.resolve();
  assert.equal(settled, false);
  resolveWrite();
  assert.equal(await pending, "copied");
});

test("clipboard denial rejects and can never be interpreted as success", async () => {
  await assert.rejects(
    writeShareUrl("https://share.ecorex.example/s/unique", {
      writeText: async () => {
        throw new DOMException("Permission denied", "NotAllowedError");
      },
    }),
    /Permission denied/,
  );
  await assert.rejects(
    writeShareUrl("https://share.ecorex.example/s/unique", null),
    /手动选择并复制链接/,
  );
});
