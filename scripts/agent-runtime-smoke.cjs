const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const mainPath = path.join(rootDir, 'electron', 'main.cjs');
const packagePath = path.join(rootDir, 'package.json');

const main = fs.readFileSync(mainPath, 'utf8');
const pkg = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
const failures = [];
const passes = [];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function check(name, fn) {
  try {
    fn();
    passes.push(name);
  } catch (error) {
    failures.push({ name, message: error && error.message ? error.message : String(error) });
  }
}

function includesAll(text, values, label) {
  for (const value of values) {
    assert(text.includes(value), `${label} missing ${value}`);
  }
}

function assertMatches(text, pattern, message) {
  assert(pattern.test(text), message);
}

function functionBody(name) {
  const start = main.indexOf(`function ${name}`);
  assert(start >= 0, `function ${name} is missing.`);
  const next = main.indexOf('\nfunction ', start + 1);
  const nextAsync = main.indexOf('\nasync function ', start + 1);
  const candidates = [next, nextAsync].filter((value) => value > start);
  const end = candidates.length ? Math.min(...candidates) : main.length;
  return main.slice(start, end);
}

check('npm script wiring', () => {
  assert(
    pkg.scripts?.['test:agent-runtime'] === 'node scripts/agent-runtime-smoke.cjs',
    'test:agent-runtime must run scripts/agent-runtime-smoke.cjs.'
  );
  for (const scriptName of ['verify:production', 'verify:production:strict', 'verify:production:release']) {
    assert(
      String(pkg.scripts?.[scriptName] || '').includes('npm run test:agent-runtime'),
      `${scriptName} must include npm run test:agent-runtime.`
    );
  }
});

check('session actor boundary symbols', () => {
  includesAll(
    main,
    [
      'const runningAgents = new Map()',
      'const pendingAgentStarts = new Map()',
      'const recentAgentStartsByWindow = new Map()',
      'const agentSessionActors = new Map()',
      "const AGENT_RUNTIME_KIND = 'claude-cli-session-actor'",
      'const MAX_RUNNING_AGENTS =',
      'const AGENT_START_DEBOUNCE_MS =',
      'const AGENT_START_PENDING_TTL_MS =',
      'function createAgentSessionActor',
      'function disposeAgentSessionActor',
      'function claimAgentStart',
      'function releaseAgentStart',
      'function agentStartSignature'
    ],
    'agent session actor boundary'
  );
  const claimBody = functionBody('claimAgentStart');
  includesAll(
    claimBody,
    [
      'runningAgents.has(payload.sessionId',
      'pendingAgentStarts.has(payload.sessionId)',
      'duplicate-session',
      'requestedClaudeSessionId',
      'activeSessionCount >= MAX_RUNNING_AGENTS',
      'recentAgentStartsByWindow.get(ownerId)',
      'duplicate-start',
      'pendingAgentStarts.set(payload.sessionId, lock)',
      'recentAgentStartsByWindow.set(ownerId'
    ],
    'agent start claim'
  );
});

check('permission snapshot is captured at session start', () => {
  const sanitizeBody = functionBody('sanitizePayload');
  includesAll(
    sanitizeBody,
    [
      'sanitizePermissionMode',
      'publicPermissionPolicy(permissionMode, { includeBackend: true })',
      'permissionPolicy.fullAccess',
      'hasFullAccessConfirmation(payload)',
      'accessMode: permissionPolicy.accessMode',
      'permissionMode: permissionPolicy.permissionMode',
      'permissionCliMode: permissionPolicy.cliMode',
      'permissionCliFlags: permissionPolicy.cliFlags',
      'permissionLabel: permissionPolicy.label',
      'permissionPolicy'
    ],
    'sanitized permission snapshot'
  );
  const snapshotBody = functionBody('createAgentPermissionSnapshot');
  includesAll(
    snapshotBody,
    [
      'Object.freeze',
      'permissionCliFlags',
      'plugins',
      'fullAccess: Boolean(permissionPolicy.fullAccess)',
      'cwd: path.resolve',
      'return assertAgentPermissionSnapshotIsolated(snapshot)'
    ],
    'immutable agent permission snapshot'
  );
  const assertBody = functionBody('assertAgentPermissionSnapshotIsolated');
  includesAll(
    assertBody,
    [
      'Object.isFrozen(snapshot)',
      'Object.isFrozen(snapshot.permissionCliFlags)',
      'Object.isFrozen(snapshot.plugins)',
      'FULL_ACCESS_CLAUDE_FLAG',
      'Agent permission snapshot is inconsistent.'
    ],
    'agent permission snapshot invariant'
  );
  const runBody = functionBody('runAgent');
  includesAll(
    runBody,
    [
      'const startClaim = claimAgentStart(safePayload, options)',
      'runtimeActor = createAgentSessionActor(sessionId, safePayload, startLock',
      'const permissionSnapshot = runtimeActor.permissionSnapshot',
      'if (permissionCliMode) args.push',
      'for (const flag of permissionCliFlags || [])',
      'permissionMode',
      'permissionCliMode',
      'permissionCliFlags',
      'permissionLabel',
      'permissionPolicy',
      'recordSessionEvent(entry, startedEvent)',
      'runningAgents.set(sessionId, entry)',
      'releaseAgentStart(sessionId, startLock)'
    ],
    'runtime session permission snapshot'
  );
});

