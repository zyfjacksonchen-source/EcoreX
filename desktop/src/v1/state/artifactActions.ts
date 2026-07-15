import type { ArtifactAction, ArtifactProjection } from "../api/contracts.ts";

export type ArtifactUiAction =
  | "thumbs_up"
  | "thumbs_down"
  | "precise_retouch"
  | "preview"
  | "open"
  | "reveal"
  | "download";

const OVERFLOW_ACTIONS = [
  "preview",
  "open",
  "reveal",
  "download",
] as const satisfies readonly ArtifactAction[];

/**
 * Maps the backend action projection to the actions this WebUI can execute.
 * Contextual actions are repeated in compact/touch overflow so hiding the
 * desktop action rail never removes a capability.
 */
export function artifactUiActions(
  actions: readonly ArtifactAction[],
  includeContextual: boolean,
): ArtifactUiAction[] {
  const projected = new Set(actions);
  const result: ArtifactUiAction[] = [];
  if (includeContextual && projected.has("feedback")) {
    result.push("thumbs_up", "thumbs_down");
  }
  if (includeContextual && projected.has("precise_retouch")) {
    result.push("precise_retouch");
  }
  for (const action of OVERFLOW_ACTIONS) {
    if (projected.has(action)) result.push(action);
  }
  return result;
}

/** Event Items make a result visible immediately; the Artifact endpoint then
 * supplies the current revision, feedback, actions, and quality projection. */
export function mergeArtifactProjections(
  itemArtifacts: readonly ArtifactProjection[],
  listedArtifacts: readonly ArtifactProjection[],
): ArtifactProjection[] {
  const merged = new Map(itemArtifacts.map((artifact) => [artifact.artifact_id, artifact]));
  for (const artifact of listedArtifacts) {
    const fromEvent = merged.get(artifact.artifact_id);
    if (!fromEvent || fromEvent.revision_id === artifact.revision_id) {
      merged.set(artifact.artifact_id, artifact);
      continue;
    }
    if (artifact.lineage?.supersedes_revision_id === fromEvent.revision_id) {
      merged.set(artifact.artifact_id, artifact);
      continue;
    }
    if (fromEvent.lineage?.supersedes_revision_id === artifact.revision_id) {
      continue;
    }
    const eventTime = Date.parse(fromEvent.created_at);
    const listTime = Date.parse(artifact.created_at);
    if (Number.isFinite(eventTime) && Number.isFinite(listTime) && listTime > eventTime) {
      merged.set(artifact.artifact_id, artifact);
    }
    // When opaque revisions cannot be ordered, retain the event projection.
    // Replacing it with an ambiguous list response could visibly roll a just-
    // completed retouch back to the base image during refresh.
  }
  return [...merged.values()];
}
