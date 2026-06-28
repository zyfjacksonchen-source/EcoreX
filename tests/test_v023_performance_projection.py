import tempfile
import threading
import time
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path


class CountingProjectionLedger:
    def __init__(self, base):
        self.base = base
        self.events_for_request_calls = 0
        self.events_for_requests_calls = 0
        self.latest_event_id_for_request_calls = 0
        self.latest_event_id_for_session_calls = 0
        self.list_events_calls = 0

    def append_event(self, **kwargs):
        return self.base.append_event(**kwargs)

    def list_events(self, **kwargs):
        self.list_events_calls += 1
        return self.base.list_events(**kwargs)

    def events_for_request(self, request_id, *, limit=5000):
        self.events_for_request_calls += 1
        return self.base.events_for_request(request_id, limit=limit)

    def events_for_requests(self, request_ids, *, limit=0):
        self.events_for_requests_calls += 1
        return self.base.events_for_requests(request_ids, limit=limit)

    def owner_session_id_for_request(self, request_id):
        return self.base.owner_session_id_for_request(request_id)

    def latest_event_id_for_request(self, request_id):
        self.latest_event_id_for_request_calls += 1
        return self.base.latest_event_id_for_request(request_id)

    def latest_event_id_for_session(self, session_id):
        self.latest_event_id_for_session_calls += 1
        return self.base.latest_event_id_for_session(session_id)


class V023RuntimeProjectionPerformanceTests(unittest.TestCase):
    def test_session_projection_batches_request_event_replay_without_n_plus_one(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "projection-batch.db")
            session_id = "perf-session-batch"
            for index in range(30):
                request_id = f"perf-request-{index:03d}"
                ledger.append_event(
                    request_id=request_id,
                    session_id=session_id,
                    event_type="run.accepted",
                    payload={"turn_id": f"turn-{index:03d}"},
                )
                ledger.append_event(
                    request_id=request_id,
                    session_id=session_id,
                    event_type="message.assistant.finalized",
                    payload={"content": f"done {index}"},
                )

            counting = CountingProjectionLedger(ledger)
            projection = RuntimeProjectionService(counting).session_projection(
                session_id,
                limit=0,
            )

        self.assertEqual(len(projection["requests"]), 30)
        self.assertEqual(counting.list_events_calls, 1)
        self.assertEqual(counting.events_for_requests_calls, 1)
        self.assertEqual(counting.events_for_request_calls, 0)
        self.assertEqual(projection["requests"][-1]["messages"][-1]["content"], "done 29")

    def test_session_projection_cache_reuses_projection_until_latest_session_event_changes(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "projection-session-cache.db")
            session_id = "perf-session-cache-session"
            for index in range(5):
                request_id = f"perf-session-cache-request-{index}"
                ledger.append_event(
                    request_id=request_id,
                    session_id=session_id,
                    event_type="run.accepted",
                    payload={"turn_id": f"turn-{index}"},
                )
                ledger.append_event(
                    request_id=request_id,
                    session_id=session_id,
                    event_type="message.assistant.finalized",
                    payload={"content": f"done {index}"},
                )

            counting = CountingProjectionLedger(ledger)
            service = RuntimeProjectionService(counting)
            first = service.session_projection(session_id, limit=0, include_events=False)
            second = service.session_projection(session_id, limit=0, include_events=False)
            ledger.append_event(
                request_id="perf-session-cache-request-5",
                session_id=session_id,
                event_type="run.accepted",
                payload={"turn_id": "turn-5"},
            )
            third = service.session_projection(session_id, limit=0, include_events=False)

        self.assertEqual(len(first["requests"]), 5)
        self.assertEqual(len(second["requests"]), 5)
        self.assertEqual(len(third["requests"]), 6)
        self.assertEqual(counting.latest_event_id_for_session_calls, 3)
        self.assertEqual(counting.list_events_calls, 2)
        self.assertEqual(counting.events_for_requests_calls, 2)

    def test_request_projection_cache_reuses_projection_until_latest_event_changes(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "projection-cache.db")
            request_id = "perf-request-cache"
            session_id = "perf-session-cache"
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="run.accepted",
                payload={"turn_id": "turn-cache"},
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="message.assistant.finalized",
                payload={"content": "first"},
            )

            counting = CountingProjectionLedger(ledger)
            service = RuntimeProjectionService(counting)
            first = service.request_projection(request_id, expected_session_id=session_id)
            second = service.request_projection(request_id, expected_session_id=session_id)

            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="message.assistant.finalized",
                payload={"content": "second"},
                created_at=time.time() + 1,
            )
            third = service.request_projection(request_id, expected_session_id=session_id)

        self.assertEqual(first["messages"][-1]["content"], "first")
        self.assertEqual(second["messages"][-1]["content"], "first")
        self.assertEqual(third["messages"][-1]["content"], "second")
        self.assertEqual(counting.events_for_request_calls, 2)
        self.assertEqual(counting.latest_event_id_for_request_calls, 3)
        self.assertEqual(len(service._request_projection_cache), 2)

    def test_session_projection_can_skip_event_sanitization_for_default_public_path(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "projection-include-events.db")
            session_id = "perf-session-include-events"
            request_id = "perf-request-include-events"
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="run.accepted",
                payload={"turn_id": "turn-include"},
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="message.assistant.finalized",
                payload={"content": "visible only in message"},
            )
            service = RuntimeProjectionService(ledger)
            light = service.session_projection(session_id, limit=0, include_events=False)
            diagnostic = service.session_projection(session_id, limit=0, include_events=True)

        self.assertEqual(light["events"], [])
        self.assertEqual(light["requests"][0]["events"], [])
        self.assertGreater(len(diagnostic["events"]), 0)
        self.assertGreater(len(diagnostic["requests"][0]["events"]), 0)
        self.assertEqual(light["requests"][0]["messages"][-1]["content"], "visible only in message")


