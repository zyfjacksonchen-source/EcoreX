import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { GENERATED_RUNTIME_CONTRACT } from "../src/v1/api/generatedRuntimeContract.ts";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const generator = path.join(desktopRoot, "tools", "generate-runtime-contracts.py");
const schemaPath = path.join(desktopRoot, "src", "v1", "api", "runtime-contract.schema.json");
const manifestPath = path.join(desktopRoot, "src", "v1", "api", "generatedRuntimeContract.ts");

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonical(value[key])]),
    );
  }
  return value;
}

test("generated Runtime contract is current with authoritative Python schemas", () => {
  const python = process.env.PYTHON ?? (process.platform === "win32" ? "python" : "python3");
  const result = spawnSync(python, [generator, "--check"], {
    cwd: desktopRoot,
    encoding: "utf8",
  });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});

test("generated manifest pins the canonical full-schema digest", async () => {
  const schema = JSON.parse(await readFile(schemaPath, "utf8"));
  const manifest = await readFile(manifestPath, "utf8");
  const canonicalBytes = `${JSON.stringify(canonical(schema))}\n`;
  const digest = createHash("sha256").update(canonicalBytes).digest("hex");
  assert.match(manifest, new RegExp(`"schemaSha256": "${digest}"`));
  assert.deepEqual(Object.keys(schema.contracts).sort(), [
    "ArtifactProjection",
    "BootstrapResponse",
    "ConversationUsageProjection",
    "CreateTurnRequest",
    "EventEnvelope",
    "InputAttachmentProjection",
    "InteractionMutationResponse",
    "InteractionRequest",
    "ProjectListResponse",
    "QueueTurnRequest",
    "ReplaceTurnRequest",
    "RespondInteractionRequest",
    "RetouchBody",
    "RetouchWorkspaceSubmitBody",
    "SteerTurnRequest",
    "ThreadProjectionResponse",
    "TurnMutationResponse",
    "TurnProjection",
  ]);
  assert.deepEqual(GENERATED_RUNTIME_CONTRACT.versions, {
    api: "v1",
    eventEnvelope: 1,
    eventSchema: 1,
    extensionContract: "1.0",
    storageSchema: 1,
  });
  assert.deepEqual(
    GENERATED_RUNTIME_CONTRACT.artifact.families,
    schema.public_artifact_policy.families,
  );
  assert.deepEqual(
    GENERATED_RUNTIME_CONTRACT.artifact.visibilities,
    schema.public_artifact_policy.visibilities,
  );
  assert.equal(GENERATED_RUNTIME_CONTRACT.artifact.families.includes("source_code"), false);
  assert.equal(GENERATED_RUNTIME_CONTRACT.artifact.visibilities.includes("internal"), false);
  assert.deepEqual(
    GENERATED_RUNTIME_CONTRACT.wireFields.ArtifactProjection.QualityEvidence,
    ["status", "checks", "score", "summary"],
  );
  assert.equal(
    GENERATED_RUNTIME_CONTRACT.wireFields.BootstrapResponse.BootstrapResponse.includes("models"),
    true,
  );
  assert.deepEqual(
    GENERATED_RUNTIME_CONTRACT.wireFields.ThreadProjectionResponse.ThreadProjectionResponse,
    ["thread", "turns", "items", "jobs", "interactions", "watermark"],
  );
  for (const contract of [
    "CreateTurnRequest",
    "SteerTurnRequest",
    "QueueTurnRequest",
    "ReplaceTurnRequest",
    "TurnProjection",
  ]) {
    const fields = GENERATED_RUNTIME_CONTRACT.wireFields[contract][contract];
    assert.equal(fields.includes("agent_model_id"), true);
    assert.equal(fields.includes("image_model_id"), true);
    assert.equal(fields.includes("model"), false);
  }
});
