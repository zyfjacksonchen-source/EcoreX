import assert from "node:assert/strict";
import test from "node:test";

import type { RetouchAnnotation } from "../api/contracts.ts";
import {
  annotationAt,
  boundedHistory,
  normalizedViewBox,
  translateAnnotation,
} from "./retouchCanvas.ts";

const rectangle: RetouchAnnotation = {
  annotation_id: "ann_one",
  kind: "rectangle",
  normalized_geometry: { x: 0.8, y: 0.8, width: 0.2, height: 0.2 },
  instruction: "remove",
};

test("translation remains inside the normalized immutable edit surface", () => {
  const moved = translateAnnotation(rectangle, { x: 0.4, y: -1 });
  assert.deepEqual(moved.normalized_geometry, { x: 0.8, y: 0, width: 0.2, height: 0.2 });
});

test("hit testing prefers the topmost annotation", () => {
  const top: RetouchAnnotation = {
    ...rectangle,
    annotation_id: "ann_top",
    instruction: "top",
  };
  assert.equal(annotationAt([rectangle, top], { x: 0.9, y: 0.9 })?.annotation_id, "ann_top");
});

test("zoom viewbox is bounded and history remains finite", () => {
  assert.equal(normalizedViewBox({ zoom: 2, pan_x: 1, pan_y: 0 }), "0.5 0 0.5 0.5");
  assert.deepEqual(boundedHistory([1, 2, 3], 4, 2), [3, 4]);
});
