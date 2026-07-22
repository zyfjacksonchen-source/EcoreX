import type { ArtifactProjection } from "../api/contracts.ts";

export function artifactRevisionIdentity(
  artifact: Pick<ArtifactProjection, "artifact_id" | "revision_id">,
): string {
  return `${artifact.artifact_id}:${artifact.revision_id}`;
}

/**
 * The event stream owns timeline position. The list endpoint is only a
 * recovery fallback for projections whose exact revision has no Artifact
 * Item, so refreshing metadata cannot duplicate a result at the transcript
 * tail.
 */
export function selectUnbackedArtifactProjections(
  itemArtifacts: readonly ArtifactProjection[],
  effectiveArtifacts: readonly ArtifactProjection[],
): ArtifactProjection[] {
  const backed = new Set(itemArtifacts.map(artifactRevisionIdentity));
  return effectiveArtifacts.filter((artifact) => !backed.has(artifactRevisionIdentity(artifact)));
}
