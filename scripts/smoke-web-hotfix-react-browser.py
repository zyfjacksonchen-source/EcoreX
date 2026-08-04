#!/usr/bin/env python3
"""Browser smoke for the v0.2.2 React WebUI hotfix surfaces."""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, static_site_server


DIST_ROOT = ROOT / "desktop" / "dist"
PNG_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _bridge_stub_script() -> str:
    session = {
        "authenticated": True,
        "localFallback": False,
        "authProvider": "web-password",
        "identitySource": "login-email",
        "deviceId": "react-hotfix-smoke-device",
        "expiresAt": "2099-01-01T00:00:00Z",
        "user": {
            "id": "ecorex-password:qa.hotfix@example.com",
            "name": "qa.hotfix",
            "email": "qa.hotfix@example.com",
            "role": "user",
            "status": "active",
        },
        "quota": {"allowed": True},
    }
    return f"""
(() => {{
  const session = {json.dumps(session)};
  window.__hotfixReactApiCalls = [];
  const sessions = [
    {{
      session_id: 'stale-race-session',
      id: 'stale-race-session',
      title: '慢历史会话',
      last_active: Date.now() / 1000 - 3,
      updatedAt: Date.now() - 3000,
      msg_count: 2
    }},
    {{
      session_id: 'general-history-1',
      id: 'general-history-1',
      title: '历史通用产物',
      last_active: Date.now() / 1000,
      updatedAt: Date.now(),
      msg_count: 2
    }},
    {{
      session_id: 'project-history-1',
      id: 'project-history-1',
      title: '旧项目会话',
      last_active: Date.now() / 1000 - 10,
      updatedAt: Date.now() - 10000,
      msg_count: 1,
      projectId: 'project-hotfix',
      projectName: 'Hotfix Project',
      projectPath: 'C:/EcoreX/Hotfix Project',
      memoryPath: 'C:/EcoreX/Hotfix Project/.ecorex/project-memory.md'
    }}
  ];
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const staleRaceHistory = [
    {{ role: 'user', content: '迟到历史请求', seq: 11, created_at: Date.now() / 1000 - 2 }},
    {{
      role: 'assistant',
      content: '迟到污染内容：这段文本不允许进入新会话空态。',
      seq: 12,
      request_id: 'req-stale-race',
      created_at: Date.now() / 1000 - 1
    }}
  ];
  const cowMarkdownLongFixture = [
    '# 标签',
    '',
    '#世界杯 #看球 #宅家看球',
    '',
    '世界杯',
    '',
    '世界杯看球',
    '',
    '看球氛围感',
    '',
    '宅家看球',
    '',
    '> 这是一段引用，用来确认 CowAgent 风格的引用块不会被外层 message-body 段落样式污染。',
    '',
    '- 客厅茶几 + 投影/电视播放足球画面',
    '- 零食饮料 + 小彩旗/球队围巾 + 暖色灯串',
    '- 画面不要太广告，要像朋友聚会现场',
    '',
    '| 项目 | 验收点 |',
    '| --- | --- |',
    '| 标题 | heading marker 渲染成 h1 且不露 raw marker |',
    '| 标签 | #世界杯 #看球 保持普通段落 |',
    '',
    '```ts',
    'const markdownParity = \"CowAgent markdown-it\";',
    'const localSecretPath = \"C:/secret/secret.localpath\";',
    'const remoteSecretUrl = \"https://example.com/secret-resource\";',
    '```',
    '',
    '超长链接 https://example.com/' + 'very-long-path-segment-'.repeat(18),
    '',
    Array.from({{ length: 28 }}, (_, index) => '第 ' + (index + 1) + ' 段：这是一段中文长文排版探针，用来确认长回复收起态是完整 Markdown DOM 后裁剪，而不是先截断 raw Markdown 再渲染。').join('\\n\\n')
  ].join('\\n');
  const artifactHistory = [
    {{ role: 'user', content: '生成两张图片', seq: 1, created_at: Date.now() / 1000 }},
    {{
      role: 'assistant',
      content: cowMarkdownLongFixture + '\\n\\n已生成 2 张图片。',
      seq: 2,
      request_id: 'req-hotfix-artifacts',
      created_at: Date.now() / 1000,
      artifacts: [
        {{ kind: 'image', title: 'image-a.png', path: 'C:/EcoreX/out/image-a.png', previewUrl: '/api/file?path=C%3A%2FEcoreX%2Fout%2Fimage-a.png', status: 'ready' }},
        {{ kind: 'image', title: 'image-a-duplicate.png', url: 'file:///C:/EcoreX/out/image-a.png', status: 'ready' }},
        {{ kind: 'image', title: 'image-b.png', path: 'C:/EcoreX/out/image-b.png', previewUrl: '/api/file?path=C%3A%2FEcoreX%2Fout%2Fimage-b.png', status: 'ready' }},
        {{ kind: 'image', title: 'image-b-duplicate.png', url: 'file:///C:/EcoreX/out/image-b.png', status: 'ready' }}
      ]
    }}
  ];
  const streamFinalText = '# 流式标题\\n\\n' + Array.from({{ length: 90 }}, (_, index) => '- 第 ' + (index + 1) + ' 行内容用于节奏探针').join('\\n');
  const streamChunks = [];
  const streamHistory = [
    {{ role: 'user', content: '流式节奏测试', seq: 31, created_at: Date.now() / 1000 }},
    {{
      role: 'assistant',
      content: streamFinalText,
      seq: 32,
      request_id: 'req-stream-smoothness',
      created_at: Date.now() / 1000,
      steps: [
        {{ type: 'tool', name: 'send', status: 'done', result: 'ok', execution_time: 0.04 }}
      ]
    }}
  ];
  for (let index = 0; index < streamFinalText.length; index += 160) {{
    streamChunks.push(streamFinalText.slice(index, index + 160));
  }}

  class HotfixEventSource {{
    constructor(url) {{
      this.url = url;
      this.readyState = 1;
      this.closed = false;
      setTimeout(() => {{
        if (typeof this.onopen === 'function') this.onopen({{ type: 'open' }});
        if (String(url).includes('req-stream-smoothness')) this.startStream();
      }}, 0);
    }}
    startStream() {{
      let id = 1;
      this.emit({{ type: 'tool_start', request_id: 'req-stream-smoothness', tool: 'send', tool_call_id: 'tool-smoke-send', arguments: {{ smoke: true }} }}, id++);
      setTimeout(() => this.emit({{ type: 'tool_end', request_id: 'req-stream-smoothness', tool: 'send', tool_call_id: 'tool-smoke-send', status: 'done', result: 'ok', execution_time: 0.04 }}, id++), 24);
      streamChunks.forEach((chunk, index) => {{
        setTimeout(() => this.emit({{ type: 'delta', request_id: 'req-stream-smoothness', delta: chunk }}, id++), index * 36 + 48);
      }});
      setTimeout(() => this.emit({{ type: 'done', request_id: 'req-stream-smoothness', final_text: streamFinalText }}, id++), streamChunks.length * 36 + 128);
    }}
    emit(item, id) {{
      if (this.closed || typeof this.onmessage !== 'function') return;
      this.onmessage({{ data: JSON.stringify(item), lastEventId: String(id) }});
    }}
    close() {{
      this.closed = true;
      this.readyState = 2;
    }}
  }}

  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'dark');
  localStorage.setItem('ecorex-release-notes-seen-version', '0.2.2');
  localStorage.setItem('ecorex-projects', JSON.stringify([{{
    id: 'project-hotfix',
    name: 'Hotfix Project',
    path: 'C:/EcoreX/Hotfix Project',
    memoryPath: 'C:/EcoreX/Hotfix Project/.ecorex/project-memory.md',
    updatedAt: new Date().toISOString()
  }}]));
  localStorage.setItem('ecorex-session-projects', JSON.stringify({{ 'project-history-1': 'project-hotfix' }}));
  localStorage.setItem('ecorex-session-project-bindings', JSON.stringify({{
    'project-history-1': {{
      projectId: 'project-hotfix',
      projectName: 'Hotfix Project',
      projectPath: 'C:/EcoreX/Hotfix Project',
      memoryPath: 'C:/EcoreX/Hotfix Project/.ecorex/project-memory.md',
      source: 'runtime'
    }}
  }}));
  window.EventSource = HotfixEventSource;

  window.ecorexDesktop = {{
    platform: 'win32',
    getEnterpriseSession: async () => session,
    enterpriseLogout: async () => ({{ status: 'success' }}),
    getSidecarStatus: async () => ({{
      state: 'running',
      phase: 'ready',
      message: 'hotfix browser smoke runtime',
      webPort: 9899
    }}),
    onSidecarStatus: () => () => undefined,
    setWindowTheme: async () => undefined,
    reportTelemetry: async () => undefined,
    checkEnterpriseQuota: async () => ({{ ok: true, quota: {{ allowed: true }} }}),
    chooseProjectFolder: async () => ({{
      id: 'project-hotfix',
      name: 'Hotfix Project',
      path: 'C:/EcoreX/Hotfix Project',
      memoryPath: 'C:/EcoreX/Hotfix Project/.ecorex/project-memory.md'
    }}),
    apiJson: async (request) => {{
      const path = String(request && request.path || '');
      window.__hotfixReactApiCalls.push(path);
      if (path.startsWith('/api/version')) return {{
        version: '0.2.2',
        releaseNotes: {{ version: '0.2.2', title: 'v0.2.2 hotfix', summary: 'browser smoke' }}
      }};
      if (path.startsWith('/api/sessions')) return {{ status: 'success', sessions, total: sessions.length }};
      if (path.startsWith('/api/runtime-projection') && path.includes('stale-race-session')) {{
        await wait(550);
        return {{
          mode: 'session',
          latest_event_id: 22,
          projection: {{
            session_id: 'stale-race-session',
            messages: staleRaceHistory,
            requests: [],
            history: {{ messages: staleRaceHistory, has_more: false }}
          }}
        }};
      }}
      if (path.startsWith('/api/history')) return {{
        status: 'success',
        messages: path.includes('general-history-1') ? artifactHistory : path.includes('stale-race-session') ? (await wait(550), staleRaceHistory) : path.includes('ecorex-draft-') ? streamHistory : [],
        context_start_seq: 0,
        total: path.includes('general-history-1') ? artifactHistory.length : path.includes('stale-race-session') ? staleRaceHistory.length : path.includes('ecorex-draft-') ? streamHistory.length : 0,
        page: 1,
        page_size: 50,
        has_more: false
      }};
      if (path.startsWith('/api/runtime-projection')) return {{
        mode: 'session',
        latest_event_id: 0,
        projection: {{ session_id: 'general-history-1', messages: [], requests: [], history: {{ messages: [], has_more: false }} }}
      }};
      if (path.startsWith('/api/active-requests')) return {{
        status: 'success',
        requests: [],
        recentTerminalRequests: [],
        runStatusCounts: {{}},
        staleLocks: []
      }};
      if (path.startsWith('/api/scheduler')) return {{
        status: 'success',
        enabled: true,
        initialized: true,
        running: false,
        serviceStatus: 'hotfix-smoke',
        tasks: [],
        taskCount: 0,
        counts: {{ total: 0, enabled: 0, disabled: 0, error: 0 }}
      }};
      if (path.startsWith('/api/tools')) return {{ status: 'success', tools: [] }};
      if (path.startsWith('/api/skills')) return {{ status: 'success', skills: [] }};
      if (path.startsWith('/api/models')) return {{ status: 'success', providers: [{{ id: 'gpt-5.6-luna' }}], capabilities: {{}} }};
      if (path.startsWith('/api/extensions')) return {{ status: 'success', extensions: [], count: 0, summary: {{}} }};
      if (path.startsWith('/api/channels')) return {{ status: 'success', channels: [] }};
      if (path.startsWith('/api/ui-state')) return request.method === 'GET' ? {{ status: 'success', state: null }} : {{ status: 'success' }};
      if (path.startsWith('/api/tool-permissions')) return {{ status: 'success', mode: 'smart-ask', grantsCount: 0, auditPath: '' }};
      if (path.startsWith('/api/capabilities')) return {{ status: 'success', packs: [] }};
      if (path.startsWith('/api/memory')) return {{ status: 'success', files: [] }};
      if (path.startsWith('/api/file-stat')) return {{ status: 'success', exists: true, readable: true }};
      if (path.startsWith('/api/file-json')) return {{ status: 'success', data: {{}} }};
      if (path === '/message') return {{ status: 'success', stream: true, request_id: 'req-stream-smoothness', session_id: request.body && request.body.session_id }};
      return {{ status: 'success' }};
    }}
  }};
}})();
"""