class V023FrontendRenderPerformanceTests(unittest.TestCase):
    def test_long_reply_and_process_details_are_bounded_until_expanded(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "desktop" / "src" / "components" / "MessageContent.tsx").read_text(encoding="utf-8")

        self.assertIn("const LONG_REPLY_PREVIEW_CHARS = 1400;", source)
        self.assertIn("const previewContent = content.length > LONG_REPLY_PREVIEW_CHARS", source)
        self.assertIn("content.slice(0, LONG_REPLY_PREVIEW_CHARS)", source)
        self.assertIn("<MarkdownBlock content={previewContent}", source)
        self.assertNotIn('<MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />\n        {artifactShelf}\n      </div>\n      <button className="long-answer-toggle long-answer-expand-bottom"', source)
        self.assertIn("const [open, setOpen] = useState(false);", source)
        self.assertIn("open={open}", source)
        self.assertIn("onToggle={(event) => setOpen(event.currentTarget.open)}", source)
        self.assertIn("{open && (", source)
        self.assertIn("steps.map((step, index) => renderStep", source)


class V023ResourceLifecyclePerformanceTests(unittest.TestCase):
    def test_scheduler_stop_releases_background_thread_without_waiting_for_poll_interval(self):
        from agent.tools.scheduler.scheduler_service import SchedulerService

        class EmptyTaskStore:
            def list_tasks(self, enabled_only=True):
                return []

        service = SchedulerService(EmptyTaskStore(), lambda _task: True)
        started = time.perf_counter()
        service.start()
        self.assertTrue(service.thread and service.thread.is_alive())
        service.stop()
        stop_ms = (time.perf_counter() - started) * 1000.0

        self.assertFalse(service.running)
        self.assertIsNone(service.thread)
        self.assertLess(stop_ms, 1000)

    def test_scheduler_callback_errors_are_redacted_in_logs_and_task_state(self):
        from unittest.mock import patch
        from agent.tools.scheduler import scheduler_service as scheduler_service_module
        from agent.tools.scheduler.scheduler_service import SchedulerService

        class RecordingTaskStore:
            def __init__(self):
                self.updates = []

            def update_task(self, task_id, values):
                self.updates.append((task_id, dict(values)))

        secret = "private scheduler callback body sk-scheduler-secret-123456"
        store = RecordingTaskStore()
        service = SchedulerService(store, lambda _task: (_ for _ in ()).throw(RuntimeError(secret)))
        logged = []

        with patch.object(scheduler_service_module.logger, "error", side_effect=lambda message: logged.append(str(message))):
            ok = service._execute_task({"id": "task-secret"})

        encoded = json.dumps({"logs": logged, "updates": store.updates}, ensure_ascii=False)
        self.assertFalse(ok)
        self.assertIn("Details redacted", encoded)
        self.assertIn("RuntimeError", encoded)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("sk-scheduler-secret", encoded)
        self.assertEqual(store.updates[0][0], "task-secret")

    def test_scheduler_schedule_parse_errors_are_redacted_in_logs(self):
        from datetime import datetime
        from unittest.mock import patch
        from agent.tools.scheduler import scheduler_service as scheduler_service_module
        from agent.tools.scheduler.scheduler_service import SchedulerService

        class RecordingTaskStore:
            def update_task(self, _task_id, _values):
                pass

            def delete_task(self, _task_id):
                pass

        service = SchedulerService(RecordingTaskStore(), lambda _task: True)
        logged = []
        next_run_secret = "private schedule sk-next-run-secret-123456"
        cron_secret = "private cron sk-cron-secret-123456"
        runat_secret = "private runat sk-runat-secret-123456"

        with patch.object(scheduler_service_module.logger, "error", side_effect=lambda message: logged.append(str(message))):
            self.assertFalse(service._is_task_due({
                "id": "task-next-run",
                "next_run_at": next_run_secret,
                "schedule": {"type": "once", "run_at": "2099-01-01T00:00:00"},
            }, datetime.now()))
            self.assertIsNone(service._calculate_next_run({
                "id": "task-cron",
                "schedule": {"type": "cron", "expression": cron_secret},
            }, datetime.now()))
            self.assertIsNone(service._calculate_next_run({
                "id": "task-runat",
                "schedule": {"type": "once", "run_at": runat_secret},
            }, datetime.now()))

        encoded = json.dumps(logged, ensure_ascii=False)
        self.assertIn("Details redacted", encoded)
        for value in (next_run_secret, cron_secret, runat_secret):
            self.assertNotIn(value, encoded)
        for token in ("sk-next-run-secret", "sk-cron-secret", "sk-runat-secret"):
            self.assertNotIn(token, encoded)

    def test_scheduler_loop_and_task_processing_errors_are_redacted_in_logs(self):
        from unittest.mock import patch
        from agent.tools.scheduler import scheduler_service as scheduler_service_module
        from agent.tools.scheduler.scheduler_service import SchedulerService

        loop_secret = "private scheduler list body sk-list-secret-123456"
        task_secret = "private scheduler task body sk-task-secret-123456"
        logged = []

        class RaisingTaskStore:
            def list_tasks(self, enabled_only=True):
                raise RuntimeError(loop_secret)

        loop_service = SchedulerService(RaisingTaskStore(), lambda _task: True)
        loop_service.running = True

        def stop_after_first_wait(_timeout):
            loop_service.running = False
            return True

        with patch.object(scheduler_service_module.logger, "info", return_value=None), \
            patch.object(scheduler_service_module.logger, "error", side_effect=lambda message: logged.append(str(message))), \
            patch.object(loop_service._stop_event, "wait", side_effect=stop_after_first_wait):
            loop_service._run_loop()

        class SingleTaskStore:
            def list_tasks(self, enabled_only=True):
                return [{"id": "task-raw", "name": "safe"}]

        task_service = SchedulerService(SingleTaskStore(), lambda _task: True)
        with patch.object(scheduler_service_module.logger, "error", side_effect=lambda message: logged.append(str(message))), \
            patch.object(task_service, "_is_task_due", side_effect=RuntimeError(task_secret)):
            task_service._check_and_execute_tasks()

        encoded = json.dumps(logged, ensure_ascii=False)
        self.assertIn("Details redacted", encoded)
        self.assertNotIn(loop_secret, encoded)
        self.assertNotIn(task_secret, encoded)
        self.assertNotIn("sk-list-secret", encoded)
        self.assertNotIn("sk-task-secret", encoded)

    def test_image_job_service_prunes_terminal_jobs_without_removing_running_jobs(self):
        from agent.protocol import ImageJobService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "image-job-resource.db")
            service = ImageJobService(ledger)

            def instant_runner(task, progress, cancel_event):
                progress("progress", 0.5)
                return {"artifacts": [{"id": task["task_id"], "kind": "image", "title": "synthetic"}]}

            def blocking_runner(_task, _progress, cancel_event):
                cancel_event.wait(2)
                return {"artifacts": []}

            completed = service.start(
                request_id="perf-resource-completed",
                session_id="perf-resource-session",
                tasks=[{"operation": "generate"}],
                runner=instant_runner,
                job_id="image-job-resource-completed",
                synchronous=False,
            )
            running = service.start(
                request_id="perf-resource-running",
                session_id="perf-resource-session",
                tasks=[{"operation": "generate"}],
                runner=blocking_runner,
                job_id="image-job-resource-running",
                synchronous=False,
            )
            service.collect(completed["job_id"], wait=True, timeout=2)
            before = service.resource_snapshot()
            cleanup = service.cleanup_finished_jobs(max_age_seconds=0, max_jobs=0)
            after = cleanup["remaining"]
            service.cancel(running["job_id"])
            service.collect(running["job_id"], wait=True, timeout=2)

        self.assertGreaterEqual(before["terminalJobCount"], 1)
        self.assertEqual(before["runningJobCount"], 1)
        self.assertEqual(cleanup["removedJobCount"], 1)
        self.assertEqual(after["jobCount"], 1)
        self.assertEqual(after["runningJobCount"], 1)

    def test_image_job_cleanup_waits_for_synchronous_parallel_workers_to_drain(self):
        from agent.protocol import ImageJobService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "image-job-sync-drain.db")
            service = ImageJobService(ledger)
            slow_worker_started = threading.Event()
            release_slow_worker = threading.Event()

            def runner(task, _progress, _cancel_event):
                if task["task_id"] == "task-1":
                    raise RuntimeError("synthetic failure")
                slow_worker_started.set()
                release_slow_worker.wait(2)
                return {"artifacts": []}

            def start_job():
                service.start(
                    request_id="perf-resource-sync-fail",
                    session_id="perf-resource-session",
                    tasks=[{"operation": "generate"}, {"operation": "generate"}],
                    runner=runner,
                    job_id="image-job-sync-drain",
                    max_parallel=2,
                    synchronous=True,
                )

            thread = threading.Thread(target=start_job, name="test-sync-image-job")
            thread.start()
            slow_worker_started.wait(timeout=2)
            deadline = time.time() + 2
            status = service.status("image-job-sync-drain")
            while time.time() < deadline and status.get("status") != "failed":
                time.sleep(0.01)
                status = service.status("image-job-sync-drain")
            cleanup = service.cleanup_finished_jobs(max_age_seconds=0, max_jobs=0)
            release_slow_worker.set()
            thread.join(timeout=3)
            final_cleanup = service.cleanup_finished_jobs(max_age_seconds=0, max_jobs=0)

        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["running"])
        self.assertEqual(cleanup["removedJobCount"], 0)
        self.assertEqual(cleanup["remaining"]["jobCount"], 1)
        self.assertEqual(final_cleanup["removedJobCount"], 1)
        self.assertFalse(thread.is_alive())

    def test_image_job_artifacts_are_redacted_in_status_api_ledger_and_projection(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests
        if "web" not in sys.modules:
            fake_web = types.ModuleType("web")

            class FakeHTTPError(Exception):
                pass

            fake_web.HTTPError = FakeHTTPError
            fake_web.ctx = types.SimpleNamespace(env={}, method="GET", status="")
            fake_web.header = lambda *args, **kwargs: None
            fake_web.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
            fake_web.data = lambda: b"{}"
            fake_web.notfound = lambda: FakeHTTPError("404")
            fake_web.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
            fake_web.config = types.SimpleNamespace(debug=False)
            fake_web.httpserver = types.SimpleNamespace(
                LogMiddleware=type("LogMiddleware", (), {"log": None}),
                StaticMiddleware=lambda app: app,
                WSGIServer=lambda *args, **kwargs: None,
            )
            sys.modules["web"] = fake_web
        from channel.web.web_channel import _image_job_projection_payload

        secret_artifact = {
            "id": "artifact-1",
            "kind": "image",
            "path": r"C:\Users\alice\Pictures\private-ocr-prompt.png",
            "url": "https://cdn.example.test/out.png?access_token=sk-artifact-secret-1234567890",
            "title": "private prompt OCR text title",
            "safeArtifactId": "artifact-1",
        }
        forbidden = [
            r"C:\Users\alice",
            "private-ocr-prompt.png",
            "access_token",
            "sk-artifact-secret",
            "private prompt OCR text title",
        ]

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "image-job-redaction.db")
            service = ImageJobService(ledger)

            def runner(_task, _progress, _cancel_event):
                return {"artifacts": [secret_artifact]}

            service.start(
                request_id="perf-resource-redaction",
                session_id="perf-resource-session",
                tasks=[{"operation": "generate"}],
                runner=runner,
                job_id="image-job-redaction",
                synchronous=True,
            )
            status = service.status("image-job-redaction")
            api_payload = _image_job_projection_payload(status, request_id="perf-resource-redaction")
            projection = RuntimeProjectionService(ledger).request_projection(
                "perf-resource-redaction",
                expected_session_id="perf-resource-session",
            )
            events = ledger.events_for_request("perf-resource-redaction", limit=0)
            service.cleanup_finished_jobs(max_age_seconds=0, max_jobs=0)
            after_cleanup = service.status("image-job-redaction")

        encoded = json.dumps(
            {
                "status": status,
                "api": api_payload,
                "projection": projection,
                "events": events,
                "afterCleanup": after_cleanup,
            },
            ensure_ascii=False,
        )
        for value in forbidden:
            self.assertNotIn(value, encoded)
        self.assertIn("artifact-ref-", encoded)
        self.assertIn("artifact-label-", encoded)
        self.assertEqual(after_cleanup["status"], "unknown")