check('transport stop and cleanup path', () => {
  const transportBody = functionBody('createCliAgentTransport');
  includesAll(
    transportBody,
    [
      "kind: 'claude-cli-child'",
      'stopped: false',
      "stop(reason = 'cancelled')",
      'this.stopped = true',
      'killProcessTree(child)'
    ],
    'agent cli transport stop'
  );
  const stopBody = functionBody('stopAgent');
  includesAll(
    stopBody,
    [
      'pendingAgentStarts.get(sessionId)',
      'pending.cancelled = true',
      'pendingAgentStarts.delete(sessionId)',
      'entry.transport.stop(reason)',
      'entry.actor.stop(reason)',
      'finalizeAgentSession(sessionId, entry',
      'Agent session stopped'
    ],
    'agent stop cleanup'
  );
  const finalizeBody = functionBody('finalizeAgentSession');
  includesAll(
    finalizeBody,
    [
      'entry.finished = true',
      'runningAgents.delete(sessionId)',
      'disposeAgentSessionActor(sessionId',
      'clearAgentTimers(entry)',
      'entry.flushBufferedOutput()',
      'writeSessionTranscript(sessionId, entry',
      'emitAgentEvent(event, { immediate: true })'
    ],
    'agent finalization cleanup'
  );
  includesAll(
    main,
    [
      'function substantiveAgentResultText',
      'function agentSessionHasSubstantiveResult',
      'function agentSessionHasUnresolvedAuthorization',
      'function agentSessionHasUnresolvedUserBlocker',
      'function incompleteAgentResultText',
      'const incompleteResult = finalStatus ===',
      'const authorizationIncomplete = finalStatus ===',
      'const unresolvedUserBlocker = finalStatus ===',
      "reason: authorizationIncomplete ? 'authorization-incomplete' : unresolvedUserBlocker ? 'user-action-required' : incompleteResult ? 'incomplete-result' : undefined",
      'entry.hasSubstantiveResult = true'
    ],
    'agent incomplete result guard'
  );
});

