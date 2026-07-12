import type {
  RetouchAnnotation,
  RetouchPoint,
  RetouchViewState,
} from "../api/contracts.ts";

export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function clamp(value: number, minimum = 0, maximum = 1): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export function boxBetween(start: RetouchPoint, end: RetouchPoint): Bounds {
  return {
    x: Math.min(start.x, end.x),
    y: Math.min(start.y, end.y),
    width: Math.abs(end.x - start.x),
    height: Math.abs(end.y - start.y),
  };
}

export function annotationBounds(annotation: RetouchAnnotation): Bounds {
  if (annotation.kind === "rectangle" || annotation.kind === "ellipse") {
    return annotation.normalized_geometry;
  }
  if (annotation.kind === "point") {
    return {
      x: clamp(annotation.normalized_geometry.x - 0.015),
      y: clamp(annotation.normalized_geometry.y - 0.015),
      width: 0.03,
      height: 0.03,
    };
  }
  const points = annotation.normalized_geometry.points;
  const x = Math.min(...points.map((point) => point.x));
  const y = Math.min(...points.map((point) => point.y));
  const right = Math.max(...points.map((point) => point.x));
  const bottom = Math.max(...points.map((point) => point.y));
  return { x, y, width: right - x, height: bottom - y };
}

export function annotationAt(
  annotations: RetouchAnnotation[],
  point: RetouchPoint,
  tolerance = 0.018,
): RetouchAnnotation | null {
  for (const annotation of [...annotations].reverse()) {
    const bounds = annotationBounds(annotation);
    if (
      point.x >= bounds.x - tolerance
      && point.x <= bounds.x + bounds.width + tolerance
      && point.y >= bounds.y - tolerance
      && point.y <= bounds.y + bounds.height + tolerance
    ) return annotation;
  }
  return null;
}

export function translateAnnotation(
  annotation: RetouchAnnotation,
  delta: RetouchPoint,
): RetouchAnnotation {
  const bounds = annotationBounds(annotation);
  const dx = clamp(delta.x, -bounds.x, 1 - bounds.x - bounds.width);
  const dy = clamp(delta.y, -bounds.y, 1 - bounds.y - bounds.height);
  if (annotation.kind === "rectangle" || annotation.kind === "ellipse") {
    return {
      ...annotation,
      normalized_geometry: {
        ...annotation.normalized_geometry,
        x: annotation.normalized_geometry.x + dx,
        y: annotation.normalized_geometry.y + dy,
      },
    };
  }
  if (annotation.kind === "point") {
    return {
      ...annotation,
      normalized_geometry: {
        x: clamp(annotation.normalized_geometry.x + dx),
        y: clamp(annotation.normalized_geometry.y + dy),
      },
    };
  }
  return {
    ...annotation,
    normalized_geometry: {
      ...annotation.normalized_geometry,
      points: annotation.normalized_geometry.points.map((point) => ({
        x: clamp(point.x + dx),
        y: clamp(point.y + dy),
      })),
    },
  } as RetouchAnnotation;
}

export function normalizedViewBox(view: Pick<RetouchViewState, "zoom" | "pan_x" | "pan_y">): string {
  const zoom = clamp(view.zoom, 1, 8);
  const size = 1 / zoom;
  const x = clamp(view.pan_x - size / 2, 0, 1 - size);
  const y = clamp(view.pan_y - size / 2, 0, 1 - size);
  return `${x} ${y} ${size} ${size}`;
}

export function boundedHistory<T>(items: T[], value: T, maximum = 50): T[] {
  return [...items, value].slice(-maximum);
}
