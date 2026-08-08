import assert from "node:assert/strict";
import test from "node:test";
import contract from "../electron/notification-contract.cjs";

test("only a new authoritative completed Turn fact is accepted once", () => {
  const facts = new contract.CompletionFacts(Date.parse("2026-08-08T12:00:00Z"));
  const completed = {
    schema_version: 1,
    event_type: "turn.status_changed",
    thread_id: "thr_notify",
    turn_id: "turn_notify",
    created_at: "2026-08-08T12:00:01Z",
    payload: { from: "running", to: "completed" },
  };
  assert.deepEqual(facts.event(completed), {
    key: "thr_notify:turn_notify:completed",
    threadId: "thr_notify",
    turnId: "turn_notify",
  });
  assert.equal(facts.event(completed), null);
  assert.equal(facts.event({ ...completed, turn_id: "turn_failed", payload: { to: "failed" } }), null);
  assert.equal(facts.event({ ...completed, turn_id: "turn_old", created_at: "2026-08-08T11:59:59Z" }), null);
});

test("projection recovery shares the same task-terminal dedupe key", () => {
  const facts = new contract.CompletionFacts(Date.parse("2026-08-08T12:00:00Z"));
  const turn = {
    thread_id: "thr_recovered",
    turn_id: "turn_recovered",
    status: "completed",
    updated_at: "2026-08-08T12:00:01Z",
  };
  assert.ok(facts.turn("thr_recovered", turn));
  assert.equal(facts.turn("thr_recovered", turn), null);
  assert.equal(facts.turn("thr_other", turn), null);
  assert.equal(contract.validThreadId("not-a-thread"), false);
});
