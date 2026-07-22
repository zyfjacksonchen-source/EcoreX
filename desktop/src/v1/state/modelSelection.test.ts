import assert from "node:assert/strict";
import test from "node:test";

import type { BootstrapResponse, TurnProjection } from "../api/contracts.ts";
import {
  modelSelectionForMutation,
  preferredModel,
  reconcileModelSelection,
} from "./useRuntimeSession.ts";

const bootstrap = {
  models: {
    snapshot_id: "models_test",
    chat: [{
      model_id: "ecorex-chat",
      display_name: "e-Mate Chat",
      capabilities: ["chat"],
      aliases: [],
      is_default: true,
      model_policy: null,
    }],
    image: [{
      model_id: "gpt-image-2",
      display_name: "e-Mate Image 2",
      capabilities: ["image_generation"],
      aliases: ["image2", "image-2"],
      is_default: true,
      model_policy: null,
    }],
  },
} as unknown as BootstrapResponse;

test("bootstrap selects chat and image defaults before the first Turn", () => {
  assert.equal(preferredModel(bootstrap.models.chat), "ecorex-chat");
  assert.equal(preferredModel(bootstrap.models.image), "gpt-image-2");
  assert.equal(reconcileModelSelection("ecorex-chat", bootstrap.models.chat), "ecorex-chat");
  assert.equal(reconcileModelSelection("removed-model", bootstrap.models.chat), "ecorex-chat");
  assert.equal(reconcileModelSelection("removed-model", []), "");
});

test("automatic image routing never replaces the Agent model and selector changes affect only new Turns", () => {
  const active = {
    turn_id: "turn-one",
    thread_id: "thread-one",
    status: "streaming",
    input: "edit image",
    agent_model_id: "ecorex-chat-frozen",
    image_model_id: "gpt-image-2-frozen",
    client_message_id: "message-one",
    metadata: {},
    terminal_reason: null,
    inherited: false,
    created_at: "2026-07-11T00:00:00Z",
    updated_at: "2026-07-11T00:00:00Z",
  } satisfies TurnProjection;

  assert.deepEqual(
    modelSelectionForMutation(
      "ecorex-chat-new", "gpt-image-3", active, "steer",
    ),
    {
      agentModelId: "ecorex-chat-frozen",
      imageModelId: "gpt-image-2-frozen",
    },
  );
  for (const disposition of ["queue", "replace"] as const) {
    assert.deepEqual(
      modelSelectionForMutation(
        "ecorex-chat-new", "gpt-image-3", active, disposition,
      ),
      { agentModelId: "ecorex-chat-new", imageModelId: "gpt-image-3" },
    );
  }
});
