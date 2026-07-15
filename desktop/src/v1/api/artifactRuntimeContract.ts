import type {
  ArtifactExternalActionProjection,
  ArtifactListResponse,
  ArtifactProjection,
  RetouchAnnotation,
  RetouchEditSurface,
  RetouchInspectionRegion,
  RetouchJobProjection,
  RetouchMaskProjection,
  RetouchWorkspaceProjection,
} from "./contracts.ts";
import { GENERATED_ARTIFACT_RUNTIME_CONTRACT } from "./generatedArtifactRuntimeContract.ts";
import { GENERATED_RUNTIME_CONTRACT } from "./generatedRuntimeContract.ts";
import {
  RuntimeContractError,
  validateArtifactProjection,
} from "./runtimeContract.ts";

export type ArtifactBoundaryKind =
  | "action"
  | "feedback"
  | "job"
  | "list"
  | "projection"
  | "workspace";

export interface ArtifactBoundaryContext {
  artifact_id?: string;
  revision_id?: string;
  workspace_id?: string;
  action?: "open" | "reveal";
  client_request_id?: string;
}

type RecordValue = Record<string, unknown>;

const WIRE = GENERATED_ARTIFACT_RUNTIME_CONTRACT.wireFields;
const VALUES = GENERATED_ARTIFACT_RUNTIME_CONTRACT.values;

function reject(contract: string, path: string, expectation: string): never {
  throw new RuntimeContractError(contract, path, expectation);
}

function record(value: unknown, contract: string, path: string): asserts value is RecordValue {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    reject(contract, path, "an object");
  }
}

function fields(
  value: RecordValue,
  expected: readonly string[],
  contract: string,
  path: string,
): void {
  const allowed = new Set(expected);
  for (const key of expected) {
    if (!Object.hasOwn(value, key)) reject(contract, `${path}.${key}`, "a declared field");
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) reject(contract, `${path}.${key}`, "no extra fields");
  }
}

function stringValue(
  value: unknown,
  contract: string,
  path: string,
  allowEmpty = false,
): asserts value is string {
  if (typeof value !== "string" || (!allowEmpty && !value.trim())) {
    reject(contract, path, allowEmpty ? "a string" : "a non-empty string");
  }
}

function nullableString(
  value: unknown,
  contract: string,
  path: string,
): asserts value is string | null {
  if (value !== null) stringValue(value, contract, path);
}

function integer(
  value: unknown,
  contract: string,
  path: string,
  minimum = 0,
): asserts value is number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
    reject(contract, path, `a safe integer >= ${minimum}`);
  }
}

function finite(
  value: unknown,
  contract: string,
  path: string,
): asserts value is number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    reject(contract, path, "a finite number");
  }
}

function oneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  contract: string,
  path: string,
): asserts value is T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) {
    reject(contract, path, allowed.join(" | "));
  }
}

function isoTimestamp(value: unknown, contract: string, path: string): asserts value is string {
  stringValue(value, contract, path);
  if (!Number.isFinite(Date.parse(value))) reject(contract, path, "an ISO timestamp");
}

function sha256(value: unknown, contract: string, path: string): asserts value is string {
  stringValue(value, contract, path);
  if (!/^[0-9a-f]{64}$/u.test(value)) reject(contract, path, "a lowercase SHA-256");
}

function stringArray(value: unknown, contract: string, path: string): asserts value is string[] {
  if (!Array.isArray(value)) reject(contract, path, "an array");
  value.forEach((item, index) => stringValue(item, contract, `${path}[${index}]`));
  if (new Set(value).size !== value.length) reject(contract, path, "unique values");
}

function same(
  actual: unknown,
  expected: string | undefined,
  contract: string,
  path: string,
): void {
  if (expected !== undefined && actual !== expected) reject(contract, path, `literal ${expected}`);
}

function normalizedNumber(value: unknown, contract: string, path: string): number {
  finite(value, contract, path);
  if (value < 0 || value > 1) reject(contract, path, "a number from 0 to 1");
  return value;
}

