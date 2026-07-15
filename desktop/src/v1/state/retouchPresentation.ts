import type {
  ArtifactProjection,
  ItemProjection,
  RetouchInspectionRegion,
} from "../api/contracts.ts";
import { tryValidateArtifactProjection } from "../api/runtimeContract.ts";

export interface RetouchPresentation {
  artifact: ArtifactProjection;
  changeSummary: string;
  inspectionRegionCount: number;
  inspectionRegions: RetouchInspectionRegion[];
}

function normalizedNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function inspectionRegion(value: unknown): value is RetouchInspectionRegion {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  if (typeof candidate.summary !== "string" || !candidate.summary.trim()) return false;
  const geometry = candidate.normalized_geometry;
  if (!geometry || typeof geometry !== "object" || Array.isArray(geometry)) return false;
  const shape = geometry as Record<string, unknown>;
  if ("points" in shape) {
    return Array.isArray(shape.points)
      && shape.points.length >= 2
      && shape.points.every((point) => {
        if (!point || typeof point !== "object" || Array.isArray(point)) return false;
        const coordinate = point as Record<string, unknown>;
        return normalizedNumber(coordinate.x) && normalizedNumber(coordinate.y);
      })
      && (
        shape.width === undefined
        || (normalizedNumber(shape.width) && shape.width > 0)
      );
  }
  if (!normalizedNumber(shape.x) || !normalizedNumber(shape.y)) return false;
  if (shape.width === undefined && shape.height === undefined) return true;
  return normalizedNumber(shape.width)
    && normalizedNumber(shape.height)
    && shape.width > 0
    && shape.height > 0
    && shape.x + shape.width <= 1
    && shape.y + shape.height <= 1;
}

export function retouchPresentation(item: ItemProjection): RetouchPresentation | null {
  if (item.kind !== "artifact") return null;
  const content = item.content;
  if (typeof content.retouch_job_id !== "string") return null;
  const artifact = tryValidateArtifactProjection(content.artifact);
  if (artifact === null) return null;
  const qualitySummary = artifact.quality_evidence?.summary;
  const changeSummary = typeof content.change_summary === "string" && content.change_summary.trim()
    ? content.change_summary.trim()
    : typeof qualitySummary === "string" && qualitySummary.trim()
      ? qualitySummary.trim()
      : "已生成新的图片修订。";
  const inspectionRegions = Array.isArray(content.inspection_regions)
    ? content.inspection_regions.filter(inspectionRegion)
    : [];
  return {
    artifact,
    changeSummary,
    inspectionRegionCount: inspectionRegions.length,
    inspectionRegions,
  };
}
