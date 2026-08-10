from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import hashlib
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ecorex.memory import MemoryContentNotFound, MemoryContentUnavailable, MemoryService
from ecorex.runtime import create_app
from ecorex.runtime.api import RuntimeSettings
from ecorex.workspace_content.service import (
    WorkspaceContentConflict,
    WorkspaceContentRejected,
    WorkspaceContentService,
    WorkspaceContentUnavailable,
)
import ecorex.workspace_content.service as workspace_content_module


def test_knowledge_workspace_is_recursive_searchable_linked_and_no_overwrite(tmp_path) -> None:
    service = WorkspaceContentService(tmp_path / "workspace")
    service.create_category("产品")
    service.create_document("产品/方案.md", "# 方案\n[执行](../执行.md)")
    service.create_document("执行.md", "# 执行\n严格验收")
    service.create_document("坏链接.md", "[根绝对](/执行.md)\n[坏编码](%ZZ.md)")

    tree = service.tree("严格")
    assert [item["path"] for item in tree["items"]] == ["执行.md"]
    assert service.document("产品/方案.md")["links"] == ["执行.md"]
    assert service.document("坏链接.md")["links"] == []
    assert service.graph()["edges"] == [{"source": "产品/方案.md", "target": "执行.md"}]

    with pytest.raises(WorkspaceContentConflict):
        service.create_document("执行.md", "不会覆盖")
    assert service.document("执行.md")["content"] == "# 执行\n严格验收"


def test_knowledge_import_renames_collisions_and_rejects_unsafe_content(tmp_path) -> None:
    service = WorkspaceContentService(tmp_path / "workspace")
    service.create_document("资料.md", "原内容")
    imported = service.import_documents(
        "",
        (("资料.md", "新内容".encode()), ("说明.txt", "中文".encode())),
    )
    assert [item["path"] for item in imported["items"]] == ["资料 (2).md", "说明.txt"]
    assert service.document("资料.md")["content"] == "原内容"

    for path in ("../逃逸.md", "/绝对.md", "目录\\文件.md", "脚本.py"):
        with pytest.raises(WorkspaceContentRejected):
            service.create_document(path, "x")
    rejected = service.import_documents(
        "",
        (("nul.md", b"a\0b"), ("bad.md", b"\xff")),
    )
    assert rejected["imported_count"] == 0
    assert rejected["rejected_count"] == 2
    assert [item["status"] for item in rejected["items"]] == ["rejected", "rejected"]


def test_knowledge_mutations_replay_persistently_and_import_reports_each_file(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    database = tmp_path / "runtime.db"
    service = WorkspaceContentService(workspace, database=database)
    service.create_document("资料.md", "原内容")
    created = service.create_document(
        "幂等.md",
        "同一内容",
        client_request_id="create-document-0001",
    )
    imported = service.import_documents(
        "",
        (("资料.md", "新内容".encode()), ("脚本.py", b"unsafe")),
        client_request_id="import-documents-0001",
    )
    assert created["path"] == "幂等.md"
    assert imported["imported_count"] == 1
    assert imported["rejected_count"] == 1
    assert imported["items"] == [
        {
            "original_name": "资料.md",
            "name": "资料 (2).md",
            "path": "资料 (2).md",
            "status": "renamed",
            "reason": None,
        },
        {
            "original_name": "脚本.py",
            "name": None,
            "path": None,
            "status": "rejected",
            "reason": "imports must be Markdown or text",
        },
    ]

    restarted = WorkspaceContentService(workspace, database=database)
    assert restarted.create_document(
        "幂等.md",
        "同一内容",
        client_request_id="create-document-0001",
    )["path"] == "幂等.md"
    assert restarted.import_documents(
        "",
        (("资料.md", "新内容".encode()), ("脚本.py", b"unsafe")),
        client_request_id="import-documents-0001",
    ) == imported
    assert not (restarted.root / "资料 (3).md").exists()
    assert not (workspace / ".emate-state").exists()

    with pytest.raises(WorkspaceContentConflict):
        restarted.create_document(
            "幂等.md",
            "不同内容",
            client_request_id="create-document-0001",
        )

    with service.database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE knowledge_mutation_requests SET plan_json='{}' "
                "WHERE client_request_id='create-document-0001'"
            )