function point(value: unknown, contract: string, path: string): void {
  record(value, contract, path);
  fields(value, ["x", "y"], contract, path);
  normalizedNumber(value.x, contract, `${path}.x`);
  normalizedNumber(value.y, contract, `${path}.y`);
}

function geometry(
  kind: RetouchAnnotation["kind"],
  value: unknown,
  contract: string,
  path: string,
): void {
  record(value, contract, path);
  if (kind === "rectangle" || kind === "ellipse") {
    fields(value, ["x", "y", "width", "height"], contract, path);
    const x = normalizedNumber(value.x, contract, `${path}.x`);
    const y = normalizedNumber(value.y, contract, `${path}.y`);
    const width = normalizedNumber(value.width, contract, `${path}.width`);
    const height = normalizedNumber(value.height, contract, `${path}.height`);
    if (width <= 0 || height <= 0 || x + width > 1 || y + height > 1) {
      reject(contract, path, "positive geometry inside normalized bounds");
    }
    return;
  }
  if (kind === "point") {
    point(value, contract, path);
    return;
  }
  const allowed = kind === "brush" ? new Set(["points", "width"]) : new Set(["points"]);
  if (!Object.hasOwn(value, "points")) reject(contract, `${path}.points`, "a declared field");
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) reject(contract, `${path}.${key}`, "no extra fields");
  }
  if (!Array.isArray(value.points)) reject(contract, `${path}.points`, "an array");
  const minimum = kind === "polygon" ? 3 : 2;
  if (value.points.length < minimum || value.points.length > 512) {
    reject(contract, `${path}.points`, `${minimum} to 512 points`);
  }
  value.points.forEach((item, index) => point(item, contract, `${path}.points[${index}]`));
  if (kind === "brush" && value.width !== undefined) {
    const width = normalizedNumber(value.width, contract, `${path}.width`);
    if (width <= 0) reject(contract, `${path}.width`, "a positive normalized width");
  }
}

function annotation(value: unknown, contract: string, path: string): asserts value is RetouchAnnotation {
  record(value, contract, path);
  fields(value, WIRE.RetouchWorkspaceResponse.RetouchAnnotationResponse, contract, path);
  oneOf(value.kind, VALUES.annotationKinds, contract, `${path}.kind`);
  geometry(value.kind, value.normalized_geometry, contract, `${path}.normalized_geometry`);
  stringValue(value.instruction, contract, `${path}.instruction`);
  nullableString(value.annotation_id, contract, `${path}.annotation_id`);
}

function inspectionRegion(
  value: unknown,
  contract: string,
  path: string,
): asserts value is RetouchInspectionRegion {
  record(value, contract, path);
  fields(value, WIRE.RetouchJobResponse.RetouchInspectionRegionResponse, contract, path);
  record(value.normalized_geometry, contract, `${path}.normalized_geometry`);
  const keys = Object.keys(value.normalized_geometry).sort().join(",");
  const inferred = keys === "height,width,x,y"
    ? "rectangle"
    : keys === "x,y"
      ? "point"
      : keys === "points"
        ? "polyline"
        : keys === "points,width"
          ? "brush"
          : null;
  if (inferred === null) reject(contract, `${path}.normalized_geometry`, "a supported inspection geometry");
  geometry(inferred, value.normalized_geometry, contract, `${path}.normalized_geometry`);
  stringValue(value.summary, contract, `${path}.summary`);
}

function editSurface(
  value: unknown,
  contract: string,
  path: string,
): asserts value is RetouchEditSurface {
  record(value, contract, path);
  fields(value, WIRE.RetouchWorkspaceResponse.RetouchEditSurfaceResponse, contract, path);
  stringValue(value.base_revision_id, contract, `${path}.base_revision_id`);
  sha256(value.raster_digest, contract, `${path}.raster_digest`);
  integer(value.width_px, contract, `${path}.width_px`, 1);
  integer(value.height_px, contract, `${path}.height_px`, 1);
  integer(value.orientation, contract, `${path}.orientation`, 1);
  if (value.orientation > 8) reject(contract, `${path}.orientation`, "an integer from 1 to 8");
  stringValue(value.color_space, contract, `${path}.color_space`);
  stringValue(value.mime_type, contract, `${path}.mime_type`);
  if (value.coordinate_space_version !== VALUES.coordinateSpaceVersion) {
    reject(contract, `${path}.coordinate_space_version`, VALUES.coordinateSpaceVersion);
  }
}

