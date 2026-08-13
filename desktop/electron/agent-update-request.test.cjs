const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { consumeAgentUpdateRequest, initAgentUpdateRequests } = require("./agent-update-request.cjs");

function fixture(root, nonce = "n".repeat(43)) {
  fs.mkdirSync(path.join(root, "bootstrap"), { recursive: true });
  fs.mkdirSync(path.join(root, "desktop-update"), { recursive: true });
  fs.writeFileSync(path.join(root, "bootstrap", "runtime-owner.json"), JSON.stringify({
    schema_version: 2,
    nonce,
    pid: 42,
    runtime_identity: {
      release_id: "release-test",
      build_digest: "a".repeat(64),
      artifact_id: "artifact-test",
      artifact_sha256: "b".repeat(64),
      payload_digest: "c".repeat(64),
    },
  }));
  return {
    schema_version: 1,
    action: "install_latest",
    owner_nonce: nonce,
    tool_call_id: "call-update-205",
  };
}

test("Agent update request is consumed only for the current Runtime owner", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "emate-agent-update-"));
  try {
    const request = fixture(root);
    const requestPath = path.join(root, "desktop-update", "request.json");
    fs.writeFileSync(requestPath, JSON.stringify(request));
    assert.deepEqual(consumeAgentUpdateRequest(root), request);
    assert.equal(fs.existsSync(requestPath), false);

    fs.writeFileSync(requestPath, JSON.stringify({ ...request, owner_nonce: "x".repeat(43) }));
    assert.equal(consumeAgentUpdateRequest(root), null);
    assert.equal(fs.existsSync(requestPath), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Agent update request invokes the one desktop updater", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "emate-agent-update-watch-"));
  try {
    const request = fixture(root);
    fs.writeFileSync(path.join(root, "desktop-update", "request.json"), JSON.stringify(request));
    let calls = 0;
    const watcher = initAgentUpdateRequests(root, { requestAutomatic: async () => { calls += 1; } });
    try {
      await new Promise((resolve) => setTimeout(resolve, 50));
      assert.equal(calls, 1);
      assert.deepEqual(
        JSON.parse(fs.readFileSync(path.join(root, "desktop-update", "receipt.json"), "utf8")),
        {
          schema_version: 1,
          owner_nonce: request.owner_nonce,
          tool_call_id: request.tool_call_id,
          status: "accepted",
          completed: false,
        },
      );
    } finally {
      watcher.stop();
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
