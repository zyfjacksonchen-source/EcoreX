import assert from "node:assert/strict";
import test from "node:test";

import type {
  ArtifactProjection,
  BootstrapResponse,
  ConversationUsageProjection,
  ConnectorCatalogResponse,
  EventEnvelope,
  ShareSnapshotProjection,
  ThreadProjection,
  RetouchWorkspaceProjection,
} from "./contracts.ts";
import {
  eventClientMessageIds,
  EventCursorResetRequired,
  projectionClientMessageIds,
  RuntimeApiError,
  RuntimeClient,
} from "./runtimeClient.ts";
import {
  ClientOperationConflictError,
  ClientOperationOutbox,
  createClientOperation,
  operationMatchesRetry,
} from "../deferred/clientOperationOutbox.ts";
import {
  RuntimeContractError,
  validateConversationUsageProjection,
  validateEventEnvelope,
} from "./runtimeContract.ts";
import {
  validateConnectorLoginCheckResponse,
  validateInteractionMutationResponse,
  validateThreadProjectionResponse,
} from "./runtimeProjectionContract.ts";
import {
  connectorAuthorizationCompleted,
  connectorOverallHealth,
  connectorSections,
  preferredConnectorAuthKind,
  safeConnectorAuthorizationUrl,
} from "../state/connectors.ts";