function mask(
  value: unknown,
  contract: string,
  path: string,
): asserts value is RetouchMaskProjection {
  record(value, contract, path);
  fields(value, WIRE.RetouchWorkspaceResponse.RetouchMaskResponse, contract, path);
  if (value.schema_version !== 1) reject(contract, `${path}.schema_version`, "literal 1");
  if (value.coordinate_space_version !== VALUES.coordinateSpaceVersion) {
    reject(contract, `${path}.coordinate_space_version`, VALUES.coordinateSpaceVersion);
  }
  integer(value.width_px, contract, `${path}.width_px`, 1);
  integer(value.height_px, contract, `${path}.height_px`, 1);
  const maskWidth = value.width_px;
  const maskHeight = value.height_px;
  sha256(value.sha256, contract, `${path}.sha256`);
  integer(value.size_bytes, contract, `${path}.size_bytes`, 1);
  const covered = normalizedNumber(value.covered_fraction, contract, `${path}.covered_fraction`);
  if (covered > 1) reject(contract, `${path}.covered_fraction`, "a value from 0 to 1");
  if (!Array.isArray(value.pixel_regions) || value.pixel_regions.length > 100) {
    reject(contract, `${path}.pixel_regions`, "at most 100 regions");
  }
  value.pixel_regions.forEach((region, index) => {
    const itemPath = `${path}.pixel_regions[${index}]`;
    record(region, contract, itemPath);
    fields(region, WIRE.RetouchWorkspaceResponse.RetouchPixelRegionResponse, contract, itemPath);
    integer(region.x, contract, `${itemPath}.x`);
    integer(region.y, contract, `${itemPath}.y`);
    integer(region.width, contract, `${itemPath}.width`, 1);
    integer(region.height, contract, `${itemPath}.height`, 1);
    if (region.x + region.width > maskWidth || region.y + region.height > maskHeight) {
      reject(contract, itemPath, "a region inside mask bounds");
    }
  });
}

function feedback(value: unknown, contract: string, path: string): void {
  record(value, contract, path);
  fields(value, WIRE.FeedbackProjectionResponse.FeedbackProjectionResponse, contract, path);
  stringValue(value.feedback_id, contract, `${path}.feedback_id`);
  stringValue(value.revision_id, contract, `${path}.revision_id`);
  oneOf(value.signal, GENERATED_RUNTIME_CONTRACT.artifact.feedbackSignals, contract, `${path}.signal`);
  isoTimestamp(value.recorded_at, contract, `${path}.recorded_at`);
}

function retouchRequest(
  value: unknown,
  contract: string,
  path: string,
): asserts value is RetouchJobProjection["request"] {
  record(value, contract, path);
  fields(value, WIRE.RetouchJobResponse.RetouchRequestResponse, contract, path);
  stringValue(value.base_revision_id, contract, `${path}.base_revision_id`);
  stringArray(value.selected_artifact_ids, contract, `${path}.selected_artifact_ids`);
  if (value.selected_artifact_ids.length === 0 || value.selected_artifact_ids.length > 50) {
    reject(contract, `${path}.selected_artifact_ids`, "1 to 50 unique Artifact identities");
  }
  nullableString(value.agent_model_id, contract, `${path}.agent_model_id`);
  nullableString(value.image_model_id, contract, `${path}.image_model_id`);
  if (!Array.isArray(value.annotations) || value.annotations.length > 100) {
    reject(contract, `${path}.annotations`, "at most 100 annotations");
  }
  value.annotations.forEach((item, index) => annotation(item, contract, `${path}.annotations[${index}]`));
  stringArray(value.reference_artifact_ids, contract, `${path}.reference_artifact_ids`);
  if (value.reference_artifact_ids.length > 10) {
    reject(contract, `${path}.reference_artifact_ids`, "at most 10 references");
  }
  stringValue(value.global_instruction, contract, `${path}.global_instruction`, true);
  stringValue(value.client_request_id, contract, `${path}.client_request_id`);
  record(value.pinned_reference_revision_ids, contract, `${path}.pinned_reference_revision_ids`);
  for (const [artifactId, revisionId] of Object.entries(value.pinned_reference_revision_ids)) {
    stringValue(artifactId, contract, `${path}.pinned_reference_revision_ids key`);
    stringValue(revisionId, contract, `${path}.pinned_reference_revision_ids.${artifactId}`);
    if (!value.reference_artifact_ids.includes(artifactId)) {
      reject(contract, `${path}.pinned_reference_revision_ids.${artifactId}`, "a selected reference");
    }
  }
  if (value.edit_surface !== null) {
    editSurface(value.edit_surface, contract, `${path}.edit_surface`);
    same(value.edit_surface.base_revision_id, value.base_revision_id, contract, `${path}.edit_surface.base_revision_id`);
  }
  if (value.mask !== null) {
    if (value.edit_surface === null) reject(contract, `${path}.mask`, "an edit surface before a mask");
    mask(value.mask, contract, `${path}.mask`);
  }
  if (value.annotations.length === 0 && !value.global_instruction.trim()) {
    reject(contract, path, "at least one annotation or a global instruction");
  }
}