class V023RefreshReplayPerformanceHarnessTests(unittest.TestCase):
    def test_refresh_replay_perf_smoke_writes_only_redacted_metrics(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "smoke-performance-refresh-replay.py").read_text(encoding="utf-8")

        self.assertIn("scenario\": \"refresh-replay\"", source)
        self.assertIn("_safe_cross_talk", source)
        self.assertIn("_safe_reconnect", source)
        self.assertIn("_safe_history", source)
        self.assertIn("duplicateMessageCount", source)
        self.assertIn("latestEventIdDelta", source)
        self.assertNotIn("visibleText", source)
        self.assertNotIn("rawMd", source)
        self.assertNotIn("document.body.innerText", source)
        self.assertNotIn("dataset.artifactUrl", source)


class V023BrowserOcrPerformanceHarnessTests(unittest.TestCase):
    def test_browser_ocr_perf_smoke_records_redacted_handoff_and_measured_flags(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "smoke-performance-browser-ocr.py").read_text(encoding="utf-8")
        artifact_path = root / "docs" / "v0.2.3" / "artifacts" / "perf-browser-ocr.json"

        self.assertIn('"scenario": "browser-ocr"', source)
        self.assertIn('"browserFirstActionMode"', source)
        self.assertIn('"browserFallbackMode"', source)
        self.assertIn('"ocrTextUrlP95Ms"', source)
        self.assertIn('"ocrImageUrlMeasured"', source)
        self.assertIn('providers.get("rapidocr")', source)
        self.assertIn('"rapidocrAvailable"', source)
        self.assertIn("OCR_IMAGE_P95_THRESHOLD_MS = 2000", source)
        self.assertIn('"ocr_image_url_unmeasured"', source)
        self.assertIn('"ocr_image_url_provider_not_rapidocr"', source)
        self.assertIn('"ocr_image_url_p95_over_threshold"', source)
        self.assertIn("_make_warmup_image", source)
        self.assertIn('"extract_text", "image": str(warmup_path)', source)
        self.assertIn("_hash(TARGET_URL)", source)
        self.assertIn("NODE_NO_WARNINGS", source)
        self.assertIn("logger.setLevel(logging.CRITICAL)", source)
        self.assertIn('cdp_first.get("mode") != "cdp"', source)
        self.assertIn('"browser_cdp_first_mode_not_cdp"', source)
        self.assertIn('cdp_first.get("autoLaunchedCdpProcessObserved") is not True', source)
        self.assertIn('"browser_cdp_process_not_observed"', source)
        self.assertIn('cdp_first.get("autoLaunchedCdpProcessAliveAfterClose") is not False', source)
        self.assertIn('"browser_cdp_process_cleanup_unmeasured_or_alive"', source)

        if artifact_path.exists():
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            encoded = json.dumps(artifact, ensure_ascii=False)
            self.assertEqual(artifact.get("scenario"), "browser-ocr")
            self.assertIn(artifact.get("status"), {"pass", "fail"})
            metrics = artifact.get("metrics") or {}
            self.assertTrue(metrics.get("ocrImageUrlMeasured"))
            self.assertLessEqual(float(metrics.get("ocrImageUrlP95Ms") or 999999), 2000)
            provider = ((artifact.get("ocr") or {}).get("imageUrl") or {}).get("provider")
            self.assertIn(provider, {"rapidocr_onnxruntime", "rapidocr"})
            self.assertNotIn("xhslink.com", encoded)
            self.assertNotIn("perfSmokeRedacted", encoded)
            self.assertNotIn("ecorex-browser-ocr-", encoded)
            self.assertNotIn("browser-cdp-profile", encoded)


