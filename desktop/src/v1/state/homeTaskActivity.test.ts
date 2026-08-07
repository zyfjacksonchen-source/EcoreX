import assert from "node:assert/strict";
import test from "node:test";

import { homeTaskActivity } from "./homeTaskActivity.ts";

test("home task activity renders authoritative Runtime counts", () => {
  const view = homeTaskActivity({
    completed_today: 2,
    partial_today: 1,
    waiting: 3,
    terminal_today: 4,
    days: [
      { date: "2026-07-30", completed: 1, partial: 0, terminal: 2 },
      { date: "2026-07-31", completed: 0, partial: 0, terminal: 0 },
      { date: "2026-08-01", completed: 0, partial: 0, terminal: 0 },
      { date: "2026-08-02", completed: 0, partial: 0, terminal: 0 },
      { date: "2026-08-03", completed: 0, partial: 0, terminal: 0 },
      { date: "2026-08-04", completed: 1, partial: 0, terminal: 1 },
      { date: "2026-08-05", completed: 2, partial: 1, terminal: 4 },
    ],
  });

  assert.equal(view.completed, 2);
  assert.equal(view.partial, 1);
  assert.equal(view.waiting, 3);
  assert.equal(view.successRate, "50%");
  assert.deepEqual(view.trend.map((day) => day.label), [
    "7/30", "7/31", "8/1", "8/2", "8/3", "8/4", "8/5",
  ]);
});