function retouchJob(
  value: unknown,
  contract = "RetouchJobResponse",
  path = "root",
): asserts value is RetouchJobProjection {
  record(value, contract, path);
  fields(value, WIRE.RetouchJobResponse.RetouchJobResponse, contract, path);
  stringValue(value.job_id, contract, `${path}.job_id`);
  stringValue(value.artifact_id, contract, `${path}.artifact_id`);
  stringValue(value.base_revision_id, contract, `${path}.base_revision_id`);
  retouchRequest(value.request, contract, `${path}.request`);
  oneOf(value.status, VALUES.retouchJobStatuses, contract, `${path}.status`);
  isoTimestamp(value.created_at, contract, `${path}.created_at`);
  nullableString(value.result_revision_id, contract, `${path}.result_revision_id`);
  nullableString(value.change_summary, contract, `${path}.change_summary`);
  if (!Array.isArray(value.inspection_regions) || value.inspection_regions.length > 100) {
    reject(contract, `${path}.inspection_regions`, "at most 100 inspection regions");
  }
  value.inspection_regions.forEach((item, index) => inspectionRegion(item, contract, `${path}.inspection_regions[${index}]`));
  nullableString(value.failure_reason, contract, `${path}.failure_reason`);
  same(value.request.base_revision_id, value.base_revision_id, contract, `${path}.request.base_revision_id`);
  if (!value.request.selected_artifact_ids.includes(value.artifact_id)) {
    reject(contract, `${path}.request.selected_artifact_ids`, "the Job target Artifact");
  }
  if (value.status === "completed") {
    if (value.result_revision_id === null || value.change_summary === null || value.failure_reason !== null) {
      reject(contract, path, "a completed result without failure");
    }
  } else if (value.status === "failed") {
    if (value.failure_reason === null || value.result_revision_id !== null) {
      reject(contract, path, "a failed result without a revision");
    }
  } else if (value.result_revision_id !== null) {
    reject(contract, `${path}.result_revision_id`, "null until completion");
  }
}

function action(
  value: unknown,
  context: ArtifactBoundaryContext,
): ArtifactExternalActionProjection {
  const contract = "ArtifactExternalActionResponse";
  record(value, contract, "root");
  fields(value, WIRE.ArtifactExternalActionResponse.ArtifactExternalActionResponse, contract, "root");
  stringValue(value.artifact_id, contract, "artifact_id");
  stringValue(value.revision_id, contract, "revision_id");
  oneOf(value.action, ["open", "reveal"] as const, contract, "action");
  stringValue(value.client_request_id, contract, "client_request_id");
  if (value.status !== "completed") reject(contract, "status", "literal completed");
  isoTimestamp(value.requested_at, contract, "requested_at");
  isoTimestamp(value.updated_at, contract, "updated_at");
  if (Date.parse(value.updated_at) < Date.parse(value.requested_at)) {
    reject(contract, "updated_at", "a timestamp after requested_at");
  }
  if (value.failure_code !== null) reject(contract, "failure_code", "null on success");
  same(value.artifact_id, context.artifact_id, contract, "artifact_id");
  same(value.revision_id, context.revision_id, contract, "revision_id");
  same(value.action, context.action, contract, "action");
  same(value.client_request_id, context.client_request_id, contract, "client_request_id");
  return value as unknown as ArtifactExternalActionProjection;
}