def _probe_script() -> str:
    return r"""
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  await wait(350);
  const bodyText = document.body.innerText;
  assert(bodyText.includes('qa.hotfix@example.com'), 'authenticated email is not visible');
  assert(bodyText.includes('和EcoreX一起开始工作'), 'new-session headline missing');
  assert(bodyText.includes('通用会话'), 'general conversation entry missing');
  assert(bodyText.includes('项目文件夹'), 'project folder entry missing');
  assert(bodyText.includes('v0.2.2'), 'visible version v0.2.2 missing');
  assert(!/local@ecorex\.local|ecorex@ecorex\.local/i.test(bodyText), 'local fallback identity leaked');
  assert(!/Run Center|RUNCENTER/.test(bodyText), 'Run Center leaked in ordinary UI');

  const projectStartTrigger = document.querySelector('.new-session-project-picker .new-session-option');
  assert(projectStartTrigger, 'project start trigger missing');
  projectStartTrigger.click();
  await wait(100);
  const projectStartMenu = document.querySelector('.new-session-project-menu');
  assert(projectStartMenu, 'project start menu did not open');
  const projectStartText = projectStartMenu.innerText || '';
  assert(projectStartMenu.querySelector('.project-start-search input[placeholder="搜索项目"]'), 'project start search missing');
  assert(projectStartText.includes('Hotfix Project'), 'existing project not listed in start menu');
  assert(projectStartText.includes('导入新文件夹'), 'import folder action missing in start menu');
  assert(projectStartText.includes('不使用项目'), 'no-project action missing in start menu');
  document.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: 24, clientY: 24 }));
  await wait(80);
  assert(!document.querySelector('.new-session-project-menu'), 'project start menu did not close on blank click');

  const bodyStyle = getComputedStyle(document.body);

  const sessionList = document.querySelector('.session-list');
  assert(sessionList, 'general session list missing');
  const staleRaceRow = Array.from(document.querySelectorAll('.session-row')).find((item) => item.innerText.includes('慢历史会话'));
  assert(staleRaceRow, 'stale race session row missing');
  (staleRaceRow.querySelector('.session-main') || staleRaceRow).click();
  await wait(40);
  document.querySelector('.sidebar-actions button').click();
  await wait(900);
  const raceText = document.querySelector('.chat-pane').innerText;
  assert(raceText.includes('和EcoreX一起开始工作'), 'fresh session headline missing after stale race');
  assert(!raceText.includes('迟到污染内容') && !raceText.includes('慢历史会话'), 'late history/projection polluted the fresh session');

  const projectMenuButton = document.querySelector('.project-menu-button');
  assert(projectMenuButton, 'project menu button missing');
  projectMenuButton.click();
  await wait(80);
  assert(document.querySelector('.context-menu:not(.chat-file-context-menu)'), 'project menu did not open');
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await wait(80);
  assert(!document.querySelector('.context-menu:not(.chat-file-context-menu)'), 'project menu did not close on Escape');
  projectMenuButton.click();
  await wait(80);
  assert(document.querySelector('.context-menu:not(.chat-file-context-menu)'), 'project menu did not reopen');
  document.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: 32, clientY: 32 }));
  await wait(80);
  assert(!document.querySelector('.context-menu:not(.chat-file-context-menu)'), 'project menu did not close on blank click');
  document.querySelector('.sidebar-actions button').click();
  await wait(160);

  const rowsBefore = sessionList.querySelectorAll('.session-row').length;
  assert(rowsBefore >= 1, 'fixture general session row missing');
  sessionList.querySelector('.sidebar-collapse-button').click();
  await wait(80);
  const collapsed = sessionList.classList.contains('is-collapsed') && sessionList.querySelectorAll('.session-row').length === 0;
  assert(collapsed, 'general session collapse did not hide rows');
  sessionList.querySelector('.sidebar-collapse-button').click();
  await wait(80);

  const row = Array.from(document.querySelectorAll('.session-row')).find((item) => item.innerText.includes('历史通用产物'));
  assert(row, 'history session row missing after expand');
  const rowButton = row.querySelector('.session-main') || row;
  rowButton.click();
  await wait(900);
  const markdownHost = document.querySelector('.message.assistant .markdown-content');
  assert(markdownHost, 'history markdown host missing');
  const historyMarkdownText = markdownHost.innerText || '';
  assert(historyMarkdownText.includes('标签'), 'history markdown heading text missing');
  assert(!historyMarkdownText.includes('# 标签'), 'CowAgent heading marker leaked as visible raw text');
  const tagHeading = Array.from(markdownHost.querySelectorAll('h1')).find((item) => item.textContent.trim() === '标签');
  assert(tagHeading, 'CowAgent h1 for "# 标签" did not render');
  const hashtagParagraph = Array.from(markdownHost.querySelectorAll('p')).find((item) => item.innerText.includes('#世界杯 #看球 #宅家看球'));
  assert(hashtagParagraph, 'CowAgent hashtag line "#世界杯 #看球 #宅家看球" should stay a paragraph');
  assert(!Array.from(markdownHost.querySelectorAll('h1, h2, h3')).some((item) => item.textContent.includes('#世界杯')), 'hashtag line was incorrectly promoted to a heading');
  const tagHeadingFontPx = Number.parseFloat(getComputedStyle(tagHeading).fontSize || '0');
  const hashtagFontPx = Number.parseFloat(getComputedStyle(hashtagParagraph).fontSize || '0');
  assert(tagHeadingFontPx > 16 && tagHeadingFontPx < 24, `history h1 font size should match CowAgent compact scale, got ${tagHeadingFontPx}`);
  assert(hashtagFontPx >= 13 && hashtagFontPx <= 15.5, `hashtag paragraph should stay body-size, got ${hashtagFontPx}`);
  const longPreview = document.querySelector('.long-answer-preview');
  assert(longPreview, 'long answer preview missing for real long markdown fixture');
  const previewStyle = getComputedStyle(longPreview);
  const previewOverflow = previewStyle.overflow;
  const previewMaxHeight = previewStyle.maxHeight;
  assert(previewOverflow === 'hidden', `long answer preview should clip rendered DOM, overflow=${previewOverflow}`);
  assert(Number.parseFloat(previewMaxHeight || '0') > 500, `long answer preview max-height missing or too small: ${previewMaxHeight}`);
  const artifactRows = Array.from(document.querySelectorAll('.artifact-row'));
  const artifactTitles = artifactRows.map((item) => item.innerText);
  assert(artifactRows.length === 2, `artifact dedupe expected 2 rows, got ${artifactRows.length}`);
  artifactRows[0].dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 320, clientY: 260 }));
  await wait(120);
  assert(document.querySelector('.chat-file-context-menu'), 'chat file context menu did not open from artifact row');
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await wait(80);
  assert(!document.querySelector('.chat-file-context-menu'), 'chat file context menu did not close on Escape');
  artifactRows[0].dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 320, clientY: 260 }));
  await wait(120);
  assert(document.querySelector('.chat-file-context-menu'), 'chat file context menu did not reopen');
  document.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: 24, clientY: 24 }));
  await wait(80);
  assert(!document.querySelector('.chat-file-context-menu'), 'chat file context menu did not close on blank click');
  const artifactMenuButton = artifactRows[0].querySelector('[data-artifact-menu-trigger]');
  assert(artifactMenuButton, 'artifact menu trigger missing');
  artifactMenuButton.click();
  await wait(80);
  assert(document.querySelector('.artifact-action-menu-portal'), 'artifact menu did not open');
  document.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: 24, clientY: 24 }));
  await wait(80);
  assert(!document.querySelector('.artifact-action-menu-portal'), 'artifact menu did not close on blank click');
  artifactMenuButton.click();
  await wait(80);
  assert(document.querySelector('.artifact-action-menu-portal'), 'artifact menu did not reopen');
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await wait(80);
  assert(!document.querySelector('.artifact-action-menu-portal'), 'artifact menu did not close on Escape');
  const codeProbe = Array.from(document.querySelectorAll('.markdown-content pre code')).find((item) => item.innerText.includes('markdownParity'));
  assert(codeProbe, 'real message code font probe missing');
  const monoStyle = getComputedStyle(codeProbe);
  const codeFontFamily = monoStyle.fontFamily;
  const codeFontPx = Number.parseFloat(monoStyle.fontSize || '0');
  const codeHasMonoStack = /ui-monospace|SFMono|Consolas|Menlo/i.test(codeFontFamily);
  assert(codeHasMonoStack, `code font stack is not the configured mono stack: ${codeFontFamily}`);
  assert(codeFontPx >= 12 && codeFontPx <= 13.5, `code font size should match CowAgent compact code scale, got ${codeFontPx}`);
  const codeHtml = codeProbe.innerHTML || '';
  assert(codeProbe.innerText.includes('C:/secret/secret.localpath'), 'code fixture local path missing');
  assert(codeProbe.innerText.includes('https://example.com/secret-resource'), 'code fixture remote URL missing');
  assert(!/<a\b/i.test(codeHtml) && !/<img\b/i.test(codeHtml) && !/<video\b/i.test(codeHtml) && !/<audio\b/i.test(codeHtml), 'code block text was incorrectly linkified or converted to media');

  document.querySelector('.sidebar-actions button').click();
  await wait(120);
  const messageList = document.querySelector('.message-list');
  const freshChatText = document.querySelector('.chat-pane').innerText;
  assert(freshChatText.includes('和EcoreX一起开始工作'), 'fresh session headline missing after new chat');
  assert(!freshChatText.includes('image-a.png') && !freshChatText.includes('历史通用产物'), 'fresh session chat pane is contaminated by previous session content');
  const overflowMetricsBeforeSend = {
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    chatPane: document.querySelector('.chat-pane').scrollWidth - document.querySelector('.chat-pane').clientWidth,
    messageList: messageList.scrollWidth - messageList.clientWidth
  };
  assert(overflowMetricsBeforeSend.document <= 1, `document horizontal overflow before send: ${overflowMetricsBeforeSend.document}`);
  assert(overflowMetricsBeforeSend.chatPane <= 1, `chat pane horizontal overflow before send: ${overflowMetricsBeforeSend.chatPane}`);
  assert(overflowMetricsBeforeSend.messageList <= 1, `message list horizontal overflow before send: ${overflowMetricsBeforeSend.messageList}`);
  const streamSamples = [];
  let lastAssistantText = '';
  const observer = new MutationObserver(() => {
    const assistant = Array.from(document.querySelectorAll('.message.assistant .message-body, .message.assistant')).pop();
    const text = assistant ? assistant.innerText : '';
    if (text && text !== lastAssistantText) {
      lastAssistantText = text;
      streamSamples.push({ t: performance.now(), length: text.length, text: text.slice(0, 120) });
    }
  });
  observer.observe(messageList, { subtree: true, childList: true, characterData: true });
  const textarea = document.querySelector('.composer textarea');
  assert(textarea, 'composer textarea missing for stream probe');
  const textareaSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
  textareaSetter.call(textarea, '流式节奏测试');
  textarea.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: '流式节奏测试' }));
  await wait(80);
  const sendButton = document.querySelector('.send-button');
  assert(sendButton, 'send button missing for stream probe');
  assert(!sendButton.disabled, 'send button stayed disabled after composer input');
  sendButton.click();
  await wait(1500);
  observer.disconnect();
  const timingText = document.querySelector('.chat-pane').innerText;
  assert(/已处理\s+\d|已在\s+\d/.test(timingText), 'run elapsed timing is not visible in chat pane: ' + timingText.slice(-800));
  assert(document.querySelector('.agent-process-timing'), 'run elapsed timing is not visible in process summary');
  await wait(700);
  const timingAfterHistoryText = document.querySelector('.chat-pane').innerText;
  assert(/已处理\s+\d|已在\s+\d/.test(timingAfterHistoryText), 'run elapsed timing was lost after history refresh: ' + timingAfterHistoryText.slice(-800));
  const overflowMetricsAfterSend = {
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    chatPane: document.querySelector('.chat-pane').scrollWidth - document.querySelector('.chat-pane').clientWidth,
    messageList: messageList.scrollWidth - messageList.clientWidth
  };
  assert(overflowMetricsAfterSend.document <= 1, `document horizontal overflow after send: ${overflowMetricsAfterSend.document}`);
  assert(overflowMetricsAfterSend.chatPane <= 1, `chat pane horizontal overflow after send: ${overflowMetricsAfterSend.chatPane}`);
  assert(overflowMetricsAfterSend.messageList <= 1, `message list horizontal overflow after send: ${overflowMetricsAfterSend.messageList}`);
  const intervals = streamSamples.slice(1).map((sample, index) => sample.t - streamSamples[index].t);
  const avgInterval = intervals.length ? intervals.reduce((sum, value) => sum + value, 0) / intervals.length : 0;
  const maxInterval = intervals.length ? Math.max(...intervals) : 0;
  assert(streamSamples.length >= 6, `stream painted too few updates: ${streamSamples.length}`);
  assert(avgInterval < 140, `stream average paint interval too high: ${avgInterval}`);
  assert(maxInterval < 420, `stream max paint interval too high: ${maxInterval}`);
  assert(!lastAssistantText.includes('# 流式标题'), 'streaming markdown leaked raw # heading marker');
  const assistantHeading = Array.from(document.querySelectorAll('.message.assistant .markdown-content h1')).find((item) => item.textContent.includes('流式标题'));
  assert(assistantHeading, 'markdown h1 did not render from # heading');
  const headingFontPx = Number.parseFloat(getComputedStyle(assistantHeading).fontSize || '0');
  assert(headingFontPx > 16 && headingFontPx < 24, `markdown h1 font size should match CowAgent compact scale, got ${headingFontPx}`);

  return {
    emailVisible: bodyText.includes('qa.hotfix@example.com'),
    versionVisible: bodyText.includes('v0.2.2'),
    newSessionHeadline: true,
    runCenterHidden: !/Run Center|RUNCENTER/.test(bodyText),
    generalCollapse: { rowsBefore, collapsed },
    artifacts: { count: artifactRows.length, titles: artifactTitles },
    markdownParity: {
      rawHeadingMarkerVisible: historyMarkdownText.includes('# 标签'),
      hashtagParagraph: hashtagParagraph.innerText,
      headingFontPx: tagHeadingFontPx,
      hashtagFontPx,
      previewOverflow,
      previewMaxHeight
    },
    artifactMenuOutsideClick: true,
    artifactMenuEscape: true,
    chatFileMenuOutsideClick: true,
    projectMenuOutsideClick: true,
    lateHistoryRaceSuppressed: true,
    freshSessionIsolation: true,
    overflow: { beforeSend: overflowMetricsBeforeSend, afterSend: overflowMetricsAfterSend },
    projectStartMenu: true,
    runTimingVisible: /已处理\s+\d|已在\s+\d/.test(timingText),
    runTimingInProcessSummary: Boolean(document.querySelector('.agent-process-timing')),
    runTimingAfterHistoryRefresh: /已处理\s+\d|已在\s+\d/.test(timingAfterHistoryText),
    streaming: {
      samples: streamSamples.length,
      avgIntervalMs: Math.round(avgInterval * 10) / 10,
      maxIntervalMs: Math.round(maxInterval * 10) / 10,
      finalLength: lastAssistantText.length,
      rawHeadingVisible: lastAssistantText.includes('# 流式标题'),
      headingFontPx
    },
    apiCalls: (window.__hotfixReactApiCalls || []).slice(-20),
    fonts: {
      body: bodyStyle.fontFamily,
      code: codeFontFamily,
      codeFontPx,
      bodyHasSystemStack: /-apple-system|BlinkMacSystemFont|Segoe UI/i.test(bodyStyle.fontFamily),
      codeHasMonoStack
    }
  };
})()
"""


