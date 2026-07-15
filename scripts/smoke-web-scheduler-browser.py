#!/usr/bin/env python3
"""Browser smoke for the Web scheduler projection management surface."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, base_api_stub_script, web_asset_server


def _scheduler_stub_script() -> str:
    extra_fetch_cases = r"""
    if (path === '/api/scheduler') {
      const state = window.__ecorexSmoke.scheduler ||= {
        calls: [],
        alerts: [],
        failNextStart: false,
        projection: {
          enabled: true,
          initialized: true,
          running: false,
          threadAlive: false,
          serviceStatus: 'initialized_stopped',
          blockingReason: 'scheduler service is initialized but not running',
          taskStore: { path: 'C:/CowAgent/scheduler/tasks.json', exists: true },
          tasks: [
            {
              id: 'task-daily',
              name: 'Daily report',
              enabled: true,
              state: 'error',
              schedule: { type: 'cron', expression: '30 9 * * *' },
              scheduleDescription: 'daily at 09:30',
              nextRunAt: '2026-06-26T09:30:00+08:00',
              lastRunAt: '2026-06-25T09:30:00+08:00',
              action: {
                type: 'agent_task',
                taskDescription: 'Generate the morning report',
                receiver: 'private-open-id',
                debugToken: 'sk-test-secret-1234567890'
              },
              lastError: 'scheduler task failed; details redacted (hostilehash)',
              lastErrorHash: 'hostilehash'
            },
            {
              id: 'task-disabled',
              name: 'Disabled weekly send',
              enabled: false,
              state: 'disabled',
              schedule: { type: 'cron', expression: '0 18 * * 5' },
              scheduleDescription: 'weekly Friday at 18:00',
              nextRunAt: '',
              lastRunAt: '',
              action: {
                type: 'send_message',
                content: 'Send weekly summary',
                receiver: 'private-open-id',
                debugToken: 'sk-test-secret-1234567890'
              },
              lastError: ''
            }
          ],
          taskCount: 2,
          counts: { total: 2, enabled: 1, disabled: 1, error: 0 },
          canModify: true,
          modifyBlockingReason: '',
          pollIntervalSeconds: 30
        }
      };

      function cloneProjection() {
        const projection = state.projection;
        projection.taskCount = projection.tasks.length;
        projection.counts = {
          total: projection.tasks.length,
          enabled: projection.tasks.filter((task) => task.enabled !== false).length,
          disabled: projection.tasks.filter((task) => task.enabled === false).length,
          error: projection.tasks.filter((task) => task.lastError).length
        };
        return JSON.parse(JSON.stringify(projection));
      }

      if (init && String(init.method || 'GET').toUpperCase() === 'POST') {
        let body = {};
        try { body = JSON.parse(init.body || '{}'); } catch (_) {}
        state.calls.push(body);
        if (body.action === 'start') {
          state.projection.enabled = true;
          if (state.failNextStart) {
            state.failNextStart = false;
            state.projection.running = false;
            state.projection.threadAlive = false;
            state.projection.serviceStatus = 'enabled_not_initialized';
            state.projection.blockingReason = 'scheduler start failed in browser smoke';
            return makeResponse({ status: 'error', message: 'scheduler start failed', ...cloneProjection() });
          }
          state.projection.running = true;
          state.projection.threadAlive = true;
          state.projection.serviceStatus = 'running';
          state.projection.blockingReason = '';
        } else if (body.action === 'stop') {
          state.projection.enabled = false;
          state.projection.running = false;
          state.projection.threadAlive = false;
          state.projection.serviceStatus = 'disabled';
          state.projection.blockingReason = 'scheduler_enabled is false';
        } else if (body.action === 'enable' || body.action === 'disable') {
          const task = state.projection.tasks.find((item) => item.id === body.task_id);
          if (task) {
            task.enabled = body.action === 'enable';
            task.state = task.enabled ? 'scheduled' : 'disabled';
          }
        } else if (body.action === 'update') {
          const task = state.projection.tasks.find((item) => item.id === body.task_id);
          if (task) {
            if (body.name) task.name = body.name;
            if (body.schedule_type && body.schedule_value) {
              task.schedule = { type: body.schedule_type, expression: body.schedule_value };
              task.scheduleDescription = `${body.schedule_type} ${body.schedule_value}`;
            }
            if (Object.prototype.hasOwnProperty.call(body, 'taskDescription')) {
              task.action.taskDescription = body.taskDescription;
            }
            if (Object.prototype.hasOwnProperty.call(body, 'content')) {
              task.action.content = body.content;
            }
          }
        } else if (body.action === 'delete') {
          state.projection.tasks = state.projection.tasks.filter((item) => item.id !== body.task_id);
        }
      }
      return makeResponse({ status: 'success', ...cloneProjection() });
    }