function workspace(value: unknown, context: ArtifactBoundaryContext): RetouchWorkspaceProjection {
  const contract = "RetouchWorkspaceResponse";
  record(value, contract, "root");
  fields(value, WIRE.RetouchWorkspaceResponse.RetouchWorkspaceResponse, contract, "root");
  stringValue(value.workspace_id, contract, "workspace_id");
  stringValue(value.artifact_id, contract, "artifact_id");
  integer(value.version, contract, "version", 1);
  oneOf(value.status, VALUES.retouchWorkspaceStatuses, contract, "status");
  editSurface(value.edit_surface, contract, "edit_surface");
  if (!Array.isArray(value.annotations) || value.annotations.length > 100) {
    reject(contract, "annotations", "at most 100 annotations");
  }
  value.annotations.forEach((item, index) => annotation(item, contract, `annotations[${index}]`));
  const annotationIds = value.annotations.flatMap((item) => item.annotation_id ? [item.annotation_id] : []);
  if (new Set(annotationIds).size !== annotationIds.length) reject(contract, "annotations", "unique annotation IDs");
  if (!Array.isArray(value.references) || value.references.length > 10) {
    reject(contract, "references", "at most 10 references");
  }
  const referenceIds = new Set<string>();
  value.references.forEach((item, index) => {
    const path = `references[${index}]`;
    record(item, contract, path);
    fields(item, WIRE.RetouchWorkspaceResponse.RetouchReferenceResponse, contract, path);
    stringValue(item.artifact_id, contract, `${path}.artifact_id`);
    stringValue(item.revision_id, contract, `${path}.revision_id`);
    stringValue(item.display_name, contract, `${path}.display_name`);
    stringValue(item.mime_type, contract, `${path}.mime_type`);
    sha256(item.sha256, contract, `${path}.sha256`);
    stringValue(item.preview_url, contract, `${path}.preview_url`);
    const expected = `/api/v1/retouch-workspaces/${value.workspace_id}/references/${item.artifact_id}/preview`;
    same(item.preview_url, expected, contract, `${path}.preview_url`);
    if (referenceIds.has(item.artifact_id)) reject(contract, path, "a unique reference Artifact");
    referenceIds.add(item.artifact_id);
  });
  stringValue(value.global_instruction, contract, "global_instruction", true);
  record(value.view_state, contract, "view_state");
  fields(value.view_state, WIRE.RetouchWorkspaceResponse.RetouchViewStateResponse, contract, "view_state");
  finite(value.view_state.zoom, contract, "view_state.zoom");
  finite(value.view_state.pan_x, contract, "view_state.pan_x");
  finite(value.view_state.pan_y, contract, "view_state.pan_y");
  nullableString(value.view_state.selected_annotation_id, contract, "view_state.selected_annotation_id");
  oneOf(value.view_state.tool, VALUES.retouchViewTools, contract, "view_state.tool");
  if (
    value.view_state.selected_annotation_id !== null
    && !annotationIds.includes(value.view_state.selected_annotation_id)
  ) {
    reject(contract, "view_state.selected_annotation_id", "an annotation in this workspace");
  }
  if (value.mask !== null) mask(value.mask, contract, "mask");
  nullableString(value.submitted_job_id, contract, "submitted_job_id");
  if (value.job !== null) retouchJob(value.job, contract, "job");
  const result = value.result === null ? null : validateArtifactProjection(value.result);
  if (value.result_surface !== null) editSurface(value.result_surface, contract, "result_surface");
  stringValue(value.surface_url, contract, "surface_url");
  nullableString(value.result_url, contract, "result_url");
  isoTimestamp(value.created_at, contract, "created_at");
  isoTimestamp(value.updated_at, contract, "updated_at");
  if (Date.parse(value.updated_at) < Date.parse(value.created_at)) reject(contract, "updated_at", "after created_at");
  same(value.surface_url, `/api/v1/retouch-workspaces/${value.workspace_id}/surface`, contract, "surface_url");
  same(value.workspace_id, context.workspace_id, contract, "workspace_id");
  same(value.artifact_id, context.artifact_id, contract, "artifact_id");
  same(value.edit_surface.base_revision_id, context.revision_id, contract, "edit_surface.base_revision_id");
  if (value.status === "submitted") {
    if (value.submitted_job_id === null) reject(contract, "submitted_job_id", "a submitted Job identity");
  } else if (value.job !== null || value.result !== null) {
    reject(contract, "status", "submitted before execution state is exposed");
  }
  if (value.job !== null) {
    same(value.job.job_id, value.submitted_job_id ?? undefined, contract, "job.job_id");
    same(value.job.artifact_id, value.artifact_id, contract, "job.artifact_id");
    same(value.job.base_revision_id, value.edit_surface.base_revision_id, contract, "job.base_revision_id");
  }
  if (result === null) {
    if (value.result_surface !== null || value.result_url !== null) reject(contract, "result", "null result metadata");
  } else {
    if (value.job === null || value.job.status !== "completed") reject(contract, "result", "a completed Job");
    same(result.artifact_id, value.artifact_id, contract, "result.artifact_id");
    same(result.revision_id, value.job.result_revision_id ?? undefined, contract, "result.revision_id");
    same(value.result_url, `/api/v1/retouch-workspaces/${value.workspace_id}/result`, contract, "result_url");
    if (value.result_surface !== null) {
      same(value.result_surface.base_revision_id, result.revision_id, contract, "result_surface.base_revision_id");
    }
  }
  return value as unknown as RetouchWorkspaceProjection;
}

