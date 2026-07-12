import assert from "node:assert/strict";
import test from "node:test";

import {
  earlierTimelineAnchor,
  newerTimelineAnchor,
  selectTimelineWindow,
  TIMELINE_WINDOW_SIZE,
} from "./timelineWindow.ts";

function items(count: number) {
  return Array.from({ length: count }, (_, index) => ({ item_id: `item-${index + 1}` }));
}

test("the latest timeline render is bounded and keeps the newest messages", () => {
  const source = items(TIMELINE_WINDOW_SIZE + 37);
  const selected = selectTimelineWindow(source, null);

  assert.equal(selected.items.length, TIMELINE_WINDOW_SIZE);
  assert.equal(selected.items[0]?.item_id, "item-38");
  assert.equal(selected.items.at(-1)?.item_id, `item-${source.length}`);
  assert.equal(selected.hiddenBefore, 37);
  assert.equal(selected.hiddenAfter, 0);
  assert.equal(selected.atLatest, true);
});

test("history pages remain anchored when new streaming messages arrive", () => {
  const source = items(300);
  const latest = selectTimelineWindow(source, null);
  const anchor = earlierTimelineAnchor(source, latest);
  assert.equal(anchor, "item-180");

  const historical = selectTimelineWindow(source, anchor);
  assert.equal(historical.items[0]?.item_id, "item-61");
  assert.equal(historical.items.at(-1)?.item_id, "item-180");

  const afterStreaming = selectTimelineWindow(items(325), anchor);
  assert.deepEqual(afterStreaming.items, historical.items);
  assert.equal(afterStreaming.hiddenAfter, 145);
  assert.equal(newerTimelineAnchor(items(325), afterStreaming), "item-300");
});

test("a stale history anchor fails safe to the latest bounded page", () => {
  const selected = selectTimelineWindow(items(150), "removed-item");

  assert.equal(selected.anchorMissing, true);
  assert.equal(selected.atLatest, true);
  assert.equal(selected.items.length, TIMELINE_WINDOW_SIZE);
  assert.equal(selected.items.at(-1)?.item_id, "item-150");
});

test("invalid window sizes are rejected", () => {
  assert.throws(() => selectTimelineWindow(items(1), null, 0), TypeError);
});
