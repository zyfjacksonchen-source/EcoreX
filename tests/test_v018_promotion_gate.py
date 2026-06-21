import importlib.util
import pathlib
import tempfile
import textwrap
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-ecorex-v0.1.18-promotion-gate.py"


def load_gate_module():
    spec = importlib.util.spec_from_file_location("v018_promotion_gate", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestV018PromotionGate(unittest.TestCase):
    def write_docs(self, root: pathlib.Path, *, status: str = "PASS") -> None:
        docs = root / "docs" / "v0.1.18"
        docs.mkdir(parents=True)
        row_ids = [
            "R18-01-A",
            "R18-01-B",
            "R18-01-C",
            "R18-02-A",
            "R18-02-B",
            "R18-02-C",
            "R18-03-A",
            "R18-03-B",
            "R18-03-C",
            "R18-03-D",
            "R18-04-A",
            "R18-04-B",
            "R18-04-C",
            "R18-04-D",
            "R18-05-A",
            "R18-05-B",
            "R18-06-A",
            "R18-07-A",
        ]
        rows = "\n".join(
            f"| {row_id} | Area | Standard | {status} | evidence |"
            for row_id in row_ids
        )
        (docs / "acceptance-checklist.md").write_text(
            textwrap.dedent(f"""
            # v0.1.18 Acceptance Checklist

            | ID | Area | Acceptance Standard | Status | Evidence |
            | --- | --- | --- | --- | --- |
            {rows}
            """).strip() + "\n",
            encoding="utf-8",
        )
        (docs / "evidence-ledger.md").write_text(
            textwrap.dedent("""
            # EcoreX v0.1.18 Evidence Ledger

            R18-RUN-LEDGER terminal-once confirmed dead-owner registry fallback suppression
            R18-SSE-CONTRACT run.failed stream.replay_gap request-scoped history recovery
            R18-CANCEL-CONCURRENCY backpressure subagent busy fallback
            R18-MODEL-GATEWAY model-call telemetry Retry policy Responses API
            R18-CONTEXT-BUDGET tool_schema_budget context_budget current-turn preservation
            R18-RUN-CENTER Run Center /api/active-requests diagnostics
            Multi-agent cross-review sidecar interruption Consensus: submit
            Multi-agent cross-review SSE replay-gap Consensus: submit
            Multi-agent cross-review model telemetry Consensus: submit
            Multi-agent cross-review desktop Run Center Consensus: submit
            """).strip() + "\n",
            encoding="utf-8",
        )

    def test_gate_reports_go_when_rows_and_evidence_pass(self):
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_docs(root)
            report = gate.build_report(root, "0.1.18")

        self.assertEqual(report["status"], "go")
        self.assertEqual(report["summary"]["blockers"], 0)

    def test_gate_reports_no_go_for_partial_acceptance_rows(self):
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_docs(root, status="PARTIAL")
            report = gate.build_report(root, "0.1.18")

        self.assertEqual(report["status"], "no-go")
        blockers = [item for item in report["checks"] if item["status"] == "fail"]
        self.assertTrue(any(item["name"] == "Acceptance rows: run-ledger" for item in blockers))

    def test_gate_detects_github_token_pattern(self):
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_docs(root)
            (root / "leak.txt").write_text("token=" + "ghp_" + ("A" * 30), encoding="utf-8")
            report = gate.build_report(root, "0.1.18")

        self.assertEqual(report["status"], "no-go")
        token_check = next(item for item in report["checks"] if item["name"] == "GitHub token pattern scan")
        self.assertEqual(token_check["status"], "fail")

    def test_gate_reports_no_go_when_token_scan_is_skipped(self):
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_docs(root)
            report = gate.build_report(root, "0.1.18", scan_tokens=False)

        self.assertEqual(report["status"], "no-go")
        token_check = next(item for item in report["checks"] if item["name"] == "GitHub token pattern scan")
        self.assertEqual(token_check["status"], "fail")
        self.assertEqual(token_check["severity"], "blocker")


if __name__ == "__main__":
    unittest.main()
