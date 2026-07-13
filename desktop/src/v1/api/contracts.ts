// Ergonomic compile-time projections for the thin WebUI. The authoritative
// Python schemas are pinned by tools/generate-runtime-contracts.py; critical
// wire values are validated against that generated manifest before use.

import type {
  GeneratedArtifactAction,
  GeneratedArtifactFamily,
  GeneratedArtifactRole,
  GeneratedArtifactStatus,
  GeneratedArtifactVisibility,
  GeneratedQualityStatus,
  GeneratedRenditionKind,
} from "./generatedRuntimeContract.ts";

export type JsonObject = Record<string, unknown>;

export type TurnStatus =
  | "accepted"
  | "queued"
  | "preparing"
  | "model_requested"
  | "streaming"
  | "tool_pending"
  | "waiting_human"
  | "tool_running"
  | "retry_wait"
  | "finalizing"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "superseded";

export type ItemStatus =
  | "created"
  | "in_progress"
  | "waiting_human"
  | "completed"
  | "failed"
  | "cancelled";

export type JobStatus =
  | "queued"
  | "leased"
  | "running"
  | "waiting_human"
  | "retry_scheduled"
  | "completed"
  | "failed"
  | "cancelled"
  | "dead_letter";

export type InteractionStatus = "pending" | "resolved" | "cancelled" | "expired";

export type InteractionFieldControl = "text" | "textarea" | "select" | "checkbox";
export type InteractionActionType =
  | "submit"
  | "continue"
  | "cancel"
  | "allow"
  | "deny"
  | "retry"
  | "skip"
  | "accept"
  | "request_changes"
  | "connector_begin_login"
  | "connector_check_status";

export interface InteractionChoice {
  option_id: string;
  label: string;
  description: string | null;
}

export interface InteractionFormField {
  field_id: string;
  label: string;
  control: InteractionFieldControl;
  required: boolean;
  description: string | null;
  placeholder: string | null;
  min_length: number;
  max_length: number;
  options: InteractionChoice[];
  sensitive: false;
}

export interface InteractionAction {
  action_id: string;
  label: string;
  action_type: InteractionActionType;
  style: "primary" | "secondary" | "danger";
  submits_form: boolean;
}

export interface InteractionContract {
  schema_version: 1;
  title: string;
  fields: InteractionFormField[];
  actions: InteractionAction[];
  connector: {
    connector_id: string;
    display_name: string;
    state:
      | "authorization_required"
      | "awaiting_callback"
      | "verifying"
      | "reauthorization_required";
    required_action_ids: string[];
  } | null;
}

export interface InteractionResponse {
  action_id: string;
  values: Record<string, string | boolean>;
}