class V023ImageArtifactOcrPerformanceHarnessTests(unittest.TestCase):
    def test_image_artifact_ocr_perf_smoke_records_redacted_cache_and_lifecycle_metrics(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "smoke-performance-image-artifact-ocr.py").read_text(encoding="utf-8")
        artifact_path = root / "docs" / "v0.2.3" / "artifacts" / "perf-image-artifact-ocr.json"

        self.assertIn('"scenario": "image-artifact-ocr"', source)
        self.assertIn('"slice": "R23-16P-07"', source)
        self.assertIn('"ocrReuseP95Ms"', source)
        self.assertIn('"artifactMergeP95Ms"', source)
        self.assertIn('"payloadBytes"', source)
        self.assertIn('"projectedArtifactFingerprintCount"', source)
        self.assertIn('"projectedArtifactShapeValidCount"', source)
        self.assertIn('"completedPath"', source)
        self.assertIn('"failurePath"', source)
        self.assertIn('"cancelPath"', source)
        self.assertIn('"retryPath"', source)
        self.assertIn('"artifact_projection_unique_count"', source)
        self.assertIn('"artifact_projection_shape"', source)
        self.assertIn('"event_count_threshold"', source)
        self.assertIn('"payload_bytes_threshold"', source)
        self.assertIn("RuntimeProjectionService(ledger).request_projection", source)
        self.assertIn("service.cleanup_finished_jobs(max_age_seconds=0, max_jobs=0)", source)
        self.assertIn("_hash(\"perf-image-artifact-session\")", source)
        self.assertNotIn('"events":', source)
        self.assertNotIn('"session_id":', source)
        self.assertNotIn('"request_id":', source)
        self.assertNotIn('"prompt":', source)

        if artifact_path.exists():
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            encoded = json.dumps(artifact, ensure_ascii=False)
            self.assertEqual(artifact.get("scenario"), "image-artifact-ocr")
            self.assertIn(artifact.get("status"), {"pass", "fail"})
            self.assertIn("ocrReuseP95Ms", artifact.get("metrics") or {})
            self.assertIn("artifactMergeP95Ms", artifact.get("metrics") or {})
            self.assertIn("projectedArtifactFingerprintCount", artifact.get("metrics") or {})
            self.assertIn("projectedArtifactShapeValidCount", artifact.get("metrics") or {})
            self.assertNotIn('"session_id"', encoded)
            self.assertNotIn('"request_id"', encoded)
            self.assertNotIn("stable-image-ref", encoded)
            self.assertNotIn("brief hash only", encoded)
            self.assertNotIn("synthetic_provider_failure", encoded)