def _long_markdown_visual_probe_script() -> str:
    return r"""
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  await wait(350);
  const row = Array.from(document.querySelectorAll('.session-row')).find((item) => item.innerText.includes('历史通用产物'));
  assert(row, 'history session row missing for long markdown screenshot');
  (row.querySelector('.session-main') || row).click();
  await wait(900);
  const messageBody = document.querySelector('.message.assistant .message-body');
  const markdownHost = document.querySelector('.message.assistant .long-answer-preview .markdown-content');
  assert(messageBody && markdownHost, 'long markdown preview host missing');
  const text = markdownHost.innerText || '';
  assert(!text.includes('# 标签'), 'long markdown screenshot still shows raw "# 标签"');
  const tagHeading = Array.from(markdownHost.querySelectorAll('h1')).find((item) => item.textContent.trim() === '标签');
  const hashtagParagraph = Array.from(markdownHost.querySelectorAll('p')).find((item) => item.innerText.includes('#世界杯 #看球 #宅家看球'));
  assert(tagHeading, 'long markdown screenshot h1 missing');
  assert(hashtagParagraph, 'long markdown screenshot hashtag paragraph missing');
  const longPreview = document.querySelector('.long-answer-preview');
  const previewStyle = getComputedStyle(longPreview);
  const overflow = {
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    chatPane: document.querySelector('.chat-pane').scrollWidth - document.querySelector('.chat-pane').clientWidth,
    messageList: document.querySelector('.message-list').scrollWidth - document.querySelector('.message-list').clientWidth
  };
  assert(overflow.document <= 1, `long markdown screenshot document overflow: ${overflow.document}`);
  assert(overflow.chatPane <= 1, `long markdown screenshot chat pane overflow: ${overflow.chatPane}`);
  assert(overflow.messageList <= 1, `long markdown screenshot message list overflow: ${overflow.messageList}`);
  messageBody.setAttribute('data-smoke-long-markdown-host', '1');
  return {
    headingFontPx: Number.parseFloat(getComputedStyle(tagHeading).fontSize || '0'),
    hashtagFontPx: Number.parseFloat(getComputedStyle(hashtagParagraph).fontSize || '0'),
    previewOverflow: previewStyle.overflow,
    previewMaxHeight: previewStyle.maxHeight,
    overflow
  };
})()
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    errors: list[str] = []
    if not (DIST_ROOT / "index.html").is_file():
        raise FileNotFoundError("desktop/dist/index.html missing; run npm --prefix desktop run build:renderer first")
    with static_site_server(DIST_ROOT) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(_bridge_stub_script())
            page.route(
                "http://127.0.0.1:9899/api/file**",
                lambda route: route.fulfill(status=200, content_type="image/png", body=PNG_PIXEL),
            )
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type == "error" else None)
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_selector(".app-shell", timeout=args.timeout_ms)
            screenshot_path = ""
            long_visual_metrics: dict[str, Any] = {}
            if args.screenshot:
                long_visual_metrics = page.evaluate(_long_markdown_visual_probe_script())
                target = Path(args.screenshot)
                if not target.is_absolute():
                    target = ROOT / target
                target.parent.mkdir(parents=True, exist_ok=True)
                page.locator('[data-smoke-long-markdown-host="1"]').screenshot(path=str(target))
                screenshot_path = str(target)
                page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                page.wait_for_selector(".app-shell", timeout=args.timeout_ms)
            metrics = page.evaluate(_probe_script())
            if long_visual_metrics:
                metrics["longMarkdownVisual"] = long_visual_metrics
            if args.width > 720:
                page.set_viewport_size({"width": 390, "height": 760})
                page.locator(".sidebar-actions button").first.click(timeout=5000)
                page.wait_for_timeout(300)
                metrics["narrowViewport"] = page.evaluate(
                    """() => {
                      const assert = (condition, message) => { if (!condition) throw new Error(message); };
                      const chatPane = document.querySelector('.chat-pane');
                      const messageList = document.querySelector('.message-list');
                      const actions = document.querySelector('.new-session-actions');
                      const buttons = Array.from(document.querySelectorAll('.new-session-option'));
                      assert(chatPane && messageList && actions && buttons.length >= 2, 'narrow new-session controls missing');
                      const overflow = {
                        document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                        chatPane: chatPane.scrollWidth - chatPane.clientWidth,
                        messageList: messageList.scrollWidth - messageList.clientWidth
                      };
                      assert(overflow.document <= 1, `narrow document horizontal overflow: ${overflow.document}`);
                      assert(overflow.chatPane <= 1, `narrow chat pane horizontal overflow: ${overflow.chatPane}`);
                      assert(overflow.messageList <= 1, `narrow message list horizontal overflow: ${overflow.messageList}`);
                      const first = buttons[0].getBoundingClientRect();
                      const second = buttons[1].getBoundingClientRect();
                      const stacked = second.top > first.bottom - 1;
                      assert(stacked, 'general/project choices did not stack on narrow viewport');
                      return {
                        width: window.innerWidth,
                        height: window.innerHeight,
                        overflow,
                        choicesStacked: stacked,
                        headlineVisible: (document.body.innerText || '').includes('和EcoreX一起开始工作')
                      };
                    }"""
                )
                if args.screenshot:
                    narrow_target = Path(args.screenshot)
                    if not narrow_target.is_absolute():
                        narrow_target = ROOT / narrow_target
                    narrow_target = narrow_target.with_name(f"{narrow_target.stem}-narrow{narrow_target.suffix}")
                    page.screenshot(path=str(narrow_target), full_page=True)
                    metrics["narrowViewport"]["screenshot"] = str(narrow_target)
            browser.close()
    result = {
        "status": "PASS" if not errors else "FAIL",
        "duration_ms": round((time.time() - started) * 1000),
        "metrics": metrics,
        "console_errors": errors,
        "screenshot": screenshot_path,
    }
    if errors:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.2.2 React WebUI hotfix browser smoke.")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=920)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--artifact", default=str(ROOT / "docs" / "v0.2.2" / "artifacts" / "r22-13-react-browser-smoke.json"))
    parser.add_argument("--screenshot", default=str(ROOT / "docs" / "v0.2.2" / "artifacts" / "r22-13-react-browser-smoke.png"))
    args = parser.parse_args()
    result = run_smoke(args)
    if args.artifact:
        target = Path(args.artifact)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
