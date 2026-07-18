# EcoreX v1.0.1 usage calibration

## Authority

- Provider-reported completion usage is the only Token measurement fact.
- The local Composer derives today/week/context projections from immutable
  `model.response_completed` events.
- The operator usage panel derives cross-user projections from immutable
  legacy `usage_events` rows and v1 Gateway `response.completed` events.
- The user directory is the union of legacy `users` and v1
  `admin_ops_users`; a Gateway `account_id` is resolved to the canonical
  account email when available.
- `sync_events.detail`, rendered text, browser counters and task counts are not
  Token sources.
- Both surfaces use `Asia/Shanghai` calendar boundaries and normalize
  `total_tokens` to at least `input_tokens + output_tokens`.

## Identity and completeness

- User identity is the case-folded account email.
- The panel includes every non-deleted account, including accounts with zero
  activity in the selected range.
- Event identities not present in the account catalog remain visible instead
  of being dropped.
- A v1 account remains visible with zero activity. Gateway requests also
  become task-detail rows even when no legacy `sync_events` copy exists.
- Duplicate display names are disambiguated with the canonical email.
- Task and Token rows are grouped independently. A missing task event cannot
  hide a valid provider usage row.
- If both ledgers contain the same `request_id`, the immutable Gateway
  completion replaces the legacy copy instead of being counted twice.

## Production calibration

On 2026-07-19 the seven-day projection was checked against direct read-only
aggregates of the production legacy, Control Plane and Gateway databases:

- 41 complete user identities were returned by the API, including v1
  zero-activity identities.
- 287 user/day rows were returned for a seven-day range.
- 233 immutable usage records were projected.
- API input/output/total values exactly matched the merged ledger aggregate:
  117,373 input, 291,017 output and 408,390 total Tokens.

The usage-panel service and static projection were promoted atomically. The
service is enabled with restart-on-failure and the public route remains
protected by its existing Basic Authentication boundary.
