"""Image job Web handlers."""

import json

import web

from channel.web.handler_support import public_error_payload, require_auth, web_body_log_summary
from common.log import logger


def _legacy_web_channel():
    from channel.web import web_channel

    return web_channel


class ImageJobsHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        wc = _legacy_web_channel()
        try:
            params = web.input(job_id="", request_id="", requestId="", wait="", timeout="", include_events="")
            job_id = wc._safe_image_job_api_identifier(params.job_id, prefix="image-job", allow_empty=True)
            if not job_id:
                return json.dumps({"status": "error", "message": "job_id is required"}, ensure_ascii=False)
            action_name = "collect" if str(params.wait or "").lower() in {"1", "true", "yes", "on"} else "status"
            decision = wc._authorize_web_capability(
                "image_jobs",
                action_name,
                arguments={"action": action_name, "job_id": job_id},
                metadata={"surface": "web", "source": "image_jobs_api"},
            )
            if decision.get("allowed") is not True:
                return json.dumps(
                    wc._permission_denied_payload(
                        decision.get("reason", ""),
                        decision,
                        capability="image_jobs",
                        action=action_name,
                    ),
                    ensure_ascii=False,
                )
            from agent.protocol import get_image_job_service

            wait = str(params.wait or "").lower() in {"1", "true", "yes", "on"}
            try:
                timeout = float(params.timeout or 0) or None
            except (TypeError, ValueError):
                timeout = None
            service = get_image_job_service()
            job = service.collect(job_id, wait=wait, timeout=timeout) if wait else service.status(job_id)
            request_id = wc._safe_image_job_api_identifier(
                getattr(params, "request_id", "") or getattr(params, "requestId", ""),
                prefix="req-image-job",
                allow_empty=True,
            )
            if not request_id and job.get("status") == "unknown":
                request_id = wc._image_job_request_id_from_events(job_id)
            include_events = str(params.include_events or "").lower() in {"1", "true", "yes", "on"}
            return json.dumps(wc._image_job_projection_payload(job, include_events=include_events, request_id=request_id), ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] image job GET error: {web_body_log_summary(exc)}")
            return json.dumps({"status": "error", "message": "image job status failed"}, ensure_ascii=False)

    def POST(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        wc = _legacy_web_channel()
        try:
            body = json.loads(web.data() or b"{}")
            action = str(body.get("action") or "start").strip().lower()
            if action != "start":
                return json.dumps({"status": "error", "message": "unsupported image job action"}, ensure_ascii=False)
            tasks = wc._image_job_api_tasks(body)
            decision = wc._authorize_web_capability(
                "image_jobs",
                "start",
                arguments={"action": "start", "task_count": len(tasks), "operation": body.get("operation") or ""},
                metadata={"surface": "web", "source": "image_jobs_api", "user_initiated": True},
            )
            if decision.get("allowed") is not True:
                return json.dumps(
                    wc._permission_denied_payload(
                        decision.get("reason", ""),
                        decision,
                        capability="image_jobs",
                        action="start",
                    ),
                    ensure_ascii=False,
                )
            request_id = wc._safe_image_job_api_identifier(body.get("request_id") or body.get("requestId"), prefix="req-image-job", allow_empty=False)
            session_id = wc._safe_image_job_api_identifier(body.get("session_id") or body.get("sessionId"), allow_empty=True)
            turn_id = wc._safe_image_job_api_identifier(body.get("turn_id") or body.get("turnId"), allow_empty=True)
            job_id = wc._safe_image_job_api_identifier(body.get("job_id") or body.get("jobId"), prefix="image-job", allow_empty=True)
            operation = str(body.get("operation") or ("edit" if any(task.get("image_url") for task in tasks) else "generate"))
            from agent.protocol import get_image_job_service, resolve_image_job_parallelism_policy

            parallelism_policy = resolve_image_job_parallelism_policy(body, len(tasks))
            max_parallel = int(parallelism_policy.get("effective_max_parallel") or 1)
            ocr_reuse = wc._image_job_ocr_reuse_enabled(body)
            ocr_provider = wc._image_job_ocr_provider(body)

            job = get_image_job_service().start(
                request_id=request_id,
                session_id=session_id,
                turn_id=turn_id,
                operation=operation,
                tasks=tasks,
                runner=wc._image_job_runner(body),
                job_id=job_id,
                metadata={
                    "source": "web_channel",
                    "provider": body.get("provider") or "",
                    "model": body.get("model") or "",
                    "image_mode": operation,
                    "input_image_count": sum(1 for task in tasks if task.get("image_url")),
                    "output_count": len(tasks),
                    "ocr_cache_enabled": bool(ocr_provider and ocr_reuse),
                    **parallelism_policy,
                },
                max_parallel=max_parallel,
                ocr_provider=ocr_provider,
                ocr_reuse=ocr_reuse,
                synchronous=bool(body.get("synchronous")),
            )
            include_events = bool(body.get("include_events") or body.get("includeEvents"))
            return json.dumps(wc._image_job_projection_payload(job, include_events=include_events), ensure_ascii=False)
        except ValueError as exc:
            return json.dumps(wc._public_validation_error_payload(exc), ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] image job start error: {web_body_log_summary(exc)}")
            return json.dumps({"status": "error", "message": "image job start failed"}, ensure_ascii=False)


class ImageJobActionHandler:
    def POST(self, job_id: str):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        wc = _legacy_web_channel()
        try:
            body = json.loads(web.data() or b"{}")
            action = str(body.get("action") or "").strip().lower()
            safe_job_id = wc._safe_image_job_api_identifier(job_id, prefix="image-job", allow_empty=True)
            if not safe_job_id:
                return json.dumps({"status": "error", "message": "invalid job_id"}, ensure_ascii=False)
            from agent.protocol import get_image_job_service

            service = get_image_job_service()
            cancel_recovered_unavailable = False
            decision_action = action or "status"
            decision = wc._authorize_web_capability(
                "image_jobs",
                decision_action,
                arguments={"action": decision_action, "job_id": safe_job_id},
                metadata={"surface": "web", "source": "image_job_action_api", "user_initiated": True},
            )
            if decision.get("allowed") is not True:
                return json.dumps(
                    wc._permission_denied_payload(
                        decision.get("reason", ""),
                        decision,
                        capability="image_jobs",
                        action=decision_action,
                    ),
                    ensure_ascii=False,
                )
            if action == "cancel":
                reason = str(body.get("reason") or "cancel_requested")
                job = service.cancel(safe_job_id, reason=reason)
                cancel_recovered_unavailable = job.get("status") == "unknown"
            elif action in {"collect", "status", ""}:
                job = service.collect(
                    safe_job_id,
                    wait=bool(body.get("wait")),
                    timeout=float(body.get("timeout") or 0) or None,
                )
            else:
                return json.dumps({"status": "error", "message": "unsupported image job action"}, ensure_ascii=False)
            request_id = wc._safe_image_job_api_identifier(
                body.get("request_id") or body.get("requestId"),
                prefix="req-image-job",
                allow_empty=True,
            )
            if not request_id and job.get("status") == "unknown":
                request_id = wc._image_job_request_id_from_events(safe_job_id)
            include_events = bool(body.get("include_events") or body.get("includeEvents"))
            payload = wc._image_job_projection_payload(job, include_events=include_events, request_id=request_id)
            if cancel_recovered_unavailable and payload.get("job", {}).get("recovered_from_projection"):
                payload["job"]["cancelled"] = False
                payload["job"]["cancel_unavailable_reason"] = "recovered_projection_no_live_worker"
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] image job action error: {web_body_log_summary(exc)}")
            return json.dumps({"status": "error", "message": "image job action failed"}, ensure_ascii=False)
