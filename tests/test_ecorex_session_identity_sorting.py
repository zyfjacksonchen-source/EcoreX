import tempfile
import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SessionIdentityOwnerContractTests(unittest.TestCase):
    def _project(self, project_id: str, root: str) -> dict:
        return {
            "projectId": project_id,
            "projectName": f"Project {project_id}",
            "projectPath": str(Path(root) / project_id),
            "memoryPath": str(Path(root) / project_id / ".ecorex" / "project-memory.md"),
            "dreamsPath": str(Path(root) / project_id / ".ecorex" / "dreams"),
        }

    def test_project_session_rejects_owner_overwrite(self):
        from agent.memory.conversation_store import ConversationSessionOwnerConflict, ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages(
                "session-owner",
                [{"role": "user", "content": "project A message"}],
                channel_type="web",
                project_context=self._project("project-a", workspace),
            )

            with self.assertRaises(ConversationSessionOwnerConflict) as raised:
                store.append_messages(
                    "session-owner",
                    [{"role": "user", "content": "project B message"}],
                    channel_type="web",
                    project_context=self._project("project-b", workspace),
                )

            page = store.load_history_page("session-owner", page=1, page_size=20)
            sessions = store.list_sessions(channel_type="web")["sessions"]

        self.assertEqual(raised.exception.reason, "project_owner_mismatch")
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(page["messages"][0]["content"], "project A message")
        self.assertEqual(page["project_context"]["projectId"], "project-a")
        self.assertEqual(sessions[0]["scope"], "project")
        self.assertEqual(sessions[0]["project"]["projectId"], "project-a")

    def test_general_session_cannot_be_silently_rebound_to_project(self):
        from agent.memory.conversation_store import ConversationSessionOwnerConflict, ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages(
                "session-general",
                [{"role": "user", "content": "general message"}],
                channel_type="web",
            )

            with self.assertRaises(ConversationSessionOwnerConflict) as raised:
                store.append_messages(
                    "session-general",
                    [{"role": "user", "content": "project message"}],
                    channel_type="web",
                    project_context=self._project("project-a", workspace),
                )

            page = store.load_history_page("session-general", page=1, page_size=20)
            sessions = store.list_sessions(channel_type="web")["sessions"]

        self.assertEqual(raised.exception.reason, "general_to_project_owner_change")
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(page["messages"][0]["content"], "general message")
        self.assertFalse(page.get("project_context"))
        self.assertEqual(sessions[0]["scope"], "general")
        self.assertIsNone(sessions[0]["project"])

    def test_same_project_can_refresh_metadata_and_append(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            first = self._project("project-a", workspace)
            second = {**first, "projectName": "Renamed Project A"}
            store.append_messages(
                "session-project",
                [{"role": "user", "content": "first"}],
                channel_type="web",
                project_context=first,
            )
            store.append_messages(
                "session-project",
                [{"role": "assistant", "content": "second"}],
                channel_type="web",
                project_context=second,
            )
            page = store.load_history_page("session-project", page=1, page_size=20)
            sessions = store.list_sessions(channel_type="web")["sessions"]

        self.assertEqual(page["total"], 2)
        self.assertEqual(page["project_context"]["projectId"], "project-a")
        self.assertEqual(page["project_context"]["projectName"], "Renamed Project A")
        self.assertEqual(sessions[0]["scope"], "project")
        self.assertEqual(sessions[0]["project"]["projectName"], "Renamed Project A")

    def test_list_sessions_can_include_specific_ids_outside_current_page(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            for session_id in ("session-old-pinned", "session-middle", "session-newest"):
                store.append_messages(
                    session_id,
                    [{"role": "user", "content": session_id}],
                    channel_type="web",
                )
            with store._lock:
                conn = store._connect()
                try:
                    conn.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (100, "session-old-pinned"))
                    conn.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (200, "session-middle"))
                    conn.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (300, "session-newest"))
                    conn.commit()
                finally:
                    conn.close()

            page = store.list_sessions(
                channel_type="web",
                page=1,
                page_size=1,
                include_session_ids=["session-old-pinned", "session-old-pinned"],
            )

        ids = [item["session_id"] for item in page["sessions"]]
        self.assertEqual(ids[0], "session-newest")
        self.assertIn("session-old-pinned", ids)
        self.assertEqual(ids.count("session-old-pinned"), 1)
        self.assertEqual(page["total"], 3)
        self.assertTrue(page["has_more"])
        self.assertIn("session-old-pinned", page["included_session_ids"])

    def test_load_messages_restores_user_attachment_references_for_next_turn_context(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            image_path = str(Path(workspace) / "paste-image.png")
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages(
                "session-image-context",
                [{
                    "role": "user",
                    "content": [{"type": "text", "text": "帮我看这张图"}],
                    "extras": {
                        "attachments": [{
                            "file_path": image_path,
                            "file_name": "paste-image.png",
                            "file_type": "image",
                        }]
                    },
                }],
                channel_type="web",
            )
            history_messages = store.load_messages("session-image-context", max_turns=10)
            ui_page = store.load_history_page("session-image-context", page=1, page_size=20)

        self.assertIn("[历史图片:", history_messages[0]["content"][0]["text"])
        self.assertIn(image_path, history_messages[0]["content"][0]["text"])
        self.assertEqual(ui_page["messages"][0]["content"], "帮我看这张图")
        self.assertEqual(ui_page["messages"][0]["extras"]["attachments"][0]["file_path"], image_path)

    def test_load_messages_restores_assistant_artifact_references_for_next_turn_context(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            artifact_path = str(Path(workspace) / "carousel-output.png")
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages(
                "session-artifact-context",
                [
                    {"role": "user", "content": "先生成一张轮播图"},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "已生成轮播图"}],
                        "extras": {
                            "artifacts": [{
                                "path": artifact_path,
                                "title": "轮播图",
                                "type": "image/png",
                            }]
                        },
                    },
                ],
                channel_type="web",
            )
            history_messages = store.load_messages("session-artifact-context", max_turns=10)
            ui_page = store.load_history_page("session-artifact-context", page=1, page_size=20)

        self.assertIn("[历史图片产物:", history_messages[1]["content"][0]["text"])
        self.assertIn("轮播图", history_messages[1]["content"][0]["text"])
        self.assertIn(artifact_path, history_messages[1]["content"][0]["text"])
        self.assertEqual(ui_page["messages"][1]["content"], "已生成轮播图")
        self.assertEqual(ui_page["messages"][1]["extras"]["artifacts"][0]["path"], artifact_path)

    def test_load_messages_limits_recovered_file_refs_per_message(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            long_path = str(Path(workspace) / ("x" * 700 + ".txt"))
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages(
                "session-context-budget",
                [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "整理这些本地文件"}],
                        "extras": {
                            "attachments": [
                                {"file_path": long_path, "file_type": "file"},
                                {"file_path": long_path, "file_type": "file"},
                            ] + [
                                {"file_path": str(Path(workspace) / f"file-{idx}.txt"), "file_type": "file"}
                                for idx in range(5)
                            ],
                        },
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "已输出文件"}],
                        "extras": {
                            "artifacts": [
                                {"path": str(Path(workspace) / f"artifact-{idx}.txt"), "title": f"artifact-{idx}", "type": "text/plain"}
                                for idx in range(5)
                            ],
                        },
                    },
                ],
                channel_type="web",
            )
            history_messages = store.load_messages("session-context-budget", max_turns=10)

        user_text = history_messages[0]["content"][0]["text"]
        assistant_text = history_messages[1]["content"][0]["text"]
        self.assertEqual(user_text.count("[历史文件:"), 4)
        self.assertEqual(assistant_text.count("[历史文件产物:"), 4)
        self.assertIn(long_path[:512], user_text)
        self.assertNotIn(long_path, user_text)
        self.assertNotIn("file-4.txt", user_text)
        self.assertNotIn("artifact-4.txt", assistant_text)
        self.assertIn("历史文件引用摘要", assistant_text)

    def test_load_messages_caps_recovered_file_refs_across_large_context(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            messages = []
            for msg_idx in range(12):
                messages.append({
                    "role": "user",
                    "content": f"处理第 {msg_idx} 批文件",
                    "extras": {
                        "attachments": [
                            {
                                "file_path": str(Path(workspace) / f"batch-{msg_idx}" / f"file-{file_idx}.txt"),
                                "file_type": "file",
                            }
                            for file_idx in range(4)
                        ],
                    },
                })
            store.append_messages("session-large-context-budget", messages, channel_type="web")
            history_messages = store.load_messages("session-large-context-budget", max_turns=20)

        joined = "\n".join(str(message["content"]) for message in history_messages)
        self.assertEqual(joined.count("[历史文件:"), 40)
        self.assertIn("历史文件引用摘要", joined)

    def test_web_channel_checks_owner_before_ui_state_binding_persist(self):
        source = Path("channel/web/web_channel.py").read_text(encoding="utf-8")

        owner_check = source.index("validate_session_owner(")
        binding_persist = source.index("_persist_project_session_binding(session_id, project_context_meta)")

        self.assertLess(owner_check, binding_persist)
        self.assertIn('"code": exc.code', source)
        self.assertIn('"error_type": "session_owner_conflict"', source)

    def test_owner_conflict_paths_do_not_echo_raw_identifiers_or_prompt(self):
        source = Path("channel/web/web_channel.py").read_text(encoding="utf-8")
        conflict_block = source[
            source.index("except ConversationSessionOwnerConflict as exc:"):
            source.index("_persist_project_session_binding(session_id, project_context_meta)")
        ]

        self.assertIn('"SESSION_OWNER_CONFLICT"', Path("agent/memory/conversation_store.py").read_text(encoding="utf-8"))
        self.assertIn("reason={exc.reason}", conflict_block)
        self.assertNotIn("session_id", conflict_block)
        self.assertNotIn("project_context_meta", conflict_block)
        self.assertNotIn("visible_message", conflict_block)
        self.assertNotIn("prompt", conflict_block)
        self.assertNotIn("projectId", conflict_block)
        self.assertNotIn("projectPath", conflict_block)

        for file_name in ("bridge/agent_bridge.py", "agent/chat/service.py"):
            text = Path(file_name).read_text(encoding="utf-8")
            start = text.index('getattr(e, "code", "") == "SESSION_OWNER_CONFLICT"')
            block = text[start:text.index("return", start) + 12]
            self.assertIn("reason=", block)
            self.assertNotIn("session_id", block)
            self.assertNotIn("project_context", block)
            self.assertNotIn("query", block)

    def test_frontend_session_sorting_uses_pin_groups_and_activity_sort_key(self):
        source = Path("desktop/src/App.tsx").read_text(encoding="utf-8")

        self.assertIn("sortKeyMs?: number", source)
        self.assertIn("if (typeof row.sortKeyMs === \"number\"", source)
        sort_function = source[source.index("function sessionActivityMs"):source.index("function sessionProjectIdFromState")]
        self.assertNotIn("timeMs(row.updatedAt)", sort_function)
        sort_block = source[source.index("return rows.sort((a, b) => {"):source.index("function estimateTextTokens")]
        self.assertIn("Number(Boolean(b.pinned)) - Number(Boolean(a.pinned))", sort_block)
        self.assertIn("sessionActivityMs(b) - sessionActivityMs(a)", sort_block)
        self.assertIn("return a.id.localeCompare(b.id)", sort_block)

    def test_frontend_runtime_snapshot_requests_pinned_and_active_session_includes(self):
        source = Path("desktop/src/services/ecorexApi.ts").read_text(encoding="utf-8")

        self.assertIn("RuntimeSnapshotOptions", source)
        self.assertIn("runtimeSnapshotSessionIncludes", source)
        self.assertIn("ecorex-pinned-sessions", source)
        self.assertIn("ecorex-last-active-session-id", source)
        self.assertIn('sessionsParams.set("include_ids"', source)
        self.assertIn('sessionsParams.set("include_pinned", "1")', source)
        self.assertNotIn('"/api/sessions?page=1&page_size=40"', source)

    def test_frontend_runtime_project_binding_wins_for_backend_sessions(self):
        source = Path("desktop/src/App.tsx").read_text(encoding="utf-8")
        runtime_block = source[
            source.index("const runtimeBinding = projectBindingFromRuntimeSession(session);"):
            source.index("return {", source.index("const runtimeBinding = projectBindingFromRuntimeSession(session);"))
        ]

        self.assertIn("const projectId = runtimeBinding?.projectId || (runtimeGeneralOwner ? null : sessionProjectIdFromState", runtime_block)
        self.assertNotIn("sessionProjectIdFromState(id, sessionProjects, sessionUiState, runtimeBinding?.projectId", runtime_block)

    def test_frontend_runtime_general_owner_blocks_stale_project_fallback(self):
        source = Path("desktop/src/App.tsx").read_text(encoding="utf-8")
        runtime_block = source[
            source.index("const runtimeBinding = projectBindingFromRuntimeSession(session);"):
            source.index("return {", source.index("const runtimeBinding = projectBindingFromRuntimeSession(session);"))
        ]

        self.assertIn("function runtimeSessionDeclaresGeneralOwner", source)
        self.assertIn('scope === "general"', source)
        self.assertIn('session.project === null', source)
        self.assertIn("const runtimeGeneralOwner = runtimeSessionDeclaresGeneralOwner(session);", runtime_block)
        self.assertIn("runtimeGeneralOwner ? null : sessionProjectIdFromState", runtime_block)
        self.assertIn("runtimeGeneralOwner && !runtimeBinding ? undefined : projectForSessionDisplay", runtime_block)

    def test_frontend_session_row_render_trusts_row_owner_without_local_fallback(self):
        source = Path("desktop/src/App.tsx").read_text(encoding="utf-8")
        render_block = source[
            source.index("const renderSessionRow = (row: SessionRow) => {"):
            source.index("function renderMessageRunTiming", source.index("const renderSessionRow = (row: SessionRow) => {"))
        ]

        self.assertIn("const rowProjectId = row.projectId || null;", render_block)
        self.assertNotIn("sessionProjectIdFromState(row.id", render_block)

    def test_run_ledger_request_owner_is_immutable(self):
        from agent.protocol import reset_run_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            self.assertTrue(ledger.create_run("req-owner", "session-a", phase="accepted"))
            self.assertTrue(ledger.create_run(
                "req-owner",
                "session-a",
                phase="tool_running",
                status="running",
                metadata={"same_session_refresh": True},
            ))
            self.assertFalse(ledger.create_run("req-owner", "session-b", phase="accepted"))
            row = ledger.get_run("req-owner")

        self.assertEqual(row["session_id"], "session-a")
        self.assertEqual(row["phase"], "tool_running")
        self.assertEqual(row["metadata"]["same_session_refresh"], True)

    def test_run_events_reject_mixed_owner_and_projection_filters_legacy_rows(self):
        from agent.protocol import (
            RunEventOwnerConflict,
            RuntimeProjectionService,
            reset_run_event_ledger_for_tests,
            reset_run_ledger_for_tests,
        )

        with tempfile.TemporaryDirectory() as workspace:
            db_path = Path(workspace) / "runtime.db"
            run_ledger = reset_run_ledger_for_tests(db_path)
            event_ledger = reset_run_event_ledger_for_tests(db_path)
            run_ledger.create_run("req-owner", "session-a", phase="accepted")
            event_ledger.append_event(
                request_id="req-owner",
                session_id="session-a",
                event_type="run.accepted",
                payload={},
                idempotency_key="req-owner:accepted",
            )
            event_ledger.append_event(
                request_id="req-owner",
                session_id="session-a",
                event_type="assistant.delta",
                payload={"content": "alpha"},
                idempotency_key="req-owner:delta-a",
            )

            with self.assertRaises(RunEventOwnerConflict):
                event_ledger.append_event(
                    request_id="req-owner",
                    session_id="session-b",
                    event_type="assistant.delta",
                    payload={"content": "wrong"},
                    idempotency_key="req-owner:delta-b",
                )

            with event_ledger._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_run_events (
                        request_id, session_id, turn_id, event_seq, event_type,
                        payload_json, idempotency_key, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "req-owner",
                        "session-b",
                        "req-owner",
                        99,
                        "assistant.delta",
                        '{"content":"wrong"}',
                        "legacy-wrong-owner",
                        "legacy-test",
                        123.0,
                    ),
                )
                conn.commit()

            service = RuntimeProjectionService(event_ledger)
            request_projection = service.request_projection("req-owner")
            session_a_projection = service.session_projection("session-a")
            session_b_projection = service.session_projection("session-b")

        self.assertEqual(request_projection["session_id"], "session-a")
        self.assertEqual(request_projection["messages"][0]["content"], "alpha")
        self.assertEqual(len(session_a_projection["requests"]), 1)
        self.assertEqual(session_a_projection["requests"][0]["messages"][0]["content"], "alpha")
        self.assertEqual(session_b_projection["requests"], [])
        self.assertEqual(session_b_projection["events"], [])

    def test_runtime_project_event_uses_minimal_project_summary(self):
        source = Path("channel/web/web_channel.py").read_text(encoding="utf-8")
        record_block = source[
            source.index("def _record_request_accepted_events"):
            source.index("def _safe_runtime_artifact_payload")
        ]

        self.assertIn("def _project_context_event_summary", source)
        self.assertIn('"project_context": _project_context_event_summary(project_context_meta)', record_block)
        self.assertIn('"bindingHash": digest', source)
        self.assertNotIn('"project_context": project_context_meta', record_block)


if __name__ == "__main__":
    unittest.main()