export function validateArtifactBoundary(
  value: unknown,
  kind: ArtifactBoundaryKind,
  context: ArtifactBoundaryContext = {},
): unknown {
  if (kind === "projection") {
    const projection = validateArtifactProjection(value);
    same(projection.artifact_id, context.artifact_id, "ArtifactProjectionResponse", "artifact_id");
    same(projection.revision_id, context.revision_id, "ArtifactProjectionResponse", "revision_id");
    return projection;
  }
  if (kind === "list") {
    const contract = "ArtifactListResponse";
    record(value, contract, "root");
    fields(value, WIRE.ArtifactListResponse.ArtifactListResponse, contract, "root");
    if (!Array.isArray(value.items)) reject(contract, "items", "an array");
    const items = value.items.map((item, index) => {
      try {
        return validateArtifactProjection(item);
      } catch (error) {
        if (error instanceof RuntimeContractError) {
          const suffix = error.path.replace(/^root\.?/u, "");
          throw new RuntimeContractError(
            error.contract,
            suffix ? `items[${index}].${suffix}` : `items[${index}]`,
            error.expectation,
          );
        }
        throw error;
      }
    });
    integer(value.count, contract, "count");
    if (value.count !== items.length) reject(contract, "count", "the exact items length");
    const identities = items.map((item) => `${item.artifact_id}\u0000${item.revision_id}`);
    if (new Set(identities).size !== identities.length) reject(contract, "items", "unique Artifact revisions");
    return { items, count: value.count } satisfies ArtifactListResponse;
  }
  if (kind === "feedback") {
    feedback(value, "FeedbackProjectionResponse", "root");
    const projection = value as NonNullable<ArtifactProjection["feedback"]>;
    same(projection.revision_id, context.revision_id, "FeedbackProjectionResponse", "revision_id");
    return projection;
  }
  if (kind === "action") return action(value, context);
  if (kind === "job") {
    retouchJob(value);
    same(value.artifact_id, context.artifact_id, "RetouchJobResponse", "artifact_id");
    same(value.base_revision_id, context.revision_id, "RetouchJobResponse", "base_revision_id");
    return value;
  }
  return workspace(value, context);
}
