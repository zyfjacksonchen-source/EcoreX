const fs = require("node:fs");
const path = require("node:path");
const { runtimeOwnerReceipt } = require("./backend.cjs");

const TOOL_CALL_ID = /^[A-Za-z0-9._:-]{1,252}$/u;

function consumeAgentUpdateRequest(dataDir) {
  const target = path.join(dataDir, "desktop-update", "request.json");
  try {
    const metadata = fs.lstatSync(target);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size < 1 || metadata.size > 2048) return null;
    const value = JSON.parse(fs.readFileSync(target, "utf8"));
    const owner = runtimeOwnerReceipt(dataDir);
    return (
      owner
      && value?.schema_version === 1
      && value.action === "install_latest"
      && value.owner_nonce === owner.nonce
      && typeof value.tool_call_id === "string"
      && TOOL_CALL_ID.test(value.tool_call_id)
      && Object.keys(value).length === 4
    ) ? value : null;
  } catch {
    return null;
  } finally {
    try { fs.unlinkSync(target); } catch { /* No request to consume. */ }
  }
}

function writeAgentUpdateReceipt(dataDir, request, status) {
  const directory = path.join(dataDir, "desktop-update");
  const target = path.join(directory, "receipt.json");
  const temporary = `${target}.${process.pid}.tmp`;
  try {
    const metadata = fs.lstatSync(directory);
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) return false;
    fs.writeFileSync(temporary, `${JSON.stringify({
      schema_version: 1,
      owner_nonce: request.owner_nonce,
      tool_call_id: request.tool_call_id,
      status,
      completed: false,
    })}\n`, { flag: "wx", mode: 0o600 });
    fs.renameSync(temporary, target);
    return true;
  } catch {
    try { fs.unlinkSync(temporary); } catch { /* No partial receipt. */ }
    return false;
  }
}

function initAgentUpdateRequests(dataDir, updater) {
  let active = false;
  const check = async () => {
    if (active) return;
    const request = consumeAgentUpdateRequest(dataDir);
    if (!request) return;
    active = true;
    try {
      const operation = updater.requestAutomatic();
      writeAgentUpdateReceipt(dataDir, request, "accepted");
      await operation;
    } catch {
      writeAgentUpdateReceipt(dataDir, request, "error");
      // The updater already projects a redacted failure to the renderer.
    } finally {
      active = false;
    }
  };
  const interval = setInterval(() => void check(), 250);
  interval.unref();
  void check();
  return { stop: () => clearInterval(interval) };
}

module.exports = { consumeAgentUpdateRequest, initAgentUpdateRequests, writeAgentUpdateReceipt };