class V023SchedulerSubagentPerformanceHarnessTests(unittest.TestCase):
    def test_scheduler_subagent_perf_smoke_records_redacted_projection_and_lifecycle_metrics(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "smoke-performance-scheduler-subagent.py").read_text(encoding="utf-8")
        artifact_path = root / "docs" / "v0.2.3" / "artifacts" / "perf-scheduler-subagent.json"

        self.assertIn('"scenario": "scheduler-subagent"', source)
        self.assertIn('"slice": "R23-16P-08"', source)
        self.assertIn('"subagentProjectionP95Ms"', source)
        self.assertIn('"schedulerProjectionP95Ms"', source)
        self.assertIn('"orphanThreadCount"', source)
        self.assertIn('"orphanTimerCount"', source)
        self.assertIn('"projectedSubagentToolShapeValidCount"', source)
        self.assertIn('"projectedSubagentToolFingerprintCount"', source)
        self.assertIn('"projectedSchedulerTaskShapeValidCount"', source)
        self.assertIn('"schedulerSendMessageTaskCount"', source)
        self.assertIn('"schedulerAgentTaskCount"', source)
        self.assertIn('"schedulerToolCallTaskCount"', source)
        self.assertIn('"schedulerSkillCallTaskCount"', source)
        self.assertIn('"completedSubagentCount"', source)
        self.assertIn('"timeoutSubagentCount"', source)
        self.assertIn('"cancelledSubagentCount"', source)
        self.assertIn('"failedSubagentCount"', source)
        self.assertIn('"projected_subagent_tool_shape"', source)
        self.assertIn('"projected_scheduler_task_shape"', source)
        self.assertIn('"scheduler_error_task_count"', source)
        self.assertIn("RuntimeProjectionService(ledger).request_projection", source)
        self.assertIn("scheduler_projection(str(root))", source)
        self.assertIn("SchedulerService(EmptyTaskStore()", source)
        self.assertIn('"subagent_projection_p95_ms"', source)
        self.assertIn('"scheduler_projection_p95_ms"', source)
        self.assertIn('"orphan_thread_count"', source)
        self.assertIn('"orphan_timer_count"', source)
        self.assertNotIn('"events":', source)
        self.assertNotIn('"session_id":', source)
        self.assertNotIn('"request_id":', source)

        if artifact_path.exists():
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            encoded = json.dumps(artifact, ensure_ascii=False)
            self.assertEqual(artifact.get("scenario"), "scheduler-subagent")
            self.assertIn(artifact.get("status"), {"pass", "fail"})
            metrics = artifact.get("metrics") or {}
            self.assertIn("subagentProjectionP95Ms", metrics)
            self.assertIn("schedulerProjectionP95Ms", metrics)
            self.assertIn("orphanThreadCount", metrics)
            self.assertIn("projectedSubagentToolShapeValidCount", metrics)
            self.assertIn("projectedSchedulerTaskShapeValidCount", metrics)
            self.assertNotIn('"session_id"', encoded)
            self.assertNotIn('"request_id"', encoded)
            self.assertNotIn("receiver-", encoded)
            self.assertNotIn("redacted scheduled", encoded)
            self.assertNotIn("sk-scheduler-subagent", encoded)


