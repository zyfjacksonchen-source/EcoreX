from __future__ import annotations

from pathlib import Path

import pytest

from ecorex.release.evidence_io import (
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)


def test_evidence_reader_rejects_nonfinite_json_and_existing_receipt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "evidence.json"
    source.write_bytes(b'{"duration":NaN}')
    payload = read_stable_regular_file(
        source, maximum_bytes=1024, code="evidence_invalid"
    )
    with pytest.raises(ValueError, match="evidence_invalid"):
        strict_json_loads(payload, code="evidence_invalid")

    output = tmp_path / "receipt.json"
    write_new_json_file({"status": "passed"}, output, code="receipt_exists")
    with pytest.raises(ValueError, match="receipt_exists"):
        write_new_json_file({"status": "forged"}, output, code="receipt_exists")
    assert output.read_text(encoding="utf-8") == '{"status":"passed"}\n'


def test_evidence_reader_rejects_symlink_or_reparse_file(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text('{"status":"passed"}', encoding="utf-8")
    linked = tmp_path / "linked.json"
    try:
        linked.symlink_to(source)
    except OSError:
        pytest.skip("this host does not grant symlink creation")
    with pytest.raises(ValueError, match="evidence_link_refused"):
        read_stable_regular_file(
            linked,
            maximum_bytes=1024,
            code="evidence_link_refused",
        )