def test_same_knowledge_request_converges_under_concurrency(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    database = tmp_path / "runtime.db"
    first = WorkspaceContentService(workspace, database=database)
    second = WorkspaceContentService(workspace, database=database)

    def create(service: WorkspaceContentService) -> str:
        return service.create_document(
            "并发.md",
            "唯一内容",
            client_request_id="concurrent-document-0001",
        )["path"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(create, (first, second))) == ["并发.md", "并发.md"]
    assert first.document("并发.md")["content"] == "唯一内容"


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "symlink"), reason="POSIX dir_fd race test")
def test_knowledge_write_never_escapes_a_replaced_parent(tmp_path, monkeypatch) -> None:
    service = WorkspaceContentService(tmp_path / "workspace")
    service.create_category("safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_link = os.link
    swapped = False

    def replace_parent_before_publish(source, target, *args, **kwargs):
        nonlocal swapped
        if target == "race.md" and not swapped:
            swapped = True
            service.root.joinpath("safe").rename(service.root / "safe-old")
            os.symlink(outside, service.root / "safe")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", replace_parent_before_publish)
    with pytest.raises(WorkspaceContentUnavailable):
        service.create_document("safe/race.md", "private")
    assert not (outside / "race.md").exists()


def test_knowledge_search_and_graph_enforce_a_total_read_budget(tmp_path, monkeypatch) -> None:
    service = WorkspaceContentService(tmp_path / "workspace")
    service.create_document("one.md", "1234")
    service.create_document("two.md", "5678")
    monkeypatch.setattr(workspace_content_module, "MAX_IMPORT_BYTES", 6)

    with pytest.raises(WorkspaceContentRejected, match="scan limit"):
        service.tree("absent")
    with pytest.raises(WorkspaceContentRejected, match="scan limit"):
        service.graph()


def test_knowledge_rejects_ambiguous_cross_platform_paths(tmp_path) -> None:
    service = WorkspaceContentService(tmp_path / "workspace")

    for path in (
        ".hidden.md",
        "CON.md",
        "lpt9.txt",
        "trailing .md ",
        "question?.md",
        "control\x1f.md",
        f"{'a' * 129}.md",
        "/".join(["part"] * 17) + ".md",
    ):
        with pytest.raises(WorkspaceContentRejected):
            service.create_document(path, "x")

    normalized = service.create_document("Ａ.md", "NFKC")
    assert normalized["path"] == "A.md"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink support")
def test_knowledge_never_follows_symlinks(tmp_path) -> None:
    service = WorkspaceContentService(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    try:
        os.symlink(outside, service.root / "linked")
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(WorkspaceContentRejected):
        service.tree()
    with pytest.raises(WorkspaceContentRejected):
        service.document("linked/secret.md")


def test_memory_projection_reads_only_active_database_rows_and_verified_cas(tmp_path) -> None:
    payload = "# 用户记忆".encode()
    digest = hashlib.sha256(payload).hexdigest()
    blobs = {digest: payload}
    service = MemoryService(tmp_path / "runtime.db", blob_loader=blobs.__getitem__)
    authority_path = "/Users/private-user/Documents/memory/user.md"
    with service.database.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_files(path,source,legacy_hash,mtime,size_bytes,blob_sha256,"
            "availability,memory_origin) VALUES(?,?,?,?,?,?,?,?)",
            (authority_path, "learned", "hash", 1, len(payload), digest, "stored", "learned"),
        )
        connection.execute(
            "INSERT INTO memory_files(path,source,legacy_hash,mtime,size_bytes,availability,"
            "memory_origin,memory_state) VALUES(?,?,?,?,?,?,?,?)",
            ("memory/hidden.md", "learned", "hidden", 1, 1, "missing", "learned", "tombstoned"),
        )
        connection.execute(
            "INSERT INTO memory_canonical_records(record_id,legacy_chunk_id,scope,source,path,"
            "start_line,end_line,text,legacy_hash,memory_origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("record_1", "legacy_1", "user", "learned", "memory/evolution.md", 1, 1, "偏好严谨", "hash_1", "learned"),
        )

    files = service.content_page(view="files", page=1)
    assert files.total == 1
    item_id = "memfile_" + hashlib.sha256(authority_path.encode()).hexdigest()
    assert files.items[0].item_id == item_id
    assert files.items[0].path == "memory/user.md"
    assert "private-user" not in str(files.to_dict())
    assert service.content_document(view="files", item_id=item_id).content == "# 用户记忆"
    assert service.content_document(view="evolution", item_id="record_1").content == "偏好严谨"
    with pytest.raises(MemoryContentNotFound):
        service.content_document(view="files", item_id="memory/hidden.md")

    unavailable = MemoryService(tmp_path / "runtime.db")
    with pytest.raises(MemoryContentUnavailable):
        unavailable.content_document(view="files", item_id=item_id)

    corrupted = MemoryService(
        tmp_path / "runtime.db",
        blob_loader=lambda _digest: b"x" * len(payload),
    )
    with pytest.raises(MemoryContentUnavailable):
        corrupted.content_document(view="files", item_id=item_id)

    with pytest.raises(ValueError):
        service.content_page(view="files", page=1_000_001)


def test_knowledge_and_memory_http_contracts_are_real_and_csrf_protected(tmp_path) -> None:
    token = "r" * 32
    csrf = "c" * 32
    workspace = tmp_path / "workspace"
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            workspace_root=workspace,
            runtime_bearer_token=token,
            csrf_token=csrf,
            webui_origins=("http://testserver",),
        )
    )
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {token}"}
    mutation = {**auth, "Origin": "http://testserver", "X-EcoreX-CSRF": csrf}

    assert client.get("/api/v1/knowledge/tree", headers=auth).json() == {
        "root": "knowledge",
        "query": None,
        "items": [],
    }
    denied = client.post(
        "/api/v1/knowledge/documents",
        headers=auth,
        json={"path": "第一篇.md", "content": "# 第一篇"},
    )
    assert denied.status_code == 403
    created = client.post(
        "/api/v1/knowledge/documents",
        headers=mutation,
        json={
            "path": "第一篇.md",
            "content": "# 第一篇",
            "client_request_id": "http-create-document-0001",
        },
    )
    assert created.status_code == 200
    assert created.json()["path"] == "第一篇.md"
    imported = client.post(
        "/api/v1/knowledge/imports",
        headers=mutation,
        data={
            "category_path": "",
            "client_request_id": "http-import-documents-0001",
        },
        files=[("files", ("第一篇.md", "# 第二篇".encode(), "text/markdown"))],
    )
    assert imported.status_code == 200
    assert imported.json()["items"][0]["path"] == "第一篇 (2).md"
    replay = client.post(
        "/api/v1/knowledge/imports",
        headers=mutation,
        data={
            "category_path": "",
            "client_request_id": "http-import-documents-0001",
        },
        files=[("files", ("第一篇.md", "# 第二篇".encode(), "text/markdown"))],
    )
    assert replay.json()["items"][0]["path"] == "第一篇 (2).md"
    assert client.get("/api/v1/memory/files", headers=auth).json()["page_size"] == 10

    openapi = app.openapi()
    for path in (
        "/api/v1/knowledge/tree",
        "/api/v1/knowledge/document",
        "/api/v1/knowledge/graph",
        "/api/v1/knowledge/categories",
        "/api/v1/knowledge/documents",
        "/api/v1/knowledge/imports",
        "/api/v1/memory/files",
        "/api/v1/memory/file",
    ):
        assert path in openapi["paths"]