export interface ThreadProjection {
  thread_id: string;
  status: "active" | "archived";
  title: string | null;
  metadata: JsonObject;
  forked_from_thread_id: string | null;
  forked_from_turn_id: string | null;
  forked_from_seq: number | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectProjection {
  project_id: string;
  name: string;
  project_path: string;
  pinned: boolean;
  thread_count: number;
  created_at: string;
  updated_at: string;
}

export interface ProjectListResponse {
  projects: ProjectProjection[];
}

export interface TurnProjection {
  turn_id: string;
  thread_id: string;
  status: TurnStatus;
  input: string;
  agent_model_id: string;
  image_model_id: string | null;
  client_message_id: string | null;
  metadata: JsonObject;
  terminal_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface ItemProjection {
  item_id: string;
  thread_id: string;
  turn_id: string;
  kind: "message" | "reasoning" | "tool_call" | "artifact" | "interaction" | "checkpoint";
  status: ItemStatus;
  content: JsonObject;
  created_at: string;
  updated_at: string;
}

export interface PublicArtifactRef extends JsonObject {
  artifact_id: string;
  revision_id: string | null;
}

export interface PublicToolActivity extends JsonObject {
  schema_version: 1;
  tool_call_id: string;
  tool_id: string;
  tool_name: string;
  display_label: string;
  phase: "requested" | "running" | "waiting_human" | "completed" | "failed" | "cancelled";
  status: ItemStatus;
  effects: ("read" | "write" | "network" | "execute" | "ui_automation" | "generate_media")[];
  risk: "low" | "medium" | "high";
  argument_summary: string;
  result_summary: string | null;
  argument_sha256: string;
  result_sha256: string | null;
  artifact_refs: PublicArtifactRef[];
}

export interface ReasoningItemContent extends JsonObject {
  channel: "reasoning_summary";
  atom_id: string;
  text: string;
  revision: number;
  presentation: "visible" | "collapsed" | "archived";
  archived_reason: string | null;
}

export interface JobProjection {
  job_id: string;
  kind: string;
  status: JobStatus;
  priority: number;
  attempt: number;
  max_attempts: number;
  thread_id: string | null;
  turn_id: string | null;
  available_at: string;
  deadline: string | null;
  reason_code: string | null;
  created_at: string;
  updated_at: string;
}

export interface InteractionProjection {
  interaction_id: string;
  kind:
    | "permission_approval"
    | "information"
    | "connector_login"
    | "conflict_resolution"
    | "artifact_review";
  status: InteractionStatus;
  prompt: string;
  contract: InteractionContract;
  options: JsonObject[];
  response: InteractionResponse | null;
  response_client_request_id: string | null;
  thread_id: string;
  turn_id: string | null;
  job_id: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface EventEnvelope {
  schema_version: 1;
  event_id: string;
  seq: number;
  thread_id: string;
  turn_id: string | null;
  item_id: string | null;
  job_id: string | null;
  tool_call_id: string | null;
  client_message_id: string | null;
  causation_id: string | null;
  correlation_id: string | null;
  trace_id: string | null;
  config_snapshot_id: string | null;
  capability_snapshot_id: string | null;
  permission_snapshot_id: string | null;
  extension_snapshot_id: string | null;
  event_type: string;
  created_at: string;
  payload: JsonObject;
}

export interface ThreadProjectionResponse {
  thread: ThreadProjection;
  turns: TurnProjection[];
  items: ItemProjection[];
  jobs: JobProjection[];
  interactions: InteractionProjection[];
  watermark: number;
}

export interface ReplayInteractionProjection extends InteractionProjection {}

export interface MockReplayResponse {
  projection: ThreadProjectionResponse;
  interactions: ReplayInteractionProjection[];
  live_replay_turn_ids: string[];
  source_watermark: number;
  through_seq: number;
  event_count: number;
  event_digest: string;
}

export interface LiveReplayResponse {
  source_thread_id: string;
  source_turn_id: string;
  causation_event_id: string;
  replay: TurnMutationResponse;
  permission_snapshot_id: string;
  extension_snapshot_id: string;
}

export interface ThreadListResponse {
  items: ThreadProjection[];
  next_cursor: string | null;
}

export interface TurnMutationResponse {
  turn: TurnProjection;
  job: JobProjection | null;
  watermark: number;
}

export interface InteractionMutationResponse {
  interaction: InteractionProjection;
  turn: TurnProjection | null;
  job: JobProjection | null;
  watermark: number;
}

export interface ConnectorLoginBeginResponse {
  interaction_id: string;
  connector_id: string;
  state: "awaiting_callback";
  authorization_url: string | null;
  verification_url: string | null;
  user_code: string | null;
  expires_at: string;
}

export type ConnectorLoginCheckResponse =
  | {
      interaction_id: string;
      connector_id: string;
      connected: false;
      state: "awaiting_callback";
    }
  | {
      interaction_id: string;
      connector_id: string;
      connected: false;
      state: "authorization_required" | "reauthorization_required";
      reason: string;
    }
  | {
      interaction_id: string;
      connector_id: string;
      connected: true;
      state: "connected";
      authority_refresh_revision_id: string | null;
      mutation: InteractionMutationResponse;
    };

export interface ConnectorLoginCancelResponse {
  interaction_id: string;
  connector_id: string;
  cancelled: true;
  mutation: InteractionMutationResponse;
}

export interface ModelPolicyDescriptor {
  schema_version: 1;
  policy_id: string;
  policy_version: string;
  local_model_id: string;
  upstream_model_id: string;
  reasoning_effort: "medium";
  context_management: {
    type: "compaction";
    compact_threshold_tokens: number;
  };
}

export interface ModelDescriptor {
  model_id: string;
  display_name: string;
  capabilities: string[];
  aliases: string[];
  is_default: boolean;
  model_policy: ModelPolicyDescriptor | null;
}

export interface ConnectorDescriptor {
  connector_id: string;
  display_name: string;
  tier: "stable" | "beta";
  health: "connected" | "disconnected" | "degraded" | "unconfigured";
  capabilities: string[];
  contract_version: string;
  description: string | null;
  auth_kinds: string[];
  icon_key: string | null;
  adapter_available: boolean;
  unavailable_reason: string | null;
}

export type ConnectorTier = "stable" | "beta";

export type ConnectorAuthKind =
  | "oauth2"
  | "device_code"
  | "app_credentials"
  | "api_token";

export type ConnectorHealth =
  | "unconfigured"
  | "authenticating"
  | "connected"
  | "degraded"
  | "error"
  | "disabled";

export type ConnectorEffect = "read" | "write" | "subscribe";

export interface ConnectorActionDescriptor {
  action_id: string;
  display_name: string;
  description: string;
  input_schema: JsonObject;
  output_schema: JsonObject;
  effects: ConnectorEffect[];
  required_scopes: string[];
  idempotent: boolean;
  requires_idempotency_key: boolean;
}

export interface ConnectorEventDescriptor {
  event_id: string;
  display_name: string;
  required_scopes: string[];
}

export interface ConnectorDefinitionProjection {
  connector_id: string;
  contract_version: "1.0";
  display_name: string;
  description: string;
  tier: ConnectorTier;
  auth_kinds: ConnectorAuthKind[];
  config_schema: JsonObject;
  actions: ConnectorActionDescriptor[];
  events: ConnectorEventDescriptor[];
  icon_key: string | null;
}

export interface ConnectorInstanceProjection {
  instance_id: string;
  connector_id: string;
  account_display_name: string;
  health: ConnectorHealth;
  granted_scopes: string[];
  available_actions: string[];
  last_error_code: string | null;
}

export interface ConnectorCatalogItem {
  definition: ConnectorDefinitionProjection;
  adapter_available: boolean;
  instances: ConnectorInstanceProjection[];
  unavailable_reason: string | null;
}

export interface ConnectorCatalogResponse {
  contract_version: "1.0";
  items: ConnectorCatalogItem[];
}

export type ExtensionKind =
  | "skill"
  | "mcp_server"
  | "tool_provider"
  | "connector_provider"
  | "capability_pack";

export type ExtensionStatus =
  | "staged"
  | "enabled"
  | "disabled"
  | "quarantined";

export type ExtensionHealth =
  | "unknown"
  | "healthy"
  | "degraded"
  | "unhealthy"
  | "circuit_open";

export type ExtensionTrust =
  | "builtin"
  | "administrator"
  | "verified_publisher"
  | "local_untrusted";

export type ExtensionActionId =
  | "enable"
  | "disable"
  | "health_check"
  | "rollback";

export interface ExtensionDependencyProjection {
  extension_id: string;
  version_range: string;
}

export interface ExtensionExportProjection {
  export_id: string;
  kind: "tool" | "skill" | "mcp_server" | "connector" | "capability_pack";
  exposure: "direct" | "deferred" | "hidden";
  permission_effects: string[];
}

export interface ExtensionActionProjection {
  action_id: ExtensionActionId;
  enabled: boolean;
  disabled_reason: string | null;
  requires_confirmation: boolean;
}

export interface ExtensionProjection {
  extension_id: string;
  display_name: string;
  description: string;
  kind: ExtensionKind;
  active_revision_id: string | null;
  active_version: string | null;
  active_digest: string | null;
  source:
    | "core_bundle"
    | "signed_release"
    | "capability_pack"
    | "administrator"
    | "local_bundle"
    | "legacy_import";
  trust: ExtensionTrust;
  status: ExtensionStatus;
  health: ExtensionHealth;
  dependencies: ExtensionDependencyProjection[];
  exports: ExtensionExportProjection[];
  actions: ExtensionActionProjection[];
  last_error_code: string | null;
  revision: number;
  updated_at: string;
}

export interface ExtensionCatalogSnapshot {
  snapshot_id: string;
  contract_version: "1.0";
  items: ExtensionProjection[];
}

export interface ExtensionMutationResponse {
  extension: ExtensionProjection;
  extensions: ExtensionCatalogSnapshot;
}

export interface ConnectorAuthChallenge {
  flow_id: string;
  connector_id: string;
  auth_kind: ConnectorAuthKind;
  expires_at: string;
  authorization_url: string | null;
  user_code: string | null;
  verification_url: string | null;
}

export interface UpdateSnapshot {
  current_version: string;
  state: "idle" | "available" | "downloading" | "awaiting_user" | "activating" | "failed";
  target_version: string | null;
  release_id: string | null;
  build_digest: string | null;
  transaction_id: string | null;
  can_activate: boolean;
  requires_refresh: boolean;
  error_code: string | null;
}

export interface MemoryResetProjection {
  reset_id: string;
  status: "active" | "undone" | "purged";
  affected_records: number;
  affected_files: number;
  created_at: string;
  undo_until: string;
  updated_at: string;
  can_undo: boolean;
}

export interface MemorySnapshot {
  revision: number;
  active_learned_records: number;
  active_user_files: number;
  factory_records: number;
  tombstoned_records: number;
  tombstoned_files: number;
  resettable_count: number;
  latest_reset: MemoryResetProjection | null;
}

export interface MemoryMutationResponse {
  memory: MemorySnapshot;
  reset: MemoryResetProjection;
}

export type MigrationCredentialKind =
  | "api_key"
  | "refresh_token"
  | "access_token"
  | "password"
  | "cryptographic_key"
  | "client_secret"
  | "credential";

export type MigrationCredentialOrigin =
  | "product_configuration"
  | "mcp_configuration"
  | "skill_configuration"
  | "permission_configuration";

export interface MigrationQuarantineItem {
  kind: MigrationCredentialKind;
  origin: MigrationCredentialOrigin;
  count: number;
}

export interface MigrationQuarantineProjection {
  status: "absent" | "available" | "deleted";
  entry_count: number;
  can_delete: boolean;
  deleted_at: string | null;
  items: MigrationQuarantineItem[];
}

export type OutputLocationAlias = "documents" | "downloads" | "workspace";

export interface OutputLocationOption {
  alias: OutputLocationAlias;
  available: boolean;
}

export interface OutputLocationCatalog {
  items: OutputLocationOption[];
}

export interface OutputPreference {
  account_id: string;
  location_alias: OutputLocationAlias;
  revision: number;
  output_policy_snapshot_id: string;
  updated_at: string;
}

export interface OutputMaterialization {
  materialization_id: string;
  artifact_id: string;
  revision_id: string;
  output_policy_snapshot_id: string;
  location_alias: OutputLocationAlias;
  display_name: string;
  sha256: string;
  size_bytes: number;
  status: "preparing" | "published" | "completed";
  reused_existing: boolean;
  created_at: string;
  completed_at: string | null;
}

export type SystemHealthStatus = "healthy" | "degraded" | "attention" | "critical";

export interface SystemHealthComponent {
  component_id: string;
  label: string;
  status: SystemHealthStatus;
  message: string;
}

export interface SystemHealthSample {
  sample_id: string;
  overall: SystemHealthStatus;
  summary: string;
  components: SystemHealthComponent[];
  sampled_at: string;
  metrics?: Record<string, unknown>;
}

export interface SystemMetricHistory {
  items: SystemHealthSample[];
}

export interface BootstrapResponse {
  api_version: "v1";
  event_schema_version: 1;
  storage_schema_version: 1;
  login: {
    authenticated: boolean;
    account_id: string | null;
    display_name: string | null;
    organization_id: string | null;
    roles: string[];
    session_revision: number | null;
  };
  policy_lease: {
    lease_id: string;
    issued_at: string;
    expires_at: string;
    duration_hours: number;
  } | null;
  models: {
    snapshot_id: string | null;
    chat: ModelDescriptor[];
    image: ModelDescriptor[];
    vision: ModelDescriptor[];
    audio: ModelDescriptor[];
    embedding: ModelDescriptor[];
  };
  model_service: {
    state: "ready" | "unavailable";
    reason: string | null;
  };
  login_service: {
    state: "ready" | "unavailable";
    reason: string | null;
  };
  share_service: {
    state: "ready" | "unavailable";
    reason: string | null;
  };
  retouch_service: {
    state: "ready" | "unavailable";
    reason: string | null;
  };
  quota: {
    remaining: number | null;
    unit: string;
    resets_at: string | null;
    limits: Record<string, number>;
  };
  permissions: {
    snapshot_id: string;
    profile: "default" | "full_access";
    revision: number;
    updated_at: string;
    sandbox: string;
    approval: string;
    full_access: boolean;
    admin_hard_denies: string[];
  };
  connectors: ConnectorDescriptor[];
  extensions: ExtensionCatalogSnapshot;
  update: UpdateSnapshot;
  csrf_token: string;
  server_time: string;
}

export type DeviceLoginStatus = "pending" | "authorized" | "denied" | "expired" | "failed";

export interface DeviceLoginProjection {
  flow_id: string;
  status: DeviceLoginStatus;
  user_code: string;
  verification_url: string;
  expires_at: string;
  poll_interval_seconds: number;
  next_poll_at: string;
  restart_required: boolean;
  restart_scheduled: boolean;
  session_generation: number | null;
  error_code: string | null;
}

export interface PermissionMutationResponse {
  permissions: BootstrapResponse["permissions"];
}

export interface UpdateMutationResponse {
  update: UpdateSnapshot;
}

export interface ActivateUpdateResponse extends UpdateMutationResponse {
  restart_scheduled: boolean;
  reload_after_ms: number;
}

export type ShareStatus =
  | "publishing"
  | "published"
  | "failed"
  | "revoking"
  | "revoked"
  | "expired";

export interface ShareSnapshotProjection {
  share_id: string;
  thread_id: string;
  source_watermark: number;
  status: ShareStatus;
  public_url: string | null;
  expires_at: string;
  created_at: string;
  updated_at: string;
  revoked_at: string | null;
  error_code: string | null;
}

export interface ShareListResponse {
  items: ShareSnapshotProjection[];
  count: number;
}

export type ArtifactFamily = GeneratedArtifactFamily;

export type ArtifactAction = GeneratedArtifactAction;

export interface ArtifactProjection {
  artifact_id: string;
  revision_id: string;
  family: ArtifactFamily;
  role: GeneratedArtifactRole;
  visibility: GeneratedArtifactVisibility;
  status: GeneratedArtifactStatus;
  display_name: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
  renditions: Array<{
    kind: GeneratedRenditionKind;
    mime_type: string;
    size_bytes: number;
    sha256: string;
  }>;
  actions: ArtifactAction[];
  feedback: {
    feedback_id: string;
    revision_id: string;
    signal: "thumbs_up" | "thumbs_down";
    recorded_at: string;
  } | null;
  lineage: {
    source_artifact_ids: string[];
    supersedes_revision_id: string | null;
  };
  quality_evidence: {
    status: GeneratedQualityStatus;
    checks: Array<{ name: string; status: GeneratedQualityStatus; detail: string | null }>;
    score: number | null;
    summary: string | null;
  };
}

export interface ArtifactListResponse {
  items: ArtifactProjection[];
  count: number;
}

export interface ArtifactExternalActionProjection {
  artifact_id: string;
  revision_id: string;
  action: "open" | "reveal";
  client_request_id: string;
  status: "completed";
  requested_at: string;
  updated_at: string;
  failure_code: null;
}

export interface RetouchPoint {
  x: number;
  y: number;
}

export type RetouchGeometry =
  | { kind: "rectangle"; normalized_geometry: { x: number; y: number; width: number; height: number } }
  | { kind: "ellipse"; normalized_geometry: { x: number; y: number; width: number; height: number } }
  | { kind: "point"; normalized_geometry: RetouchPoint }
  | { kind: "polygon"; normalized_geometry: { points: RetouchPoint[] } }
  | { kind: "polyline"; normalized_geometry: { points: RetouchPoint[] } }
  | { kind: "brush"; normalized_geometry: { points: RetouchPoint[]; width?: number } };

export type RetouchAnnotation = RetouchGeometry & {
  annotation_id?: string;
  instruction: string;
};

export type RetouchGeometryValue = RetouchGeometry["normalized_geometry"];

export interface RetouchInspectionRegion {
  normalized_geometry: RetouchGeometryValue;
  summary: string;
}

export interface RetouchJobProjection {
  job_id: string;
  artifact_id: string;
  base_revision_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  request: {
    base_revision_id: string;
    selected_artifact_ids: string[];
    annotations: RetouchAnnotation[];
    reference_artifact_ids: string[];
    global_instruction: string;
    client_request_id: string;
    pinned_reference_revision_ids?: Record<string, string>;
    edit_surface?: RetouchEditSurface;
    mask?: RetouchMaskProjection;
  };
  created_at: string;
  result_revision_id: string | null;
  change_summary: string | null;
  inspection_regions: RetouchInspectionRegion[];
  failure_reason: string | null;
}

export interface RetouchEditSurface {
  base_revision_id: string;
  raster_digest: string;
  width_px: number;
  height_px: number;
  orientation: number;
  color_space: string;
  mime_type: string;
  coordinate_space_version: "oriented-normalized-v1";
}

export interface RetouchMaskProjection {
  schema_version: 1;
  coordinate_space_version: "oriented-normalized-v1";
  width_px: number;
  height_px: number;
  sha256: string;
  size_bytes: number;
  covered_fraction: number;
  pixel_regions: Array<{ x: number; y: number; width: number; height: number }>;
}

export interface RetouchReferenceProjection {
  artifact_id: string;
  revision_id: string;
  display_name: string;
  mime_type: string;
  sha256: string;
  preview_url: string;
}

export interface RetouchViewState {
  zoom: number;
  pan_x: number;
  pan_y: number;
  selected_annotation_id: string | null;
  tool: RetouchAnnotation["kind"] | "select" | "pan";
}

export interface RetouchWorkspaceProjection {
  workspace_id: string;
  artifact_id: string;
  version: number;
  status: "editing" | "submitting" | "submitted";
  edit_surface: RetouchEditSurface;
  annotations: RetouchAnnotation[];
  references: RetouchReferenceProjection[];
  global_instruction: string;
  view_state: Partial<RetouchViewState>;
  mask: RetouchMaskProjection | null;
  submitted_job_id: string | null;
  job: RetouchJobProjection | null;
  result: ArtifactProjection | null;
  result_surface: RetouchEditSurface | null;
  surface_url: string;
  result_url: string | null;
  created_at: string;
  updated_at: string;
}
