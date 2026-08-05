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
import type {
  GeneratedMemoryResetStatus,
  GeneratedMigrationCredentialKind,
  GeneratedMigrationCredentialOrigin,
  GeneratedMigrationQuarantineStatus,
  GeneratedOutputLocationAlias,
  GeneratedOutputMaterializationStatus,
  GeneratedSystemHealthStatus,
} from "./generatedSettingsRuntimeContract.ts";
import type {
  GeneratedInteractionKind,
  GeneratedInteractionStatus,
  GeneratedItemKind,
  GeneratedItemStatus,
  GeneratedJobStatus,
  GeneratedThreadStatus,
  GeneratedTurnStatus,
} from "./generatedRuntimeProjectionContract.ts";

export type JsonObject = Record<string, unknown>;

export type ThreadStatus = GeneratedThreadStatus;
export type TurnStatus = GeneratedTurnStatus;
export type ItemKind = GeneratedItemKind;
export type ItemStatus = GeneratedItemStatus;
export type JobStatus = GeneratedJobStatus;
export type InteractionKind = GeneratedInteractionKind;
export type InteractionStatus = GeneratedInteractionStatus;

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
  status: ThreadStatus;
  title: string | null;
  pinned: boolean;
  active_turn_status: TurnStatus | null;
  last_turn_status: TurnStatus | null;
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

export interface InputAttachmentProjection {
  attachment_id: string;
  revision_id: string;
  display_name: string;
  mime_type: string;
  size_bytes: number;
  media_kind: "image" | "document" | "file";
  sha256: string;
  thumbnail_url: string | null;
  created_at: string;
}

export interface TokenUsageWindow {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface ContextUsageProjection {
  used_tokens: number | null;
  window_tokens: number | null;
  model_id: string | null;
  model_display_name: string | null;
  model_catalog_snapshot_id: string | null;
  measured_at: string | null;
}

export interface TaskActivityDay {
  date: string;
  completed: number;
  terminal: number;
}

export interface TaskActivityProjection {
  completed_today: number;
  waiting: number;
  terminal_today: number;
  days: TaskActivityDay[];
}

export interface ConversationUsageProjection {
  thread_id: string;
  timezone: string;
  scope: "account" | "local_device";
  source: "managed_gateway" | "local_event_store";
  complete_across_devices: boolean;
  today: TokenUsageWindow;
  week: TokenUsageWindow;
  context: ContextUsageProjection;
  task_activity: TaskActivityProjection;
  calculated_at: string;
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
  inherited: boolean;
  created_at: string;
  updated_at: string;
}

export interface ItemProjection {
  item_id: string;
  thread_id: string;
  turn_id: string;
  kind: ItemKind;
  status: ItemStatus;
  content: JsonObject;
  inherited: boolean;
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
  kind: InteractionKind;
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

export interface ReplaceTurnResponse {
  superseded_turn: TurnProjection;
  replacement_turn: TurnProjection;
  job: JobProjection;
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
      reason: null;
      authority_refresh_revision_id: null;
      mutation: null;
    }
  | {
      interaction_id: string;
      connector_id: string;
      connected: false;
      state: "authorization_required" | "reauthorization_required";
      reason: string;
      authority_refresh_revision_id: null;
      mutation: null;
    }
  | {
      interaction_id: string;
      connector_id: string;
      connected: true;
      state: "connected";
      reason: null;
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
  reasoning_effort: "medium" | "high" | "max";
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
  | "quarantined"
  | "uninstalled";

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
  | "rollback"
  | "configure"
  | "uninstall";

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
  category:
    | "system"
    | "office"
    | "image_media"
    | "collaboration"
    | "data"
    | "development"
    | "automation"
    | "general";
  icon_key: string;
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
  provenance: {
    brand: "e-Mate";
    original_platform: string | null;
    original_url: string | null;
  };
  readiness: "ready" | "needs_configuration" | "missing_runtime" | "unsupported";
  requirements: string[];
  tags: string[];
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
  extension_generation: number;
  items: ExtensionProjection[];
}

export interface ExtensionMutationResponse {
  extension: ExtensionProjection;
  extensions: ExtensionCatalogSnapshot;
}

export interface MCPOAuthStatusProjection {
  service_id: string;
  state: "authorization_required" | "authorizing" | "authorized" | "reauthorization_required";
  expires_at: number | null;
  scope: string;
}

export interface MCPOAuthStatusResponse {
  items: MCPOAuthStatusProjection[];
}

export interface MCPOAuthChallengeProjection {
  service_id: string;
  state: "authorizing";
  authorization_url: string;
  expires_at: number;
}

export interface CapabilityMentionProjection {
  reference: string;
  label: string;
  description: string;
  kind: "system" | "skill" | "collaboration";
}

export interface CapabilityMentionCatalog {
  schema_version: 1;
  snapshot_id: string;
  items: CapabilityMentionProjection[];
}

export interface SkillHubCardProjection {
  slug: string;
  title: string;
  summary: string;
  version: string;
  package_sha256: string;
  package_size_bytes: number;
  tags: string[];
  category: "third_party" | "content_creation" | "office_productivity";
  uploader: { nickname: string; author_ref: string };
  provenance: { brand: "e-Mate"; original_platform: string | null; original_url: string | null };
  installation_status: "not_installed" | "installed_enabled" | "installed_disabled" | "uninstalled";
  readiness: "ready" | "needs_configuration" | "missing_runtime" | "unsupported";
}

export interface SkillHubListResponse {
  schema_version: 1;
  items: SkillHubCardProjection[];
  next_cursor: string | null;
}

export interface SkillHubDetailProjection {
  schema_version: 1;
  skill: SkillHubCardProjection;
  versions: SkillHubCardProjection[];
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
  status: GeneratedMemoryResetStatus;
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

export type MigrationCredentialKind = GeneratedMigrationCredentialKind;

export type MigrationCredentialOrigin = GeneratedMigrationCredentialOrigin;

export interface MigrationQuarantineItem {
  kind: MigrationCredentialKind;
  origin: MigrationCredentialOrigin;
  count: number;
}

export interface MigrationQuarantineProjection {
  status: GeneratedMigrationQuarantineStatus;
  entry_count: number;
  can_delete: boolean;
  deleted_at: string | null;
  items: MigrationQuarantineItem[];
}

export type OutputLocationAlias = GeneratedOutputLocationAlias;

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
  status: GeneratedOutputMaterializationStatus;
  reused_existing: boolean;
  created_at: string;
  completed_at: string | null;
}

export type SystemHealthStatus = GeneratedSystemHealthStatus;

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
    session_lease_digest: string | null;
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

export interface LoginSessionResponse {
  authenticated: true;
  display_name: string;
  generation: number;
  restart_required: true;
  restart_scheduled: boolean;
}

export interface LogoutSessionResponse {
  authenticated: false;
  generation: number;
  restart_required: true;
  restart_scheduled: boolean;
}

export interface PasswordSessionChangeResponse {
  schema_version: 1;
  status: "changed";
  reauthentication_required: true;
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
  annotation_id?: string | null;
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
    agent_model_id: string | null;
    image_model_id: string | null;
    annotations: RetouchAnnotation[];
    reference_artifact_ids: string[];
    global_instruction: string;
    client_request_id: string;
    pinned_reference_revision_ids: Record<string, string>;
    edit_surface: RetouchEditSurface | null;
    mask: RetouchMaskProjection | null;
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
  view_state: RetouchViewState;
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