class V023PerformanceEvidenceAuditTests(unittest.TestCase):
    def test_performance_evidence_audit_uses_matrix_pairs_and_hashed_findings(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "audit-performance-evidence.py").read_text(encoding="utf-8")
        artifact_path = root / "docs" / "v0.2.3" / "artifacts" / "perf-evidence-audit.json"

        self.assertIn('"scenario": "performance-evidence-audit"', source)
        self.assertIn('"slice": "R23-16P-09"', source)
        self.assertIn("performance-harness-matrix.json", source)
        self.assertIn("scan-session-artifacts-privacy.py", source)
        self.assertIn("privacyArtifact", source)
        self.assertIn("findingTypeHash", source)
        self.assertIn("REQUIRED_SCENARIO_IDS", source)
        self.assertIn("requiredScenarioMissingCount", source)
        self.assertIn("matrixConfigIssueCount", source)
        self.assertIn("scenarioPairCount", source)
        self.assertIn("selfAuditScenarioCount", source)
        self.assertIn("SELF_SCENARIO_ID", source)
        self.assertIn("missingMainArtifactCount", source)
        self.assertIn("missingScanArtifactCount", source)
        self.assertIn("scanNotCleanCount", source)
        self.assertIn("findingBucketCount", source)
        self.assertNotIn('"session_id":', source)
        self.assertNotIn('"request_id":', source)
        self.assertNotIn('"project_id":', source)
        self.assertNotIn('"filePath":', source)
        self.assertNotIn('"content":', source)
        self.assertNotIn("matchedText", source)

        if artifact_path.exists():
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            encoded = json.dumps(artifact, ensure_ascii=False)
            metrics = artifact.get("metrics") or {}
            self.assertEqual(artifact.get("scenario"), "performance-evidence-audit")
            self.assertIn(artifact.get("status"), {"pass", "fail"})
            self.assertGreaterEqual(metrics.get("matrixScenarioCount") or 0, 8)
            self.assertGreaterEqual(metrics.get("scenarioPairCount") or 0, 7)
            self.assertEqual(metrics.get("selfAuditScenarioCount"), 1)
            self.assertEqual(metrics.get("requiredScenarioMissingCount"), 0)
            self.assertEqual(metrics.get("matrixConfigIssueCount"), 0)
            self.assertEqual(metrics.get("missingMainArtifactCount"), 0)
            self.assertEqual(metrics.get("missingScanArtifactCount"), 0)
            self.assertEqual(metrics.get("scanNotCleanCount"), 0)
            self.assertEqual(metrics.get("findingBucketCount"), 0)
            self.assertNotIn('"session_id"', encoded)
            self.assertNotIn('"request_id"', encoded)
            self.assertNotIn('"project_id"', encoded)
            self.assertNotIn("xhslink.com", encoded)
            self.assertNotIn("perfSmokeRedacted", encoded)
            self.assertNotIn("browser-cdp-profile", encoded)

    def test_performance_evidence_audit_fails_closed_when_matrix_coverage_contract_breaks(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "audit-performance-evidence.py"
        base_row = {
            "id": "long-session-projection",
            "artifact": "docs/v0.2.3/artifacts/perf-long-session.json",
            "privacyArtifact": "docs/v0.2.3/artifacts/perf-long-session-privacy-scan.json",
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            narrowed_matrix = tmp_root / "narrowed.json"
            narrowed_output = tmp_root / "narrowed-output.json"
            narrowed_matrix.write_text(json.dumps({"scenarios": [base_row]}), encoding="utf-8")
            narrowed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--matrix",
                    str(narrowed_matrix),
                    "--output",
                    str(narrowed_output),
                    "--salt",
                    "v023-negative",
                ],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            narrowed_payload = json.loads(narrowed_output.read_text(encoding="utf-8"))
            self.assertNotEqual(narrowed.returncode, 0)
            self.assertEqual(narrowed_payload.get("status"), "fail")
            self.assertGreater(narrowed_payload.get("metrics", {}).get("requiredScenarioMissingCount") or 0, 0)

            missing_pair_matrix = tmp_root / "missing-pair.json"
            missing_pair_output = tmp_root / "missing-pair-output.json"
            broken_row = dict(base_row)
            broken_row.pop("privacyArtifact")
            missing_pair_matrix.write_text(json.dumps({"scenarios": [broken_row]}), encoding="utf-8")
            missing_pair = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--matrix",
                    str(missing_pair_matrix),
                    "--output",
                    str(missing_pair_output),
                    "--salt",
                    "v023-negative",
                ],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            missing_pair_payload = json.loads(missing_pair_output.read_text(encoding="utf-8"))
            self.assertNotEqual(missing_pair.returncode, 0)
            self.assertEqual(missing_pair_payload.get("status"), "fail")
            self.assertGreater(missing_pair_payload.get("metrics", {}).get("matrixConfigIssueCount") or 0, 0)

    def test_performance_evidence_audit_does_not_require_stale_self_report(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "audit-performance-evidence.py"
        required_ids = [
            "long-session-projection",
            "frontend-render-state-isolation",
            "complex-task-soak",
            "refresh-replay",
            "browser-ocr",
            "image-artifact-ocr",
            "scheduler-subagent",
        ]
        rows = [
            {
                "id": scenario_id,
                "artifact": "docs/v0.2.3/artifacts/perf-long-session.json",
                "privacyArtifact": "docs/v0.2.3/artifacts/perf-long-session-privacy-scan.json",
            }
            for scenario_id in required_ids
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            matrix = tmp_root / "matrix.json"
            output = tmp_root / "generated-self-report.json"
            rows.append(
                {
                    "id": "performance-evidence-audit",
                    "artifact": str((tmp_root / "missing-before-run.json").as_posix()),
                    "privacyArtifact": str((tmp_root / "missing-before-run-scan.json").as_posix()),
                }
            )
            matrix.write_text(json.dumps({"scenarios": rows}), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--matrix",
                    str(matrix),
                    "--output",
                    str(output),
                    "--salt",
                    "v023-self-positive",
                ],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(payload.get("status"), "pass")
            self.assertEqual(payload.get("metrics", {}).get("matrixScenarioCount"), 8)
            self.assertEqual(payload.get("metrics", {}).get("scenarioPairCount"), 7)
            self.assertEqual(payload.get("metrics", {}).get("selfAuditScenarioCount"), 1)
            self.assertEqual(payload.get("metrics", {}).get("missingMainArtifactCount"), 0)
            self.assertEqual(payload.get("metrics", {}).get("missingScanArtifactCount"), 0)


class V023BrowserResourceCleanupTests(unittest.TestCase):
    def test_browser_service_waits_and_kills_auto_launched_cdp_process(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "agent" / "tools" / "browser" / "browser_service.py").read_text(encoding="utf-8")
        cleanup_slice = source[source.find("if self._cdp_process:"):source.find("self._cdp_process = None", source.find("if self._cdp_process:"))]

        self.assertIn("self._cdp_process.terminate()", cleanup_slice)
        self.assertIn("self._cdp_process.wait(timeout=5)", cleanup_slice)
        self.assertIn("self._cdp_process.kill()", cleanup_slice)
        self.assertIn("self._cdp_process.wait(timeout=2)", cleanup_slice)


if __name__ == "__main__":
    unittest.main()
