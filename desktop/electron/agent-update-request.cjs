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

function initAgentUpdateRequests(dataDir, updater) {
  let active = false;
  const check = async () => {
    if (active) return;
    const request = consumeAgentUpdateRequest(dataDir);
    if (!request) return;
    active = true;
    try {
      await updater.requestAutomatic();
    } catch {
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

module.exports = { consumeAgentUpdateRequest, initAgentUpdateRequests };