check('runtime status and diagnostics surface', () => {
  const runtimeBody = functionBody('runtimeStatusSnapshot');
  includesAll(
    runtimeBody,
    [
      'Array.from(agentSessionActors.values())',
      'runtimeKind: AGENT_RUNTIME_KIND',
      'activeActors: actors.length',
      'preloadStatus: startupPreloadState.status',
      'actors'
    ],
    'agent runtime status snapshot'
  );
  const publicRuntimeBody = functionBody('publicAgentRuntimeStatus');
  includesAll(
    publicRuntimeBody,
    [
      'runtimeKind: snapshot.runtimeKind',
      'activeActors: snapshot.activeActors',
      'preloadStatus: snapshot.preloadStatus',
      'actors: snapshot.actors.map'
    ],
    'public agent runtime status'
  );
  const healthBody = functionBody('collectStartupHealth');
  includesAll(
    healthBody,
    [
      'locateClaude()',
      'collectBackendStatus',
      'collectCapabilities',
      'cli:',
      'nativePackage: nativeClaudePackageName()',
      'capabilities: summarizeCapabilities(capabilities)',
      'agentRuntime: publicAgentRuntimeStatus()',
      'runningSessions: getRunningSessionSummaries()'
    ],
    'startup runtime health'
  );
  const diagnosticsBody = functionBody('collectDiagnostics');
  includesAll(
    diagnosticsBody,
    [
      'agentBridge:',
      'path: claude.path',
      'version: claude.version',
      'nativePackage',
      'runtime: publicAgentRuntimeStatus()',
      'runningSessions: getRunningSessionSummaries()',
      'recentSessionHistory: recentSessionFiles()'
    ],
    'diagnostics runtime summary'
  );
  const diagnosticHealthBody = functionBody('healthSummaryForDiagnostics');
  includesAll(
    diagnosticHealthBody,
    [
      'runtimeEngine:',
      'available: Boolean(health.cli?.available)',
      'nativePackage',
      'sessions:',
      'runningCount: runningSessions.length'
    ],
    'exported diagnostic runtime health summary'
  );
  assertMatches(
    main,
    /handleSafe\('startup:health'[\s\S]*?\{\s*authRequired:\s*true\s*\}/,
    'startup:health must remain authenticated.'
  );
  assertMatches(
    main,
    /handleSafe\('diagnostics:get'[\s\S]*?\{\s*authRequired:\s*true\s*\}/,
    'diagnostics:get must remain authenticated.'
  );
  assertMatches(
    main,
    /handleSafe\('agent:sessions'[\s\S]*?getRunningSessionSummaries\(\)[\s\S]*?\{\s*authRequired:\s*true\s*\}/,
    'agent:sessions must expose runtime summaries behind auth.'
  );
});

check('agent stream backpressure and transcript hygiene', () => {
  includesAll(
    main,
    [
      'const MAX_AGENT_EVENT_QUEUE =',
      'const HARD_MAX_AGENT_EVENT_QUEUE =',
      'const AGENT_EVENT_PAUSE_HIGH_WATER =',
      'function setAgentStreamsPaused',
      'function flushAgentEvents',
      'function normalizeAgentEvent',
      'safeTranscriptTextPreview',
      'safeDiagnosticText',
      'safeSessionSummaryForDiagnostics'
    ],
    'agent event and diagnostic hygiene'
  );
});

check('attachment ingestion, tool ledger and run journal', () => {
  includesAll(
    main,
    [
      'const ATTACHMENT_TEXT_MAX_BYTES = 512 * 1024',
      'const ATTACHMENT_IMAGE_MAX_BYTES = 768 * 1024',
      'const FILE_PREVIEW_IMAGE_MAX_BYTES = 768 * 1024',
      'const RUN_JOURNAL_FILE_NAME =',
      'function resolveAttachmentTarget',
      'function ingestAgentAttachments',
      'function composePromptWithAttachmentContext',
      'const attachmentContext = ingestAgentAttachments(payload, { cwd, projectContext })',
      'function toolLedgerStartEvent',
      'function toolLedgerFinishEvent',
      'function appendRunJournalEntry',
      'function recentUnfinishedRunJournals',
      "handleSafe('attachment:ingest'",
      "handleSafe('file:preview'"
    ],
    'attachment ingestion and durable tool/run records'
  );
  assertMatches(
    main,
    /resolveAttachmentTarget[\s\S]*isRegisteredSelectedAttachment\(target,\s*input\)[\s\S]*pathContainsSymlink\(root\.root,\s*target\)/,
    'attachment paths must be limited to workspace/project or explicit selected-file grants.'
  );
  assertMatches(
    main,
    /previewImageFile[\s\S]*dataUrl[\s\S]*FILE_PREVIEW_IMAGE_MAX_BYTES/,
    'file preview must return bounded image data URLs.'
  );
  assertMatches(
    main,
    /isDocumentMetadataPreviewExtension\(file\.extension\)[\s\S]*previewMetadataOnly\(file,\s*'metadata-only'\)/,
    'file preview must return PDF and Office metadata without executing content.'
  );
  assertMatches(
    main,
    /const finishLedgers = toolResults\.map[\s\S]*ledger:\s*finishLedgers\.length === 1 \? finishLedgers\[0\] : finishLedgers/,
    'tool result events must carry structured ledger completions for every returned tool result.'
  );
});

console.log('\nAgent runtime smoke');
for (const name of passes) console.log(`  ok   ${name}`);

if (failures.length) {
  console.error('\nFailures');
  for (const failure of failures) {
    console.error(`  fail ${failure.name}`);
    console.error(`       ${failure.message.replace(/\n/g, '\n       ')}`);
  }
  process.exit(1);
}

console.log(`\nAll ${passes.length} agent runtime smoke checks passed.`);