test("artifact preview preserves a 404 as a typed Runtime API failure", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({
    error: {
      code: "artifact_preview_not_found",
      message: "Artifact preview was not found.",
    },
  }, { status: 404 });
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    await assert.rejects(
      client.artifactBlob("artifact-missing", "preview"),
      (error: unknown) => error instanceof RuntimeApiError
        && error.status === 404
        && error.code === "artifact_preview_not_found",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

const bootstrap: BootstrapResponse = {
  api_version: "v1",
  event_schema_version: 1,
  storage_schema_version: 1,
  login: {
    authenticated: true,
    account_id: "local",
    display_name: "User",
    organization_id: "org-test",
    roles: ["member"],
    session_revision: 1,
    session_lease_digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  },
  policy_lease: {
    lease_id: "lease_1",
    issued_at: "2026-07-10T00:00:00Z",
    expires_at: "2026-07-13T00:00:00Z",
    duration_hours: 72,
  },
  models: {
    snapshot_id: "models_test",
    chat: [{
      model_id: "ecorex-chat",
      display_name: "GPT-5.6 SOL · 中等推理",
      capabilities: ["chat", "tools", "vision", "reasoning"],
      aliases: ["chat", "default", "gpt-5.6-sol", "gpt5.6-sol"],
      is_default: true,
      model_policy: {
        schema_version: 1,
        policy_id: "ecorex-chat-gpt-5.6-sol",
        policy_version: "1.0.0",
        local_model_id: "ecorex-chat",
        upstream_model_id: "gpt-5.6-sol",
        reasoning_effort: "medium",
        context_management: {
          type: "compaction",
          compact_threshold_tokens: 272_000,
        },
      },
    }],
    image: [{ model_id: "gpt-image-2", display_name: "Image 2", capabilities: [], aliases: ["image2"], is_default: true, model_policy: null }],
    vision: [],
    audio: [],
    embedding: [],
  },
  model_service: { state: "ready", reason: null },
  login_service: { state: "ready", reason: null },
  share_service: { state: "unavailable", reason: "share_service_not_configured" },
  retouch_service: { state: "unavailable", reason: "managed_image_edit_not_configured" },
  quota: { remaining: null, unit: "managed_requests", resets_at: null, limits: {} },
  permissions: {
    snapshot_id: "perm_1",
    profile: "default",
    revision: 1,
    updated_at: "2026-07-10T00:00:00Z",
    sandbox: "workspace-write",
    approval: "on-request",
    full_access: false,
    admin_hard_denies: [],
  },
  connectors: [],
  extensions: {
    snapshot_id: "extensions_test",
    contract_version: "1.0",
    items: [],
  },
  update: {
    current_version: "1.0.0",
    state: "idle",
    target_version: null,
    release_id: null,
    build_digest: null,
    transaction_id: null,
    can_activate: false,
    requires_refresh: false,
    error_code: null,
  },
  csrf_token: "c".repeat(43),
  server_time: "2026-07-10T00:00:00Z",
};

const conversationUsage: ConversationUsageProjection = {
  thread_id: "thr_usage",
  timezone: "Asia/Shanghai",
  today: { input_tokens: 120, output_tokens: 30, total_tokens: 150 },
  week: { input_tokens: 640, output_tokens: 80, total_tokens: 720 },
  context: {
    used_tokens: 120,
    window_tokens: 272_000,
    model_id: "ecorex-chat",
    measured_at: "2026-07-13T01:00:00Z",
  },
  calculated_at: "2026-07-13T01:00:01Z",
};

test("conversation usage is a strict provider-reported Runtime projection", async () => {
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  globalThis.fetch = async (input) => {
    requests.push(String(input));
    return Response.json(conversationUsage);
  };
  try {
    const client = new RuntimeClient({ apiBase: "http://127.0.0.1:8765" });
    assert.deepEqual(await client.conversationUsage("thr_usage"), conversationUsage);
    assert.deepEqual(requests, ["http://127.0.0.1:8765/api/v1/threads/thr_usage/usage"]);
    assert.throws(
      () => validateConversationUsageProjection({
        ...conversationUsage,
        context: { ...conversationUsage.context, unknown: true },
      }),
      (error: unknown) => error instanceof RuntimeContractError
        && error.contract === "ConversationUsageProjection"
        && error.path === "context.unknown",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

class MemorySessionStorage {
  readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

function messageOperation(input: {
  text?: string;
  threadId?: string | null;
  activeTurnId?: string | null;
  disposition?: "steer" | "queue" | "replace";
  operationId?: string;
  clientMessageId?: string;
  now?: Date;
  ttlMilliseconds?: number;
}) {
  return createClientOperation({
    input: input.text ?? "hello",
    threadId: input.threadId === undefined ? "thread-one" : input.threadId,
    activeTurn: input.activeTurnId
      ? { turn_id: input.activeTurnId, status: "streaming" }
      : null,
    disposition: input.disposition ?? "steer",
    models: { agentModelId: "ecorex-chat", imageModelId: "gpt-image-2" },
    observedAfterSeq: 7,
    operationId: input.operationId,
    clientMessageId: input.clientMessageId,
    now: input.now,
    ttlMilliseconds: input.ttlMilliseconds,
  });
}

test("turn.accepted rejects the removed generic model payload", () => {
  const legacy = {
    schema_version: 1,
    event_id: "evt_legacy_model",
    seq: 1,
    thread_id: "thr_1",
    turn_id: "trn_1",
    item_id: null,
    job_id: null,
    tool_call_id: null,
    client_message_id: null,
    causation_id: null,
    correlation_id: null,
    trace_id: null,
    config_snapshot_id: null,
    capability_snapshot_id: null,
    permission_snapshot_id: null,
    extension_snapshot_id: null,
    event_type: "turn.accepted",
    created_at: bootstrap.server_time,
    payload: { input: "legacy", model: "gpt-image-2", metadata: {} },
  };
  assert.throws(
    () => validateEventEnvelope(legacy),
    (error: unknown) => error instanceof RuntimeContractError,
  );
});

test("bootstrap rejects protocol drift before untrusted state reaches the UI", async () => {
  const originalFetch = globalThis.fetch;
  const { api_version: omittedApiVersion, ...malformed } = structuredClone(bootstrap);
  assert.equal(omittedApiVersion, "v1");
  globalThis.fetch = async () => Response.json(malformed);
  try {
    const client = new RuntimeClient({ bearerToken: "b".repeat(43) });
    await assert.rejects(
      client.bootstrap(),
      (error: unknown) => error instanceof RuntimeContractError
        && error.contract === "BootstrapResponse"
        && error.path === "api_version",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bootstrap rejects an unknown extension enum before it reaches feature state", async () => {
  const originalFetch = globalThis.fetch;
  const malformed = structuredClone(bootstrap);
  malformed.extensions.items = [{
    extension_id: "office.example",
    display_name: "Office Example",
    description: "",
    kind: "skill",
    category: "office",
    icon_key: "document",
    active_revision_id: null,
    active_version: null,
    active_digest: null,
    source: "core_bundle",
    trust: "builtin",
    status: "enabled",
    health: "healthy",
    dependencies: [],
    exports: [],
    actions: [],
    last_error_code: null,
    revision: 1,
    updated_at: bootstrap.server_time,
  }];
  Reflect.set(malformed.extensions.items[0]!, "health", "invented");
  globalThis.fetch = async () => Response.json(malformed);
  try {
    const client = new RuntimeClient({ bearerToken: "b".repeat(43) });
    await assert.rejects(
      client.bootstrap(),
      (error: unknown) => error instanceof RuntimeContractError
        && error.contract === "BootstrapResponse"
        && error.path === "extensions.items[0].health",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

const connectorCatalog: ConnectorCatalogResponse = {
  contract_version: "1.0",
  items: [
    {
      definition: {
        connector_id: "feishu",
        contract_version: "1.0",
        display_name: "飞书",
        description: "飞书云文档与消息",
        tier: "stable",
        auth_kinds: ["oauth2"],
        config_schema: {},
        actions: [],
        events: [],
        icon_key: "feishu",
      },
      adapter_available: true,
      instances: [
        {
          instance_id: "instance_feishu",
          connector_id: "feishu",
          account_display_name: "工作账号",
          health: "connected",
          granted_scopes: ["docs:read"],
          available_actions: ["document.read"],
          last_error_code: null,
        },
      ],
      unavailable_reason: null,
    },
    {
      definition: {
        connector_id: "qq_mail",
        contract_version: "1.0",
        display_name: "QQ 邮箱",
        description: "QQ 邮箱 Beta 连接器",
        tier: "beta",
        auth_kinds: ["app_credentials"],
        config_schema: {},
        actions: [],
        events: [],
        icon_key: "mail",
      },
      adapter_available: false,
      instances: [],
      unavailable_reason: "adapter_not_installed",
    },
  ],
};

test("bootstrap bearer is sent and returned CSRF protects later mutations", async () => {
  const requests: Request[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.url.endsWith("/bootstrap")) {
      return Response.json(bootstrap);
    }
    return Response.json({
      thread_id: "thr_1",
      status: "active",
      title: null,
      pinned: false,
      active_turn_status: null,
      metadata: {},
      forked_from_thread_id: null,
      forked_from_turn_id: null,
      forked_from_seq: null,
      created_at: bootstrap.server_time,
      updated_at: bootstrap.server_time,
    }, { status: 201 });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
    });
    const loaded = await client.bootstrap();
    client.acceptBootstrap(loaded);
    await client.createThread(messageOperation({ threadId: null }));

    assert.equal(requests[0].url, "http://127.0.0.1:8765/api/v1/bootstrap");
    assert.equal(requests[1].url, "http://127.0.0.1:8765/api/v1/threads");
    assert.equal(requests[0].headers.get("authorization"), `Bearer ${"b".repeat(43)}`);
    assert.equal(requests[0].headers.get("x-ecorex-csrf"), null);
    assert.equal(requests[1].headers.get("x-ecorex-csrf"), "c".repeat(43));
    assert.equal(requests[1].headers.get("content-type"), "application/json");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("managed device login exposes only public challenge fields and protects begin/poll mutations", async () => {
  const requests: Request[] = [];
  const originalFetch = globalThis.fetch;
  const projection = {
    flow_id: `devflow_${"a".repeat(32)}`,
    status: "pending" as const,
    user_code: "ABCD-EFGH",
    verification_url: "https://login.ecorex.example/device",
    expires_at: "2026-07-10T01:00:00Z",
    poll_interval_seconds: 5,
    next_poll_at: "2026-07-10T00:00:05Z",
    restart_required: false,
    restart_scheduled: false,
    session_generation: null,
    error_code: null,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    return Response.json(projection, { status: request.method === "POST" && request.url.endsWith("/device") ? 202 : 200 });
  };
  try {
    const client = new RuntimeClient({ apiBase: "http://127.0.0.1:8765", bearerToken: "b".repeat(43) });
    client.acceptBootstrap(bootstrap);
    const started = await client.beginDeviceLogin("device-login-stable-id");
    await client.deviceLogin(started.flow_id);
    await client.pollDeviceLogin(started.flow_id, "device-poll-stable-id");

    assert.equal(requests[0].url, "http://127.0.0.1:8765/api/v1/session/device");
    assert.equal(requests[1].url, `http://127.0.0.1:8765/api/v1/session/device/${started.flow_id}`);
    assert.equal(requests[2].url, `http://127.0.0.1:8765/api/v1/session/device/${started.flow_id}/poll`);
    assert.equal(requests[0].headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.equal(requests[1].headers.get("x-ecorex-csrf"), null);
    assert.equal(requests[2].headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.deepEqual(JSON.parse(await requests[0].text()), { client_request_id: "device-login-stable-id" });
    assert.deepEqual(JSON.parse(await requests[2].text()), { client_request_id: "device-poll-stable-id" });
    assert.equal("device_code" in started, false);
    assert.equal("access_token" in started, false);
    assert.equal("refresh_token" in started, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("managed logout carries the current lease digest, CSRF, and one stable intent id", async () => {
  const requests: Request[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    return Response.json({
      authenticated: false,
      generation: 2,
      restart_required: true,
      restart_scheduled: true,
    });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const receipt = await client.logoutSession(
      bootstrap.login.session_lease_digest!,
      "session-logout-stable-id",
    );

    assert.equal(receipt.restart_scheduled, true);
    assert.equal(requests[0].url, "http://127.0.0.1:8765/api/v1/session/logout");
    assert.equal(requests[0].headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.deepEqual(JSON.parse(await requests[0].text()), {
      lease_digest: bootstrap.login.session_lease_digest,
      client_request_id: "session-logout-stable-id",
      confirmed: true,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("permission changes carry CSRF, optimistic revision, and a stable idempotency ID", async () => {
  const requests: Request[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    return Response.json({
      permissions: {
        ...bootstrap.permissions,
        profile: "full_access",
        revision: 2,
        full_access: true,
        sandbox: "danger-full-access",
        approval: "never",
      },
    });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const result = await client.updatePermission(
      "full_access",
      bootstrap.permissions.revision,
      "permission_stable_retry",
    );
    const body = JSON.parse(await requests[0].text());

    assert.equal(requests[0].method, "PUT");
    assert.equal(requests[0].headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.equal(body.profile, "full_access");
    assert.equal(body.expected_revision, 1);
    assert.equal(body.client_request_id, "permission_stable_retry");
    assert.equal(result.permissions.full_access, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("extension catalog and actions use backend projections, revision fencing, and stable identity", async () => {
  const requests: Request[] = [];
  const originalFetch = globalThis.fetch;
  const extension = {
    extension_id: "office-tools",
    display_name: "办公工具",
    description: "办公工具集合",
    kind: "tool_provider" as const,
    category: "office" as const,
    icon_key: "document",
    active_revision_id: "rev_1",
    active_version: "1.0.0",
    active_digest: "a".repeat(64),
    source: "signed_release" as const,
    trust: "verified_publisher" as const,
    status: "enabled" as const,
    health: "healthy" as const,
    dependencies: [],
    exports: [],
    actions: [{
      action_id: "health_check" as const,
      enabled: true,
      disabled_reason: null,
      requires_confirmation: false,
    }],
    last_error_code: null,
    revision: 4,
    updated_at: bootstrap.server_time,
  };
  const snapshot = {
    snapshot_id: "extensions_4",
    contract_version: "1.0" as const,
    items: [extension],
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.method === "GET") return Response.json(snapshot);
    return Response.json({ extension, extensions: snapshot });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const catalog = await client.extensionCatalog();
    const mutated = await client.mutateExtension(
      extension.extension_id,
      "health_check",
      extension.revision,
      "extension-stable-retry",
    );
    const installed = await client.installLocalSkill(
      "local.office-helper",
      "UEsDBA==",
      0,
      "extension-local-install",
    );

    assert.equal(catalog.snapshot_id, snapshot.snapshot_id);
    assert.equal(mutated.extension.extension_id, extension.extension_id);
    assert.equal(installed.extensions.snapshot_id, snapshot.snapshot_id);
    assert.equal(requests[0].url, "http://127.0.0.1:8765/api/v1/extensions");
    assert.equal(requests[0].headers.get("x-ecorex-csrf"), null);
    assert.equal(
      requests[1].url,
      "http://127.0.0.1:8765/api/v1/extensions/office-tools/health",
    );
    assert.equal(requests[1].headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.deepEqual(JSON.parse(await requests[1].text()), {
      expected_revision: 4,
      client_request_id: "extension-stable-retry",
    });
    assert.equal(
      requests[2].url,
      "http://127.0.0.1:8765/api/v1/extensions/local-skills",
    );
    assert.equal(requests[2].headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.deepEqual(JSON.parse(await requests[2].text()), {
      extension_id: "local.office-helper",
      bundle_base64: "UEsDBA==",
      expected_revision: 0,
      client_request_id: "extension-local-install",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("extension revision conflicts preserve the backend code for refresh-before-retry", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({
    detail: {
      code: "extension_revision_conflict",
      message: "扩展修订已经变化。",
      current_revision: 9,
    },
  }, { status: 409 });
  try {
    const client = new RuntimeClient({ apiBase: "http://127.0.0.1:8765" });
    client.acceptBootstrap(bootstrap);
    await assert.rejects(
      client.mutateExtension("office-tools", "disable", 8, "extension-conflict"),
      (error: unknown) => {
        assert.ok(error instanceof RuntimeApiError);
        assert.equal(error.status, 409);
        assert.equal(error.code, "extension_revision_conflict");
        assert.equal(error.message, "扩展修订已经变化。");
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("update check and explicit activation use the authenticated Runtime contract", async () => {
  const requests: Request[] = [];
  const originalFetch = globalThis.fetch;
  const awaiting = {
    ...bootstrap.update,
    state: "awaiting_user" as const,
    target_version: "1.0.1",
    release_id: "release-1.0.1-stable",
    build_digest: "a".repeat(64),
    transaction_id: "transaction-1",
    can_activate: true,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.url.endsWith("/activate")) {
      return Response.json({
        update: { ...awaiting, state: "activating", can_activate: false, requires_refresh: true },
        restart_scheduled: true,
        reload_after_ms: 800,
      });
    }
    return Response.json({ update: awaiting });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    await client.updateStatus();
    await client.checkUpdate();
    const activated = await client.activateUpdate("transaction-1", "activation-stable-id");
    const activationBody = JSON.parse(await requests[2].text());

    assert.equal(requests[0].method, "GET");
    assert.equal(requests[1].method, "POST");
    assert.equal(requests[1].headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.deepEqual(activationBody, {
      transaction_id: "transaction-1",
      confirmed: true,
      client_request_id: "activation-stable-id",
    });
    assert.equal(activated.restart_scheduled, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("authenticated SSE parses facts, ignores watermarks, and sends its cursor", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const fact: EventEnvelope = {
    schema_version: 1,
    event_id: "evt_5",
    seq: 5,
    thread_id: "thr_1",
    turn_id: "trn_1",
    item_id: null,
    job_id: null,
    tool_call_id: null,
    client_message_id: null,
    causation_id: null,
    correlation_id: null,
    trace_id: null,
    config_snapshot_id: null,
    capability_snapshot_id: "cap_1",
    permission_snapshot_id: "perm_1",
    extension_snapshot_id: "ext_1",
    event_type: "turn.status_changed",
    created_at: bootstrap.server_time,
    payload: { to: "completed" },
  };
  globalThis.fetch = async (input, init) => {
    requests.push(new Request(input, init));
    const body = [
      "event: watermark\ndata: {\"watermark\":4}\n\n",
      `id: 5\nevent: turn.status_changed\ndata: ${JSON.stringify(fact)}\n\n`,
      ": keepalive\n\n",
    ].join("");
    return new Response(body, { headers: { "content-type": "text/event-stream" } });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    const events: EventEnvelope[] = [];
    let opened = 0;
    await client.streamEvents(
      "thr_1",
      4,
      (event) => events.push(event),
      new AbortController().signal,
      () => { opened += 1; },
    );
    assert.deepEqual(events, [fact]);
    assert.equal(opened, 1);
    assert.equal(requests[0]?.headers.get("authorization"), `Bearer ${"b".repeat(43)}`);
    assert.equal(requests[0]?.headers.get("last-event-id"), "4");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("SSE rejects an incomplete event envelope instead of partially rendering it", async () => {
  const originalFetch = globalThis.fetch;
  const malformed = {
    schema_version: 1,
    event_id: "evt_incomplete",
    seq: 7,
    thread_id: "thr_1",
    event_type: "turn.status_changed",
  };
  globalThis.fetch = async () => new Response(
    `id: 7\nevent: turn.status_changed\ndata: ${JSON.stringify(malformed)}\n\n`,
    { headers: { "content-type": "text/event-stream" } },
  );
  try {
    const client = new RuntimeClient({ bearerToken: "b".repeat(43) });
    const events: EventEnvelope[] = [];
    await assert.rejects(
      client.streamEvents(
        "thr_1",
        6,
        (event) => events.push(event),
        new AbortController().signal,
      ),
      (error: unknown) => error instanceof RuntimeContractError
        && error.contract === "EventEnvelope",
    );
    assert.deepEqual(events, []);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("SSE rejects header type or id disagreement with the durable envelope", async () => {
  const originalFetch = globalThis.fetch;
  const event: EventEnvelope = {
    schema_version: 1,
    event_id: "evt_8",
    seq: 8,
    thread_id: "thr_1",
    turn_id: "trn_1",
    item_id: null,
    job_id: null,
    tool_call_id: null,
    client_message_id: null,
    causation_id: null,
    correlation_id: null,
    trace_id: null,
    config_snapshot_id: null,
    capability_snapshot_id: null,
    permission_snapshot_id: null,
    extension_snapshot_id: null,
    event_type: "turn.status_changed",
    created_at: bootstrap.server_time,
    payload: { to: "completed" },
  };
  try {
    const client = new RuntimeClient({ bearerToken: "b".repeat(43) });
    globalThis.fetch = async () => new Response(
      `id: 8\nevent: item.completed\ndata: ${JSON.stringify(event)}\n\n`,
      { headers: { "content-type": "text/event-stream" } },
    );
    await assert.rejects(
      client.streamEvents("thr_1", 7, () => undefined, new AbortController().signal),
      (error: unknown) => error instanceof RuntimeApiError
        && error.code === "event_stream_type_mismatch",
    );

    globalThis.fetch = async () => new Response(
      `id: 9\nevent: turn.status_changed\ndata: ${JSON.stringify(event)}\n\n`,
      { headers: { "content-type": "text/event-stream" } },
    );
    await assert.rejects(
      client.streamEvents("thr_1", 7, () => undefined, new AbortController().signal),
      (error: unknown) => error instanceof RuntimeApiError
        && error.code === "event_stream_id_mismatch",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("cursor-ahead response requests a projection resync", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json(
    { detail: "event cursor is ahead of this thread", code: "cursor_ahead", watermark: 2 },
    { status: 409 },
  );
  try {
    const client = new RuntimeClient({ bearerToken: "b".repeat(43) });
    await assert.rejects(
      client.streamEvents("thr_1", 99, () => undefined, new AbortController().signal),
      EventCursorResetRequired,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("artifact list, feedback, and blob use the authenticated Runtime contract", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const artifact: ArtifactProjection = {
    artifact_id: "art_1",
    revision_id: "rev_1",
    family: "image",
    role: "deliverable",
    visibility: "primary",
    status: "ready",
    display_name: "image.png",
    mime_type: "image/png",
    size_bytes: 8,
    sha256: "a".repeat(64),
    created_at: bootstrap.server_time,
    lineage: { source_artifact_ids: [], supersedes_revision_id: null },
    renditions: [],
    actions: ["preview", "download", "feedback", "precise_retouch"],
    feedback: null,
    quality_evidence: {
      status: "not_checked",
      checks: [],
      score: null,
      summary: null,
    },
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.url.includes("/feedback")) {
      return Response.json({
        feedback_id: "feedback_1",
        revision_id: artifact.revision_id,
        signal: "thumbs_up",
        recorded_at: bootstrap.server_time,
      });
    }
    if (request.url.endsWith("/preview")) {
      return new Response(new Uint8Array([1, 2, 3]), {
        headers: { "content-type": "image/png" },
      });
    }
    if (request.url.endsWith("/artifacts/art_1")) return Response.json(artifact);
    return Response.json({ items: [artifact], count: 1 });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const listed = await client.listArtifacts("thread / one");
    const loaded = await client.artifact(artifact.artifact_id);
    await client.artifactFeedback(artifact, "thumbs_up");
    const blob = await client.artifactBlob(artifact.artifact_id, "preview");

    assert.equal(listed.items[0]?.artifact_id, artifact.artifact_id);
    assert.equal(loaded.revision_id, artifact.revision_id);
    assert.match(requests[0]?.url ?? "", /thread_id=thread(?:\+|%20)%2F(?:\+|%20)one/);
    assert.match(requests[1]?.url ?? "", /\/artifacts\/art_1$/);
    assert.equal(requests[2]?.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.equal(requests[3]?.headers.get("authorization"), `Bearer ${"b".repeat(43)}`);
    assert.equal(blob.type, "image/png");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("artifact list rejects internal implementation files at the transport boundary", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({
    items: [{
      artifact_id: "art_internal",
      revision_id: "rev_internal",
      family: "source_code",
      role: "source",
      visibility: "internal",
      status: "ready",
      display_name: "worker.py",
      mime_type: "text/x-python",
      size_bytes: 10,
      sha256: "a".repeat(64),
      created_at: bootstrap.server_time,
      lineage: { source_artifact_ids: [], supersedes_revision_id: null },
      renditions: [],
      actions: [],
      feedback: null,
      quality_evidence: { status: "not_checked", checks: [], score: null, summary: null },
    }],
    count: 1,
  });
  try {
    const client = new RuntimeClient({ bearerToken: "b".repeat(43) });
    await assert.rejects(
      client.listArtifacts(),
      (error: unknown) => error instanceof RuntimeContractError
        && error.contract === "ArtifactProjection"
        && error.path === "items[0].family",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("artifact transports reject count drift and invalid content digests", async () => {
  const originalFetch = globalThis.fetch;
  const artifact: ArtifactProjection = {
    artifact_id: "art_digest",
    revision_id: "rev_digest",
    family: "document",
    role: "deliverable",
    visibility: "primary",
    status: "ready",
    display_name: "report.docx",
    mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    size_bytes: 10,
    sha256: "a".repeat(64),
    created_at: bootstrap.server_time,
    lineage: { source_artifact_ids: [], supersedes_revision_id: null },
    renditions: [],
    actions: ["download"],
    feedback: null,
    quality_evidence: { status: "not_checked", checks: [], score: null, summary: null },
  };
  try {
    const client = new RuntimeClient({ bearerToken: "b".repeat(43) });
    globalThis.fetch = async () => Response.json({ items: [artifact], count: 2 });
    await assert.rejects(
      client.listArtifacts(),
      (error: unknown) => error instanceof RuntimeContractError
        && error.contract === "ArtifactListResponse"
        && error.path === "count",
    );

    globalThis.fetch = async () => Response.json({ ...artifact, sha256: "not-a-digest" });
    await assert.rejects(
      client.artifact(artifact.artifact_id),
      (error: unknown) => error instanceof RuntimeContractError
        && error.contract === "ArtifactProjection"
        && error.path === "root.sha256",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("retouch workspace transport sends only structured ids, geometry, and version fences", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const artifact: ArtifactProjection = {
    artifact_id: "art_target",
    revision_id: "rev_base",
    family: "image",
    role: "deliverable",
    visibility: "primary",
    status: "ready",
    display_name: "poster.png",
    mime_type: "image/png",
    size_bytes: 8,
    sha256: "a".repeat(64),
    created_at: bootstrap.server_time,
    lineage: { source_artifact_ids: [], supersedes_revision_id: null },
    renditions: [],
    actions: ["preview", "download", "precise_retouch"],
    feedback: null,
    quality_evidence: { status: "not_checked", checks: [], score: null, summary: null },
  };
  const workspace: RetouchWorkspaceProjection = {
    workspace_id: "rtw_one",
    artifact_id: artifact.artifact_id,
    version: 1,
    status: "editing",
    edit_surface: {
      base_revision_id: artifact.revision_id,
      raster_digest: artifact.sha256,
      width_px: 1200,
      height_px: 800,
      orientation: 1,
      color_space: "srgb",
      mime_type: "image/png",
      coordinate_space_version: "oriented-normalized-v1",
    },
    annotations: [],
    references: [],
    global_instruction: "",
    view_state: {
      zoom: 1,
      pan_x: 0,
      pan_y: 0,
      selected_annotation_id: null,
      tool: "select",
    },
    mask: null,
    submitted_job_id: null,
    job: null,
    result: null,
    result_surface: null,
    surface_url: "/api/v1/retouch-workspaces/rtw_one/surface",
    result_url: null,
    created_at: bootstrap.server_time,
    updated_at: bootstrap.server_time,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.url.endsWith("/surface")) {
      return new Response(new Uint8Array([1, 2, 3]), { headers: { "content-type": "image/png" } });
    }
    return Response.json(workspace);
  };
  try {
    const client = new RuntimeClient({ apiBase: "http://127.0.0.1:8765" });
    const opened = await client.openRetouchWorkspace(artifact, "open-id");
    await client.saveRetouchWorkspace(opened, {
      annotations: [{
        annotation_id: "ann_one",
        kind: "rectangle",
        normalized_geometry: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
        instruction: "remove object",
      }],
      referenceArtifactIds: ["art_reference"],
      globalInstruction: "keep the rest stable",
      viewState: { zoom: 2, pan_x: 0.5, pan_y: 0.5, tool: "select" },
    }, "save-id");
    await client.submitRetouchWorkspace(
      opened,
      { agentModelId: "ecorex-chat", imageModelId: "gpt-image-2" },
      "submit-id",
    );
    const blob = await client.retouchWorkspaceBlob(opened.workspace_id, "surface");

    assert.equal(blob.type, "image/png");
    const openBody = JSON.parse(await requests[0]!.clone().text());
    const saveBody = JSON.parse(await requests[1]!.clone().text());
    const submitBody = JSON.parse(await requests[2]!.clone().text());
    assert.deepEqual(openBody, { base_revision_id: "rev_base", client_request_id: "open-id" });
    assert.equal(saveBody.expected_version, 1);
    assert.deepEqual(saveBody.reference_artifact_ids, ["art_reference"]);
    assert.equal(saveBody.annotations[0].annotation_id, "ann_one");
    assert.equal(saveBody.prompt, undefined);
    assert.equal(saveBody.path, undefined);
    assert.deepEqual(submitBody, {
      expected_version: 1,
      agent_model_id: "ecorex-chat",
      image_model_id: "gpt-image-2",
      client_request_id: "submit-id",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("artifact open and reveal submit only backend identities, never a filesystem path", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    const action = request.url.endsWith("/reveal") ? "reveal" : "open";
    return Response.json({
      artifact_id: "art / one",
      revision_id: "rev_1",
      action,
      client_request_id: (await request.clone().json()).client_request_id,
      status: "completed",
      requested_at: bootstrap.server_time,
      updated_at: bootstrap.server_time,
      failure_code: null,
    });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    await client.artifactExternalAction("art / one", "open", "action-id");
    await client.artifactExternalAction("art / one", "reveal", "action-id-2");

    assert.match(requests[0]?.url ?? "", /artifacts\/art%20%2F%20one\/actions\/open$/);
    assert.match(requests[1]?.url ?? "", /artifacts\/art%20%2F%20one\/actions\/reveal$/);
    assert.deepEqual(await requests[0]?.json(), { client_request_id: "action-id" });
    assert.deepEqual(await requests[1]?.json(), { client_request_id: "action-id-2" });
    assert.equal(requests[0]?.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("turn mutations always send separate Agent and image model identities", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    const replacement = {
      turn_id: "turn-replacement",
      thread_id: "thread-one",
      status: "queued",
      input: "replace",
      agent_model_id: "ecorex-chat",
      image_model_id: "gpt-image-2",
      client_message_id: "message-replacement",
      metadata: {},
      terminal_reason: null,
      inherited: false,
      created_at: bootstrap.server_time,
      updated_at: bootstrap.server_time,
    };
    if (request.url.endsWith("/replace")) {
      return Response.json({
        superseded_turn: {
          ...replacement,
          turn_id: "turn-one",
          status: "superseded",
          terminal_reason: "replaced_by_user",
        },
        replacement_turn: replacement,
        job: {
          job_id: "job-replacement",
          kind: "agent_turn",
          status: "queued",
          priority: 0,
          attempt: 0,
          max_attempts: 3,
          thread_id: "thread-one",
          turn_id: "turn-replacement",
          available_at: bootstrap.server_time,
          deadline: null,
          reason_code: null,
          created_at: bootstrap.server_time,
          updated_at: bootstrap.server_time,
        },
        watermark: 4,
      });
    }
    return Response.json({
      turn: { ...replacement, turn_id: "turn-mutation", input: "mutation" },
      job: null,
      watermark: 3,
    });
  };
  try {
    const client = new RuntimeClient({ apiBase: "http://127.0.0.1:8765" });
    await client.createTurn("thread-one", messageOperation({ text: "create" }));
    await client.steerTurn(messageOperation({
      text: "steer",
      activeTurnId: "turn-one",
      disposition: "steer",
    }));
    await client.queueTurn("thread-one", messageOperation({
      text: "queue",
      activeTurnId: "turn-one",
      disposition: "queue",
    }));
    await client.replaceTurn(messageOperation({
      text: "replace",
      activeTurnId: "turn-one",
      disposition: "replace",
    }));

    assert.equal(requests.length, 4);
    for (const request of requests) {
      const body = JSON.parse(await request.clone().text());
      assert.equal(body.agent_model_id, "ecorex-chat");
      assert.equal(body.image_model_id, "gpt-image-2");
      assert.equal(body.model, undefined);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("client operations freeze the active Turn, models, and request identity", () => {
  const active = { turn_id: "turn-frozen", status: "streaming" };
  const models = { agentModelId: "ecorex-chat", imageModelId: "gpt-image-2" };
  const operation = createClientOperation({
    input: "  keep this intent  ",
    threadId: "thread-frozen",
    activeTurn: active,
    disposition: "steer",
    models,
    observedAfterSeq: 19,
    attachments: [{
      attachment_id: "attachment_frozen",
      revision_id: "revision_frozen",
      display_name: "brief.txt",
      mime_type: "text/plain",
      size_bytes: 12,
      media_kind: "document",
      sha256: "a".repeat(64),
      created_at: "2026-07-11T00:00:00.000Z",
    }],
    operationId: "operation_frozen",
    clientMessageId: "message_frozen",
    now: new Date("2026-07-11T00:00:00Z"),
  });

  active.turn_id = "turn-changed";
  models.agentModelId = "different-model";
  assert.equal(operation.turn?.turn_id, "turn-frozen");
  assert.equal(operation.models.agentModelId, "ecorex-chat");
  assert.equal(operation.input, "keep this intent");
  assert.equal(operation.operation_id, "operation_frozen");
  assert.equal(operation.client_message_id, "message_frozen");
  assert.equal(operation.observed_after_seq, 19);
  assert.equal(operation.attachments[0]?.attachment_id, "attachment_frozen");
  assert.equal(Object.isFrozen(operation), true);
  assert.equal(Object.isFrozen(operation.models), true);
  assert.equal(Object.isFrozen(operation.turn), true);
});

test("session outbox survives reload, deduplicates repeated clicks, and rejects identity drift", () => {
  const storage = new MemorySessionStorage();
  const now = new Date("2026-07-11T00:00:00Z");
  const operation = messageOperation({
    text: "send once",
    operationId: "operation_stable",
    clientMessageId: "message_stable",
    now,
  });
  const first = new ClientOperationOutbox({ storage, now: () => now });
  first.stage(operation);
  first.stage(operation);

  const reloaded = new ClientOperationOutbox({ storage, now: () => now });
  assert.equal(reloaded.list().length, 1);
  assert.equal(reloaded.list()[0]?.operation.client_message_id, "message_stable");

  const changed = messageOperation({
    text: "different payload",
    operationId: "operation_stable",
    clientMessageId: "message_stable",
    now,
  });
  assert.throws(
    () => reloaded.stage(changed),
    (error: unknown) => error instanceof ClientOperationConflictError,
  );

  const serialized = [...storage.values.values()][0] ?? "";
  // Durable retry keeps only opaque Runtime-issued input references; it never
  // stores browser file bytes, local paths, credentials, or server responses.
  assert.doesNotMatch(serialized, /bearerToken|csrfToken|server_response|data:[^,]+;base64|[A-Za-z]:\\/u);
  assert.match(serialized, /"version":1/u);
});

test("retry keeps the persisted Turn target even after live state advances", () => {
  const now = new Date("2026-07-11T00:00:00Z");
  const operation = messageOperation({
    text: "continue the same request",
    threadId: "thread-frozen",
    activeTurnId: "turn-original",
    disposition: "replace",
    operationId: "operation_retry_frozen",
    clientMessageId: "message_retry_frozen",
    now,
  });
  const storage = new MemorySessionStorage();
  const outbox = new ClientOperationOutbox({ storage, now: () => now });
  const record = outbox.stage(operation);

  assert.equal(
    operationMatchesRetry(record, "continue the same request", "thread-frozen"),
    true,
  );
  assert.equal(operation.turn?.turn_id, "turn-original");
  assert.equal(operation.disposition, "replace");
  assert.equal(
    operationMatchesRetry(record, "continue the same request", "thread-other"),
    false,
  );
  assert.equal(
    operationMatchesRetry(record, "changed request", "thread-frozen"),
    false,
  );
});

test("an unresolved first-message retry cannot jump into a newly selected Thread", () => {
  const now = new Date("2026-07-11T00:00:00Z");
  const operation = messageOperation({
    text: "first message",
    threadId: null,
    operationId: "operation_unresolved_retry",
    clientMessageId: "message_unresolved_retry",
    now,
  });
  const storage = new MemorySessionStorage();
  const outbox = new ClientOperationOutbox({ storage, now: () => now });
  const record = outbox.stage(operation);

  assert.equal(operationMatchesRetry(record, "first message", null), true);
  assert.equal(
    operationMatchesRetry(record, "first message", "thread-selected-later"),
    false,
  );
});

test("new-thread first message is a recoverable two-phase operation after lost responses", async () => {
  const storage = new MemorySessionStorage();
  const now = new Date("2026-07-11T00:00:00Z");
  const operation = messageOperation({
    text: "first message",
    threadId: null,
    operationId: "operation_two_phase",
    clientMessageId: "message_two_phase",
    now,
  });
  const acceptedThreadRequestIds = new Set<string>();
  const acceptedMessageIds = new Set<string>();
  let loseThreadResponse = true;
  let loseMessageResponse = true;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    const body = JSON.parse(await request.clone().text());
    if (request.url.endsWith("/api/v1/threads")) {
      acceptedThreadRequestIds.add(body.client_request_id);
      if (loseThreadResponse) {
        loseThreadResponse = false;
        throw new TypeError("connection closed after commit");
      }
      return Response.json({
        thread_id: "thread-two-phase",
        status: "active",
        title: null,
        pinned: false,
        active_turn_status: null,
        metadata: {},
        forked_from_thread_id: null,
        forked_from_turn_id: null,
        forked_from_seq: null,
        created_at: now.toISOString(),
        updated_at: now.toISOString(),
      }, { status: 201 });
    }
    acceptedMessageIds.add(body.client_message_id);
    if (loseMessageResponse) {
      loseMessageResponse = false;
      throw new TypeError("connection closed after commit");
    }
    return Response.json({
      turn: {
        turn_id: "turn-two-phase",
        thread_id: "thread-two-phase",
        status: "queued",
        input: "first message",
        agent_model_id: "ecorex-chat",
        image_model_id: "gpt-image-2",
        client_message_id: "message_two_phase",
        metadata: {},
        terminal_reason: null,
        inherited: false,
        created_at: now.toISOString(),
        updated_at: now.toISOString(),
      },
      job: null,
      watermark: 3,
    });
  };
  try {
    const client = new RuntimeClient({ apiBase: "http://127.0.0.1:8765" });
    const firstLoad = new ClientOperationOutbox({ storage, now: () => now });
    firstLoad.stage(operation);
    await assert.rejects(client.createThread(operation), /connection closed/u);

    const afterThreadReload = new ClientOperationOutbox({ storage, now: () => now });
    const recovered = afterThreadReload.list()[0]!;
    assert.equal(recovered.resolved_thread_id, null);
    const thread = await client.createThread(recovered.operation);
    afterThreadReload.resolveThread(recovered.operation.operation_id, thread.thread_id);
    await assert.rejects(
      client.createTurn(thread.thread_id, recovered.operation),
      /connection closed/u,
    );

    const afterMessageReload = new ClientOperationOutbox({ storage, now: () => now });
    const pending = afterMessageReload.list()[0]!;
    assert.equal(pending.resolved_thread_id, "thread-two-phase");
    await client.createTurn(pending.resolved_thread_id!, pending.operation);
    assert.deepEqual(afterMessageReload.acknowledge(["message_two_phase"]), [
      "operation_two_phase",
    ]);
    assert.equal(afterMessageReload.list().length, 0);
    assert.deepEqual([...acceptedThreadRequestIds], ["operation_two_phase"]);
    assert.deepEqual([...acceptedMessageIds], ["message_two_phase"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("outbox confirmation is atomic and expiration cleanup is bounded", () => {
  const storage = new MemorySessionStorage();
  const createdAt = new Date("2026-07-11T00:00:00Z");
  const operation = messageOperation({
    operationId: "operation_expiring",
    clientMessageId: "message_expiring",
    now: createdAt,
    ttlMilliseconds: 1_000,
  });
  const outbox = new ClientOperationOutbox({
    storage,
    maxRecords: 1,
    now: () => createdAt,
  });
  outbox.stage(operation);
  assert.deepEqual(outbox.acknowledge(["different-message"]), []);
  assert.equal(outbox.list().length, 1);
  assert.throws(
    () => outbox.stage(messageOperation({
      operationId: "operation_overflow",
      clientMessageId: "message_overflow",
      now: createdAt,
    })),
    /待发送消息过多/u,
  );

  const expired = new ClientOperationOutbox({
    storage,
    now: () => new Date("2026-07-11T00:00:02Z"),
  });
  assert.deepEqual(expired.list(), []);
  assert.equal(storage.values.size, 0);
});

test("only durable projection or event facts expose message confirmations", () => {
  const confirmedTurn = {
    turn_id: "turn-confirmed",
    thread_id: "thread-one",
    status: "queued" as const,
    input: "hello",
    agent_model_id: "ecorex-chat",
    image_model_id: "gpt-image-2",
    client_message_id: "message-from-projection",
    metadata: {},
    terminal_reason: null,
    inherited: false,
    created_at: bootstrap.server_time,
    updated_at: bootstrap.server_time,
  };
  const event: EventEnvelope = {
    schema_version: 1,
    event_id: "event-confirmed",
    seq: 9,
    thread_id: "thread-one",
    turn_id: "turn-confirmed",
    item_id: null,
    job_id: null,
    tool_call_id: null,
    client_message_id: "message-from-event",
    causation_id: null,
    correlation_id: null,
    trace_id: null,
    config_snapshot_id: null,
    capability_snapshot_id: null,
    permission_snapshot_id: null,
    extension_snapshot_id: null,
    event_type: "turn.steered",
    created_at: bootstrap.server_time,
    payload: {},
  };
  assert.deepEqual(
    projectionClientMessageIds({ turns: [confirmedTurn, { ...confirmedTurn, client_message_id: null }] }),
    ["message-from-projection"],
  );
  assert.deepEqual(eventClientMessageIds([event, { ...event, client_message_id: null }]), [
    "message-from-event",
  ]);
});

test("thread projections reject stale nested wire shapes before reducer state", () => {
  const projection = {
    thread: {
      thread_id: "thread-contract",
      status: "active",
      title: null,
      pinned: false,
      active_turn_status: null,
      metadata: {},
      forked_from_thread_id: null,
      forked_from_turn_id: null,
      forked_from_seq: null,
      created_at: bootstrap.server_time,
      updated_at: bootstrap.server_time,
    },
    turns: [{
      turn_id: "turn-contract",
      thread_id: "thread-contract",
      status: "queued",
      input: "hello",
      agent_model_id: "ecorex-chat",
      image_model_id: null,
      client_message_id: "message-contract",
      metadata: {},
      terminal_reason: null,
      inherited: false,
      created_at: bootstrap.server_time,
      updated_at: bootstrap.server_time,
    }],
    items: [],
    jobs: [],
    interactions: [],
    watermark: 3,
  };
  assert.equal(
    validateThreadProjectionResponse(projection).turns[0]?.turn_id,
    "turn-contract",
  );
  const stale = structuredClone(projection);
  Reflect.deleteProperty(stale.turns[0]!, "inherited");
  assert.throws(
    () => validateThreadProjectionResponse(stale),
    (error: unknown) => error instanceof RuntimeContractError
      && error.contract === "TurnProjection"
      && error.path === "turns[0].inherited",
  );
  const crossThread = structuredClone(projection);
  crossThread.turns[0]!.thread_id = "different-thread";
  assert.throws(
    () => validateThreadProjectionResponse(crossThread),
    (error: unknown) => error instanceof RuntimeContractError
      && error.contract === "ThreadProjectionResponse"
      && error.path === "turns[0].thread_id",
  );
});

test("event pages retain the caller cursor used to confirm a pending message", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  globalThis.fetch = async (input, init) => {
    requests.push(new Request(input, init));
    return Response.json({ events: [], after_seq: 17, watermark: 19, has_more: false });
  };
  try {
    const client = new RuntimeClient({ apiBase: "http://127.0.0.1:8765" });
    const page = await client.eventPage("thread / one", 17);
    assert.equal(page.watermark, 19);
    assert.match(requests[0]?.url ?? "", /threads\/thread%20%2F%20one\/events\?after_seq=17&limit=1000$/u);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("connector catalog and lifecycle mutations use strict authenticated routes", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const instance = connectorCatalog.items[0]!.instances[0]!;
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.method === "GET") return Response.json(connectorCatalog);
    if (request.url.endsWith("/auth/begin") || request.url.endsWith("/reauthorize")) {
      return Response.json({
        flow_id: "flow_1",
        connector_id: "feishu/docs",
        auth_kind: "oauth2",
        expires_at: "2026-07-10T01:00:00Z",
        authorization_url: "https://open.feishu.cn/open-apis/authen/v1/authorize?state=s&code_challenge=c&code_challenge_method=S256",
        user_code: null,
        verification_url: null,
      });
    }
    if (request.url.endsWith("/health")) return Response.json(instance);
    return new Response(null, { status: 204 });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const catalog = await client.connectorCatalog();
    const challenge = await client.beginConnectorAuth(
      "feishu/docs",
      "oauth2",
      "connector_auth_stable",
    );
    await client.reauthorizeConnector(
      "instance / one",
      "oauth2",
      "connector_reauthorize_stable",
    );
    await client.refreshConnectorHealth("instance / one", "connector_health_stable");
    await client.disconnectConnector("instance / one", "connector_disconnect_stable");

    assert.equal(catalog.contract_version, "1.0");
    assert.equal(challenge.flow_id, "flow_1");
    assert.match(requests[1]?.url ?? "", /connectors\/feishu%2Fdocs\/auth\/begin$/);
    assert.deepEqual(JSON.parse(await requests[1]!.text()), { auth_kind: "oauth2" });
    assert.equal(requests[1]?.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.equal(
      requests[1]?.headers.get("x-ecorex-client-request-id"),
      "connector_auth_stable",
    );
    assert.match(requests[2]?.url ?? "", /instances\/instance%20%2F%20one\/reauthorize$/);
    assert.equal(requests[2]?.method, "POST");
    assert.equal(
      requests[2]?.headers.get("x-ecorex-client-request-id"),
      "connector_reauthorize_stable",
    );
    assert.match(requests[3]?.url ?? "", /instances\/instance%20%2F%20one\/health$/);
    assert.equal(requests[3]?.method, "POST");
    assert.equal(
      requests[3]?.headers.get("x-ecorex-client-request-id"),
      "connector_health_stable",
    );
    assert.equal(requests[4]?.method, "DELETE");
    assert.equal(
      requests[4]?.headers.get("x-ecorex-client-request-id"),
      "connector_disconnect_stable",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("connector view helpers preserve tier, OAuth, health, and URL authority", () => {
  const sections = connectorSections(connectorCatalog.items);
  assert.deepEqual(sections.map((section) => section.tier), ["stable", "beta"]);
  assert.equal(sections[0]?.items[0]?.definition.connector_id, "feishu");
  assert.equal(preferredConnectorAuthKind(connectorCatalog.items[0]!), "oauth2");
  assert.equal(preferredConnectorAuthKind(connectorCatalog.items[1]!), null);
  assert.equal(connectorOverallHealth(connectorCatalog.items[0]!), "connected");

  const authorizationUrl = "https://open.feishu.cn/authorize?state=one&code_challenge=two";
  assert.equal(safeConnectorAuthorizationUrl(authorizationUrl), authorizationUrl);
  assert.throws(
    () => safeConnectorAuthorizationUrl("http://open.feishu.cn/authorize"),
      /安全检查/,
  );
  assert.throws(
    () => safeConnectorAuthorizationUrl("https://user:secret@open.feishu.cn/authorize"),
    /安全检查/,
  );
  assert.throws(
    () => safeConnectorAuthorizationUrl("https://open.feishu.cn/authorize#token"),
    /安全检查/,
  );

  assert.equal(
    connectorAuthorizationCompleted(connectorCatalog, "feishu", new Set()),
    true,
  );
  assert.equal(
    connectorAuthorizationCompleted(
      connectorCatalog,
      "feishu",
      new Set(["instance_feishu"]),
    ),
    false,
  );
  assert.equal(
    connectorAuthorizationCompleted(
      connectorCatalog,
      "feishu",
      new Set(["instance_feishu"]),
      "instance_feishu",
    ),
    true,
  );
});

test("thread catalog mutations preserve backend order and authenticated idempotency", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const thread: ThreadProjection = {
    thread_id: "thr / one",
    status: "active",
    title: "月度经营复盘",
    pinned: false,
    active_turn_status: null,
    metadata: {},
    forked_from_thread_id: null,
    forked_from_turn_id: null,
    forked_from_seq: null,
    created_at: bootstrap.server_time,
    updated_at: bootstrap.server_time,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.method === "GET") {
      return Response.json({ items: [thread], next_cursor: "cursor-signed" });
    }
    return Response.json({
      ...thread,
      status: request.url.endsWith("/archive") ? "archived" : "active",
    });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const listed = await client.listThreads("active", 25, "signed cursor / one");
    await client.renameThread(thread.thread_id, "新名称", "rename-stable");
    await client.setThreadArchived(thread.thread_id, true, "archive-stable");
    await client.setThreadArchived(thread.thread_id, false, "restore-stable");

    assert.deepEqual(listed.items, [thread]);
    assert.equal(listed.next_cursor, "cursor-signed");
    assert.match(requests[0]!.url, /status=active/);
    assert.match(requests[0]!.url, /limit=25/);
    assert.match(requests[0]!.url, /cursor=signed(?:\+|%20)cursor(?:\+|%20)%2F(?:\+|%20)one/);
    assert.match(requests[1]!.url, /threads\/thr%20%2F%20one$/);
    assert.equal(requests[1]!.method, "PUT");
    assert.deepEqual(JSON.parse(await requests[1]!.text()), {
      title: "新名称",
      client_request_id: "rename-stable",
    });
    assert.match(requests[2]!.url, /threads\/thr%20%2F%20one\/archive$/);
    assert.match(requests[3]!.url, /threads\/thr%20%2F%20one\/restore$/);
    const preferencePayload = JSON.parse(await requests[2]!.text());
    assert.deepEqual(preferencePayload, {
      client_request_id: "archive-stable",
    });
    assert.deepEqual(JSON.parse(await requests[3]!.text()), {
      client_request_id: "restore-stable",
    });
    for (const request of requests.slice(1)) {
      assert.equal(request.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("ShareSnapshot lifecycle never reuses a thread route as a public identity", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const share: ShareSnapshotProjection = {
    share_id: "shr_0123456789abcdef0123456789abcdef",
    thread_id: "thr / one",
    source_watermark: 12,
    status: "published",
    public_url: "https://share.ecorex.example/s/token-one",
    expires_at: "2026-07-17T00:00:00Z",
    created_at: bootstrap.server_time,
    updated_at: bootstrap.server_time,
    revoked_at: null,
    error_code: null,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.method === "GET" && request.url.includes("/threads/")) {
      return Response.json({ items: [share], count: 1 });
    }
    if (request.url.endsWith("/revoke")) {
      return Response.json({ ...share, status: "revoked", public_url: null });
    }
    return Response.json(share, { status: request.method === "POST" ? 201 : 200 });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const listed = await client.listShares("thr / one");
    const created = await client.createShare("thr / one", 168, "share-create-stable");
    const loaded = await client.share(share.share_id);
    const revoked = await client.revokeShare(share.share_id, "share-revoke-stable");

    assert.equal(listed.count, 1);
    assert.equal(created.share_id, share.share_id);
    assert.equal(loaded.public_url, share.public_url);
    assert.equal(revoked.status, "revoked");
    assert.match(requests[0]!.url, /threads\/thr%20%2F%20one\/shares$/);
    assert.equal(requests[0]!.method, "GET");
    assert.equal(requests[1]!.method, "POST");
    assert.deepEqual(JSON.parse(await requests[1]!.text()), {
      expires_in_hours: 168,
      client_request_id: "share-create-stable",
    });
    assert.match(requests[2]!.url, /shares\/shr_0123456789abcdef0123456789abcdef$/);
    assert.match(requests[3]!.url, /shares\/shr_0123456789abcdef0123456789abcdef\/revoke$/);
    assert.deepEqual(JSON.parse(await requests[3]!.text()), {
      client_request_id: "share-revoke-stable",
    });
    assert.equal(requests[1]!.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.equal(requests[3]!.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Replay transport keeps Mock read-only and sends an explicitly confirmed stable Live request", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const thread: ThreadProjection = {
    thread_id: "thread / replay",
    status: "active",
    title: "诊断任务",
    pinned: false,
    active_turn_status: null,
    metadata: {},
    forked_from_thread_id: null,
    forked_from_turn_id: null,
    forked_from_seq: null,
    created_at: bootstrap.server_time,
    updated_at: bootstrap.server_time,
  };
  const turn = {
    turn_id: "turn / source",
    thread_id: thread.thread_id,
    status: "completed" as const,
    input: "生成报告",
    agent_model_id: "ecorex-chat",
    image_model_id: "gpt-image-2",
    client_message_id: "message-source",
    metadata: {},
    terminal_reason: "completed",
    created_at: bootstrap.server_time,
    updated_at: bootstrap.server_time,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.method === "GET") {
      return Response.json({
        projection: { thread, turns: [turn], items: [], watermark: 7 },
        interactions: [],
        live_replay_turn_ids: [turn.turn_id],
        source_watermark: 7,
        through_seq: 7,
        event_count: 7,
        event_digest: "a".repeat(64),
      });
    }
    return Response.json({
      source_thread_id: thread.thread_id,
      source_turn_id: turn.turn_id,
      causation_event_id: "event-source",
      replay: {
        turn: { ...turn, turn_id: "turn-replay", status: "queued" },
        job: null,
        watermark: 8,
      },
      permission_snapshot_id: "permission-current",
    }, { status: 202 });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const mock = await client.mockReplay(thread.thread_id);
    const live = await client.liveReplay(
      thread.thread_id,
      turn.turn_id,
      "live-replay-stable",
    );

    assert.equal(mock.event_digest, "a".repeat(64));
    assert.equal(live.replay.turn.turn_id, "turn-replay");
    assert.match(requests[0]!.url, /threads\/thread%20%2F%20replay\/replay$/);
    assert.equal(requests[0]!.method, "GET");
    assert.equal(requests[0]!.headers.get("x-ecorex-csrf"), null);
    assert.match(requests[1]!.url, /threads\/thread%20%2F%20replay\/replay\/live$/);
    assert.equal(requests[1]!.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.deepEqual(JSON.parse(await requests[1]!.text()), {
      source_turn_id: "turn / source",
      confirmed: true,
      client_request_id: "live-replay-stable",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("system health keeps primary status separate from bounded technical history", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const sample = {
    sample_id: "syssample_1",
    overall: "healthy" as const,
    summary: "EcoreX 运行正常",
    components: [
      {
        component_id: "responsiveness",
        label: "运行响应",
        status: "healthy" as const,
        message: "界面和后台响应正常。",
      },
    ],
    sampled_at: bootstrap.server_time,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.url.includes("/metrics")) {
      return Response.json({ items: [{
        ...sample,
        metrics: { runtime: { sse_connections: 0 }, process: {}, storage: {}, services: {} },
      }] });
    }
    return Response.json(
      request.url.includes("technical=true")
        ? {
            ...sample,
            metrics: { runtime: { sse_connections: 0 }, process: {}, storage: {}, services: {} },
          }
        : sample,
    );
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
    });
    const primary = await client.systemHealth();
    const technical = await client.systemHealth({ technical: true });
    const history = await client.systemMetrics(999);

    assert.equal(primary.metrics, undefined);
    assert.equal(technical.metrics?.runtime instanceof Object, true);
    assert.equal(history.items.length, 1);
    assert.match(requests[0]!.url, /\/system\/health$/);
    assert.match(requests[1]!.url, /\/system\/health\?technical=true$/);
    assert.match(requests[2]!.url, /\/system\/metrics\?limit=200$/);
    assert.equal(requests.every((request) => request.method === "GET"), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("output location and materialization send aliases and artifact identities only", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.url.endsWith("/locations")) {
      return Response.json({ items: [
        { alias: "documents", available: true },
        { alias: "downloads", available: true },
        { alias: "workspace", available: true },
      ] });
    }
    if (request.method === "GET") {
      return Response.json({
        account_id: "account-ga",
        location_alias: "documents",
        revision: 1,
        output_policy_snapshot_id: `outpol_${"1".repeat(64)}`,
        updated_at: bootstrap.server_time,
      });
    }
    if (request.method === "PUT") {
      return Response.json({
        account_id: "account-ga",
        location_alias: "downloads",
        revision: 2,
        output_policy_snapshot_id: `outpol_${"2".repeat(64)}`,
        updated_at: bootstrap.server_time,
      });
    }
    return Response.json({
      materialization_id: `mat_${"3".repeat(64)}`,
      artifact_id: "artifact_1",
      revision_id: "revision_1",
      output_policy_snapshot_id: `outpol_${"1".repeat(64)}`,
      location_alias: "documents",
      display_name: "报告.pdf",
      sha256: "a".repeat(64),
      size_bytes: 12,
      status: "completed",
      reused_existing: false,
      created_at: bootstrap.server_time,
      completed_at: bootstrap.server_time,
    });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    await client.outputLocations();
    await client.outputPreference();
    await client.updateOutputPreference("downloads", 1, "output-location-stable");
    await client.materializeArtifact(
      { artifact_id: "artifact_1", revision_id: "revision_1" },
      "output-artifact-stable",
    );

    const preferencePayload = await requests[2]!.clone().json();
    const materializationPayload = await requests[3]!.clone().json();
    assert.deepEqual(preferencePayload, {
      location_alias: "downloads",
      expected_revision: 1,
      client_request_id: "output-location-stable",
    });
    assert.deepEqual(materializationPayload, {
      revision_id: "revision_1",
      client_request_id: "output-artifact-stable",
    });
    assert.equal(JSON.stringify(preferencePayload).includes("C:\\"), false);
    assert.equal(requests[2]!.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.equal(requests[3]!.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("legacy credential quarantine exposes summaries and deletes by stable intent only", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    return Response.json({
      status: request.method === "POST" ? "deleted" : "available",
      entry_count: 2,
      can_delete: request.method !== "POST",
      deleted_at: request.method === "POST" ? bootstrap.server_time : null,
      items: [
        { kind: "api_key", origin: "product_configuration", count: 1 },
        { kind: "access_token", origin: "mcp_configuration", count: 1 },
      ],
    });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const available = await client.migrationQuarantine();
    const deleted = await client.deleteMigrationQuarantine("delete-quarantine-stable");

    assert.equal(available.items[0]?.kind, "api_key");
    assert.equal(deleted.status, "deleted");
    assert.match(requests[0]!.url, /\/migration\/quarantine$/);
    assert.equal(requests[0]!.method, "GET");
    assert.equal(requests[0]!.headers.get("x-ecorex-csrf"), null);
    assert.match(requests[1]!.url, /\/migration\/quarantine\/delete$/);
    assert.equal(requests[1]!.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
    assert.deepEqual(await requests[1]!.clone().json(), {
      confirmed: true,
      client_request_id: "delete-quarantine-stable",
    });
    assert.equal((await requests[1]!.clone().text()).includes("path"), false);
    assert.equal((await requests[1]!.clone().text()).includes("secret"), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("settings transports reject malformed, extra, and cross-identity Runtime data", async () => {
  const originalFetch = globalThis.fetch;
  const publicHealth = {
    sample_id: "syssample_1",
    overall: "healthy",
    summary: "EcoreX 运行正常",
    components: [{
      component_id: "runtime",
      label: "运行响应",
      status: "healthy",
      message: "运行正常。",
    }],
    sampled_at: bootstrap.server_time,
  };
  const materialization = {
    materialization_id: `mat_${"3".repeat(64)}`,
    artifact_id: "artifact-other",
    revision_id: "revision-expected",
    output_policy_snapshot_id: `outpol_${"1".repeat(64)}`,
    location_alias: "documents",
    display_name: "报告.pdf",
    sha256: "a".repeat(64),
    size_bytes: 12,
    status: "completed",
    reused_existing: false,
    created_at: bootstrap.server_time,
    completed_at: bootstrap.server_time,
  };
  const payloads: unknown[] = [
    {
      revision: 0,
      active_learned_records: 1,
      active_user_files: 1,
      factory_records: 0,
      tombstoned_records: 0,
      tombstoned_files: 0,
      resettable_count: 99,
      latest_reset: null,
    },
    {
      status: "available",
      entry_count: 1,
      can_delete: true,
      deleted_at: null,
      items: [{ kind: "api_key", origin: "product_configuration", count: 1, path: "secret" }],
    },
    { items: [{ alias: "documents", available: true }] },
    {
      account_id: "account-ga",
      location_alias: "documents",
      revision: 1,
      output_policy_snapshot_id: "mutable-policy",
      updated_at: bootstrap.server_time,
    },
    materialization,
    { ...publicHealth, metrics: {} },
    { ...publicHealth, metrics: { runtime: {} } },
    { items: [publicHealth] },
  ];
  globalThis.fetch = async () => Response.json(payloads.shift());
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
    });
    for (const operation of [
      () => client.memory(),
      () => client.migrationQuarantine(),
      () => client.outputLocations(),
      () => client.outputPreference(),
      () => client.materializeArtifact({
        artifact_id: "artifact-expected",
        revision_id: "revision-expected",
      }),
      () => client.systemHealth(),
      () => client.systemHealth({ technical: true }),
      () => client.systemMetrics(),
    ]) {
      await assert.rejects(operation(), RuntimeContractError);
    }
    assert.equal(payloads.length, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("memory transport preserves the authoritative reset identity and derived count", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const reset = {
    reset_id: "memreset_1",
    status: "active" as const,
    affected_records: 2,
    affected_files: 1,
    created_at: bootstrap.server_time,
    undo_until: "2026-07-16T08:00:00Z",
    updated_at: bootstrap.server_time,
    can_undo: true,
  };
  const memory = {
    revision: 2,
    active_learned_records: 2,
    active_user_files: 1,
    factory_records: 1,
    tombstoned_records: 0,
    tombstoned_files: 0,
    resettable_count: 3,
    latest_reset: reset,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    return Response.json(request.method === "GET" ? memory : { memory, reset });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
      csrfToken: "csrf-memory",
    });
    assert.equal((await client.memory()).resettable_count, 3);
    assert.equal(
      (await client.resetLearnedMemory("reset-memory-stable")).reset.reset_id,
      reset.reset_id,
    );
    assert.equal(requests[1]!.headers.get("x-ecorex-csrf"), "csrf-memory");
    assert.deepEqual(await requests[1]!.clone().json(), {
      confirmed: true,
      client_request_id: "reset-memory-stable",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("connector-login HITL begin, check, and cancel never use ordinary respond", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Request[] = [];
  const mutation = {
    interaction: {
      interaction_id: "interaction / one",
      kind: "connector_login",
      status: "resolved",
      prompt: "连接飞书文档后继续",
      contract: {
        schema_version: 1,
        title: "连接飞书文档",
        fields: [],
        actions: [{
          action_id: "cancel",
          label: "取消",
          action_type: "cancel",
          style: "secondary",
          submits_form: false,
        }],
        connector: {
          connector_id: "feishu/docs",
          display_name: "飞书文档",
          state: "awaiting_callback",
          required_action_ids: [],
        },
      },
      options: [],
      response: { action_id: "cancel", values: {} },
      response_client_request_id: "connector_cancel_stable",
      thread_id: "thread-connector",
      turn_id: null,
      job_id: null,
      expires_at: null,
      created_at: bootstrap.server_time,
      updated_at: bootstrap.server_time,
    },
    turn: null,
    job: null,
    watermark: 8,
  };
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    if (request.url.endsWith("/begin")) {
      return Response.json({
        interaction_id: "interaction / one",
        connector_id: "feishu/docs",
        state: "awaiting_callback",
        authorization_url: "https://open.feishu.cn/authorize",
        verification_url: null,
        user_code: null,
        expires_at: "2026-07-12T12:00:00Z",
      });
    }
    if (request.url.endsWith("/check")) {
      return Response.json({
        interaction_id: "interaction / one",
        connector_id: "feishu/docs",
        connected: false,
        state: "awaiting_callback",
        reason: null,
        authority_refresh_revision_id: null,
        mutation: null,
      }, { status: 202 });
    }
    return Response.json({
      interaction_id: "interaction / one",
      connector_id: "feishu/docs",
      cancelled: true,
      mutation,
    });
  };
  try {
    const client = new RuntimeClient({
      apiBase: "http://127.0.0.1:8765/api/v1",
      bearerToken: "b".repeat(43),
    });
    client.acceptBootstrap(bootstrap);
    const started = await client.connectorLoginInteraction("interaction / one", "begin");
    const checked = await client.connectorLoginInteraction("interaction / one", "check");
    const cancelled = await client.connectorLoginInteraction("interaction / one", "cancel");

    assert.equal(started.state, "awaiting_callback");
    assert.equal(checked.connected, false);
    assert.equal(cancelled.cancelled, true);
    assert.deepEqual(
      requests.map((request) => new URL(request.url).pathname),
      [
        "/api/v1/interactions/interaction%20%2F%20one/connector-login/begin",
        "/api/v1/interactions/interaction%20%2F%20one/connector-login/check",
        "/api/v1/interactions/interaction%20%2F%20one/connector-login/cancel",
      ],
    );
    for (const request of requests) {
      assert.equal(request.method, "POST");
      assert.equal(request.headers.get("x-ecorex-csrf"), bootstrap.csrf_token);
      assert.deepEqual(await request.clone().json(), {});
      assert.equal(request.url.includes("/respond"), false);
      assert.equal(request.headers.has("x-ecorex-client-request-id"), false);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("connector-login and HITL mutation responses fail closed on state or identity drift", () => {
  const mutation = {
    interaction: {
      interaction_id: "interaction-contract",
      kind: "connector_login",
      status: "resolved",
      prompt: "继续办公任务",
      contract: {
        schema_version: 1,
        title: "连接飞书文档",
        fields: [],
        actions: [{
          action_id: "check_status",
          label: "检查状态",
          action_type: "connector_check_status",
          style: "primary",
          submits_form: false,
        }],
        connector: {
          connector_id: "feishu_docs",
          display_name: "飞书文档",
          state: "awaiting_callback",
          required_action_ids: [],
        },
      },
      options: [],
      response: { action_id: "check_status", values: {} },
      response_client_request_id: "connector_check_stable",
      thread_id: "thread-contract",
      turn_id: null,
      job_id: null,
      expires_at: null,
      created_at: bootstrap.server_time,
      updated_at: bootstrap.server_time,
    },
    turn: null,
    job: null,
    watermark: 9,
  };
  assert.equal(
    validateInteractionMutationResponse(mutation, "interaction-contract").watermark,
    9,
  );
  const connected = {
    interaction_id: "interaction-contract",
    connector_id: "feishu_docs",
    connected: true,
    state: "connected",
    reason: null,
    authority_refresh_revision_id: "revision-authority",
    mutation,
  };
  assert.equal(
    validateConnectorLoginCheckResponse(connected, "interaction-contract").connected,
    true,
  );
  assert.throws(
    () => validateConnectorLoginCheckResponse({
      interaction_id: "interaction-contract",
      connector_id: "feishu_docs",
      connected: false,
      state: "awaiting_callback",
    }),
    (error: unknown) => error instanceof RuntimeContractError
      && error.contract === "ConnectorLoginCheckResponse",
  );
  assert.throws(
    () => validateInteractionMutationResponse(mutation, "different-interaction"),
    (error: unknown) => error instanceof RuntimeContractError
      && error.contract === "InteractionMutationResponse"
      && error.path === "interaction.interaction_id",
  );
  assert.throws(
    () => validateConnectorLoginCheckResponse({
      ...connected,
      connector_id: "tencent_docs",
    }),
    (error: unknown) => error instanceof RuntimeContractError
      && error.contract === "ConnectorLoginCheckResponse"
      && error.path === "mutation.interaction.contract.connector.connector_id",
  );
});