"""
    return base_api_stub_script(extra_fetch_cases)


def _scheduler_probe_script() -> str:
    return r"""
(async () => {
  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  window.prompt = () => { throw new Error('scheduler smoke should not use prompt editing'); };
  window.alert = (message) => {
    (window.__ecorexSmoke.scheduler.alerts ||= []).push(String(message || ''));
  };
  currentLang = 'en';
  applyI18n();
  navigateTo('tasks');
  loadTasksView(true);

  const wait = (label, predicate, timeout = 5000) => new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolve();
      } catch (_) {}
      if (Date.now() - start > timeout) return reject(new Error(`timeout waiting for scheduler UI: ${label}`));
      setTimeout(tick, 25);
    };
    tick();
  });

  await wait('initial task cards', () => document.querySelectorAll('.scheduler-task-card').length >= 2);

  const dailyCard = document.querySelector('.scheduler-task-card[data-task-id="task-daily"]');
  const disabledCard = document.querySelector('.scheduler-task-card[data-task-id="task-disabled"]');
  assert(dailyCard, 'daily task card missing');
  assert(disabledCard, 'disabled task card missing');
  assert(!document.getElementById('tasks-runtime-status').classList.contains('hidden'), 'runtime status is hidden');
  assert(!document.getElementById('tasks-list').classList.contains('hidden'), 'task list is hidden');
  assert(document.querySelectorAll('.scheduler-task-editor').length === 2, 'task editors missing');
  assert(document.querySelectorAll('[data-scheduler-action="save"]').length === 2, 'save commands missing');
  assert(document.querySelectorAll('[data-scheduler-action="delete"]').length === 2, 'delete commands missing');

  const form = dailyCard.querySelector('.scheduler-task-editor');
  form.elements.name.value = 'Daily report edited';
  form.elements.schedule_type.value = 'cron';
  form.elements.schedule_value.value = '45 8 * * *';
  form.elements.action_content.value = 'Generate edited report';
  dailyCard.querySelector('[data-scheduler-action="save"]').click();
  await wait('save command', () => (window.__ecorexSmoke.scheduler.calls || []).length >= 1);
  await wait('edited projection render', () => document.querySelector('.scheduler-task-card[data-task-id="task-daily"] input[name="name"]').value === 'Daily report edited');

  await wait('disable button enabled', () => {
    const button = document.querySelector('.scheduler-task-card[data-task-id="task-daily"] [data-scheduler-action="disable"]');
    return button && !button.disabled;
  });
  document.querySelector('.scheduler-task-card[data-task-id="task-daily"] [data-scheduler-action="disable"]').click();
  await wait('disable command', () => (window.__ecorexSmoke.scheduler.calls || []).length >= 2);
  await wait('disabled projection render', () => document.querySelector('.scheduler-task-card[data-task-id="task-daily"]').classList.contains('is-disabled'));

  await wait('start button enabled', () => {
    const button = document.querySelector('#tasks-runtime-status [data-scheduler-action="start"]');
    return button && !button.disabled;
  });
  document.querySelector('#tasks-runtime-status [data-scheduler-action="start"]').click();
  await wait('start command', () => (window.__ecorexSmoke.scheduler.calls || []).length >= 3);
  await wait('running runtime projection render', () => document.querySelector('#tasks-runtime-status .scheduler-runtime-title').innerText.includes('running'));

  await wait('stop button enabled', () => {
    const button = document.querySelector('#tasks-runtime-status [data-scheduler-action="stop"]');
    return button && !button.disabled;
  });
  document.querySelector('#tasks-runtime-status [data-scheduler-action="stop"]').click();
  await wait('stop command', () => (window.__ecorexSmoke.scheduler.calls || []).length >= 4);
  await wait('disabled runtime projection render', () => document.querySelector('#tasks-runtime-status .scheduler-runtime-title').innerText.includes('disabled'));

  window.__ecorexSmoke.scheduler.failNextStart = true;
  await wait('failed start button enabled', () => {
    const button = document.querySelector('#tasks-runtime-status [data-scheduler-action="start"]');
    return button && !button.disabled;
  });
  document.querySelector('#tasks-runtime-status [data-scheduler-action="start"]').click();
  await wait('failed start command', () => (window.__ecorexSmoke.scheduler.calls || []).length >= 5);
  await wait('failed start alert', () => (window.__ecorexSmoke.scheduler.alerts || []).length >= 1);
  await wait('failed start projection render', () => document.querySelector('#tasks-runtime-status .scheduler-runtime-title').innerText.includes('enabled_not_initialized'));

  const calls = window.__ecorexSmoke.scheduler.calls || [];
  const allowedKeys = new Set(['action', 'task_id', 'name', 'schedule_type', 'schedule_value', 'taskDescription', 'content']);
  calls.forEach((call) => {
    Object.keys(call).forEach((key) => assert(allowedKeys.has(key), `unexpected scheduler payload key: ${key}`));
  });

  const bodyText = document.body.innerText;
  assert(bodyText.includes('Daily report edited'), 'edited task name is not visible');
  assert(bodyText.includes('Disabled weekly send'), 'disabled task is not visible');
  assert(!bodyText.includes('private-open-id'), 'private receiver leaked into scheduler UI');
  assert(!bodyText.includes('sk-test'), 'secret token leaked into scheduler UI');

  const metrics = {
    runtimeText: document.getElementById('tasks-runtime-status').innerText,
    taskCards: document.querySelectorAll('.scheduler-task-card').length,
    disabledCards: document.querySelectorAll('.scheduler-task-card.is-disabled').length,
    editors: document.querySelectorAll('.scheduler-task-editor').length,
    saveButtons: document.querySelectorAll('[data-scheduler-action="save"]').length,
    deleteButtons: document.querySelectorAll('[data-scheduler-action="delete"]').length,
    calls,
    alerts: window.__ecorexSmoke.scheduler.alerts || [],
    visibleText: document.getElementById('view-tasks').innerText,
    listHidden: document.getElementById('tasks-list').classList.contains('hidden'),
    statusHidden: document.getElementById('tasks-runtime-status').classList.contains('hidden')
  };

  assert(metrics.taskCards === 2, 'expected two visible scheduler tasks');
  assert(metrics.disabledCards >= 2, 'disabled state did not render after disable command');
  assert(metrics.calls[0].action === 'update', 'first scheduler command should be update');
  assert(metrics.calls[0].task_id === 'task-daily', 'update command used wrong task id');
  assert(metrics.calls[0].schedule_value === '45 8 * * *', 'schedule value was not read from editor');
  assert(metrics.calls[0].taskDescription === 'Generate edited report', 'task content was not read from editor');
  assert(metrics.calls[1].action === 'disable', 'second scheduler command should be disable');
  assert(metrics.calls[2].action === 'start', 'third scheduler command should be start');
  assert(metrics.calls[3].action === 'stop', 'fourth scheduler command should be stop');
  assert(metrics.calls[4].action === 'start', 'fifth scheduler command should be failed start');
  assert(metrics.alerts[0] === 'scheduler start failed', 'failed start alert missing');
  assert(metrics.runtimeText.includes('enabled_not_initialized'), 'failed start projection did not render');
  return metrics;
})();
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    started = time.time()
    with web_asset_server() as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(_scheduler_stub_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_function(
                "() => typeof renderSchedulerProjection === 'function' && typeof loadTasksView === 'function'",
                timeout=args.timeout_ms,
            )
            metrics = page.evaluate(_scheduler_probe_script())
            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.locator("#view-tasks").screenshot(path=str(screenshot_target))
                screenshot_path = str(screenshot_target)
            browser.close()
    result = {
        "status": "PASS",
        "url": url,
        "duration_ms": round((time.time() - started) * 1000),
        "screenshot": screenshot_path,
        "metrics": metrics,
        "console_errors": errors,
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL"}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Web scheduler browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the task view.")
    args = parser.parse_args()

    try:
        result = run_smoke(args)
    except Exception as exc:  # pragma: no cover - script-level failure report
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
