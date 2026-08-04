# Production Usage/Audit reconciliation gate

Status: **passed** on 2026-08-04. The authenticated production Usage service now runs the v0.3.0 projection and the same-filter Usage/Audit comparison is recorded in `artifacts/usage-audit-production-reconciliation.json`.

## 2026-08-04 production repair and accepted evidence

- A strict known-host SSH connection reached the previously recorded production identity without exposing the password, host, token or raw user rows.
- The first real comparison reproduced the defect: the deployed July service returned the old Audit shape, so Usage had KPIs while Audit did not.
- `ecorex/control_plane/usage_panel_service.py` was installed by atomic replacement at the existing loopback service path. The deployer preserved the exact previous bytes, restarted the service and would restore/restart them on any failed health or reconciliation check.
- The final production projection is `v0.3.0-usage-1`. Usage and Audit KPIs and reconciliation objects are exactly equal for `[2026-08-01, 2026-08-05)` in `Asia/Shanghai`.
- Accepted reconciliation: `canonical_record_count=186`, `replaced_duplicate_count=0`, `unassociated_record_count=186`, `missing_provider_usage_count=0`. The high unassociated count remains visible; it was not hidden or backfilled.
- Deployment receipt: `artifacts/usage-panel-production-deploy-v030.json`.
- Readback-bound reconciliation: `artifacts/usage-audit-production-reconciliation.json`.

## Historical first read-only attempt (superseded)

The configured operator credential file exists and resolves to the same redacted production identity already recorded by `production-deploy-online.json` (`domainHash=A753D877497CBE35`, `sshHostHash=CDF1CF905198CA97`). No secret value or raw response was persisted.

Before the approved password-in-memory SSH repair, the gate was open for these reasons:

- `ECOREX_CONTROL_PLANE_URL`, `ECOREX_CONTROL_PLANE_HOSTS`, `ECOREX_CONTROL_PLANE_TOKEN`, `ECOREX_GATEWAY_URL`, provider bearer variables and OpenAI bearer variables are absent from the current process.
- Read-only public probes to `/api/v1/admin/usage/summary`, `/api/v1/admin/models` and `/api/v1/models` reached the production identity and returned `401`, proving the services require an unavailable bearer rather than being treated as successful connectivity.
- The proposed public `/ecorex-agent/usage/api/{health,data,runtime-audit}` paths all returned `404`; the deployed reverse-proxy contract does not expose the loopback Usage service at that prefix.
- Non-interactive system SSH with strict known-host validation returned exit `255`. The operator file contains password-based access, but the current environment has neither an SSH agent/key nor a password-capable SSH library/helper. The password was not placed on a command line or printed.
- A Luna invocation was not attempted: the only public read-only catalog endpoint returned `401`, while `/v1/responses` is a metered POST that would write production usage facts and is outside this no-write acceptance slice.

The Usage/Audit authority gap described above is now closed by the approved password-in-memory SSH path and accepted artifact. The separate live Luna gate is tracked in `completion-audit.md`; it is not part of this passed Usage/Audit gate.

The accepted comparison is reproducible with the following shape against the authenticated loopback service. Use the same inclusive start and exclusive end dates for both reads and do not place bearer tokens in shell history.

```powershell
$usageBase = "http://127.0.0.1:<approved-forwarded-usage-port>"
$start = "2026-08-01"
$end = "2026-08-05"
$data = Invoke-RestMethod "$usageBase/api/data?start=$start&end=$end"
$audit = Invoke-RestMethod "$usageBase/api/runtime-audit?start=$start&end=$end&limit=12000"
if ($data.projection_version -ne $audit.projection_version) { throw "projection mismatch" }
if (($data.kpis | ConvertTo-Json -Compress) -ne ($audit.kpis | ConvertTo-Json -Compress)) { throw "KPI mismatch" }
if (($data.reconciliation | ConvertTo-Json -Compress) -ne ($audit.reconciliation | ConvertTo-Json -Compress)) { throw "reconciliation mismatch" }
[ordered]@{
  schema_version = 1
  generated_at = (Get-Date).ToUniversalTime().ToString("o")
  environment = "production"
  timezone = "Asia/Shanghai"
  range = @{ start = $start; end = $end }
  projection_version = $data.projection_version
  kpis = $data.kpis
  reconciliation = $data.reconciliation
  canonical_record_count = $data.reconciliation.canonical_record_count
  replaced_duplicate_count = $data.reconciliation.replaced_duplicate_count
  unassociated_record_count = $data.reconciliation.unassociated_record_count
  missing_provider_usage_count = $data.reconciliation.missing_provider_usage_count
  usage_audit_match = $true
} | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 usage-audit-production-reconciliation.json
```

Acceptance requires an operator-signed/readback-bound artifact containing the schema above, the exact production endpoint identity and non-secret request correlation metadata. `usage_audit_match` may be true only after all three exact comparisons pass. Missing provider usage and unassociated records must remain visible counts; they must not be backfilled or hidden.
