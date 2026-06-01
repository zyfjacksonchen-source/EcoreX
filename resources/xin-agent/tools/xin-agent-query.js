#!/usr/bin/env node

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const COMMANDS = new Set([
  'schema',
  'account list',
  'project list',
  'project detail',
  'report summary',
  'task list',
  'user list',
  'sync state',
  'sync changes'
]);
const PLATFORMS = new Set(['xhs', 'bili']);
const XHS_CHANNELS = new Set(['spotlight', 'chengfeng']);

function output(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function fail(code, message, extra = {}) {
  output({ ok: false, error: { code, message }, ...extra });
  process.exit(0);
}

function parseArgs(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith('--')) continue;
    const key = token.slice(2).replace(/-/g, '_');
    const next = argv[i + 1];
    if (!next || next.startsWith('--')) {
      result[key] = true;
    } else {
      result[key] = next;
      i += 1;
    }
  }
  return result;
}

function intValue(value, fallback) {
  const number = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(number) ? number : fallback;
}

function dateValue(value) {
  const text = String(value || '').trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : '';
}

function dateTimeValue(value) {
  const text = String(value || '').trim().replace('T', ' ');
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return `${text} 00:00:00`;
  return /^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}$/.test(text) ? text : '';
}

function boolValue(value) {
  if (typeof value === 'boolean') return value;
  return ['1', 'true', 'yes', 'y', 'on', 'include'].includes(String(value ?? '').trim().toLowerCase());
}

function idValue(value, label) {
  const text = String(value || '').trim();
  if (!/^\d{1,32}$/.test(text)) fail(`missing-${label}`, `${label} is required.`);
  return text;
}

function normalizeCommand(value) {
  return String(value || '').trim().toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ');
}

function cliArgs(input) {
  const command = normalizeCommand(input.command || input.action || '');
  if (!COMMANDS.has(command)) fail('invalid-command', 'Unsupported command.');
  if (command === 'schema') return ['schema'];
  const limit = Math.max(1, Math.min(500, intValue(input.limit, 500)));
  if (command === 'sync state') return ['sync', 'state'];
  if (command === 'sync changes') {
    const since = dateTimeValue(input.since || input.since_at || input.sinceAt);
    if (!since) fail('missing-since', 'since is required as YYYY-MM-DD HH:mm:ss.');
    return ['sync', 'changes', '--since', since, '--limit', String(limit)];
  }
  if (command === 'project detail') {
    const projectId = idValue(input.project_id || input.projectId, 'project_id');
    return ['project', 'detail', '--project-id', projectId, '--limit', String(limit)];
  }
  if (command === 'task list') {
    const projectId = idValue(input.project_id || input.projectId, 'project_id');
    const args = ['task', 'list', '--project-id', projectId];
    if (boolValue(input.include_archived || input.includeArchived)) args.push('--include-archived');
    args.push('--limit', String(limit));
    return args;
  }
  if (command === 'user list') {
    const args = ['user', 'list'];
    if (boolValue(input.include_resigned || input.includeResigned)) args.push('--include-resigned');
    args.push('--limit', String(limit));
    return args;
  }
  const platform = String(input.platform || 'xhs').trim().toLowerCase();
  if (!PLATFORMS.has(platform)) fail('invalid-platform', 'Unsupported platform.');
  if (command === 'project list' && platform !== 'xhs') fail('invalid-platform', 'project list supports xhs only.');
  const xhsChannel = String(input.xhs_channel || input.channel || 'spotlight').trim().toLowerCase();
  if (platform === 'xhs' && !XHS_CHANNELS.has(xhsChannel)) fail('invalid-channel', 'Unsupported xhs channel.');
  const offset = Math.max(0, intValue(input.offset, 0));
  const args = command.split(' ');
  args.push('--source', 'mpi', '--platform', platform);
  if (platform === 'xhs') args.push('--xhs-channel', xhsChannel);
  const accountId = String(input.account_id || input.accountId || '').trim();
  if (command === 'project list' || command === 'report summary') {
    if (!/^\d{1,32}$/.test(accountId)) fail('missing-account-id', 'account_id is required.');
    args.push('--account-id', accountId);
    const startDate = dateValue(input.start_date || input.startDate);
    const endDate = dateValue(input.end_date || input.endDate);
    if (!startDate || !endDate) fail('missing-date', 'start_date and end_date are required.');
    args.push('--start-date', startDate, '--end-date', endDate);
  }
  args.push('--limit', String(limit), '--offset', String(offset));
  return args;
}

function posixQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function invocation(args) {
  const mode = String(process.env.ECOREX_XIN_AGENT_MODE || '').trim().toLowerCase();
  const directDir = process.env.ECOREX_XIN_AGENT_APP_DIR || '/app';
  const dockerDir = process.env.ECOREX_XIN_AGENT_DOCKER_DIR || '/opt/xhs-report';
  if (mode === 'direct' || (process.platform !== 'win32' && fs.existsSync(path.join(directDir, 'xin_agent_cli.py')))) {
    return { command: process.env.ECOREX_XIN_AGENT_PYTHON || 'python', args: ['xin_agent_cli.py', ...args], cwd: directDir, mode: 'direct' };
  }
  if (mode === 'docker' || (process.platform !== 'win32' && fs.existsSync(dockerDir))) {
    return { command: process.env.ECOREX_XIN_AGENT_DOCKER_COMMAND || 'sudo', args: ['docker', 'compose', 'exec', '-T', 'web', 'python', 'xin_agent_cli.py', ...args], cwd: dockerDir, mode: 'docker' };
  }
  const remote = String(process.env.ECOREX_XIN_AGENT_REMOTE || 'ubuntu@140.143.183.53').trim();
  if (!remote) fail('not-configured', 'Remote xin-agent CLI is not configured.');
  const port = String(process.env.ECOREX_XIN_AGENT_REMOTE_PORT || '22').trim();
  const remoteCwd = String(process.env.ECOREX_XIN_AGENT_REMOTE_CWD || '/opt/xhs-report').trim();
  const remoteCommand = ['cd', posixQuote(remoteCwd), '&&', 'sudo', 'docker', 'compose', 'exec', '-T', 'web', 'python', 'xin_agent_cli.py', ...args.map(posixQuote)].join(' ');
  const sshHome = String(process.env.ECOREX_XIN_AGENT_SSH_HOME || '').trim();
  const sshProgramData = String(process.env.ECOREX_XIN_AGENT_SSH_PROGRAMDATA || process.env.ProgramData || '').trim();
  return {
    command: process.env.ECOREX_XIN_AGENT_SSH_COMMAND || 'ssh',
    args: ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-p', port, remote, remoteCommand],
    cwd: process.cwd(),
    mode: 'ssh',
    envPatch: {
      ...(sshHome ? { HOME: sshHome, USERPROFILE: sshHome } : {}),
      ...(sshProgramData ? { ProgramData: sshProgramData } : {})
    }
  };
}

function parseJson(stdout) {
  const text = String(stdout || '').trim();
  for (const line of text.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)) {
    if (!line.startsWith('{')) continue;
    try {
      return JSON.parse(line);
    } catch {
      // Keep looking.
    }
  }
  return null;
}

function run(command, args, cwd, envPatch = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { cwd, env: { ...process.env, ...envPatch }, shell: false, windowsHide: true });
    let stdout = '';
    let stderr = '';
    const timeout = setTimeout(() => {
      child.kill();
      resolve({ code: -1, stdout, stderr: `${stderr}\nCommand timed out.`.trim() });
    }, Math.max(5000, Math.min(120000, intValue(process.env.ECOREX_XIN_AGENT_TIMEOUT_MS, 60000))));
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.on('error', (error) => {
      clearTimeout(timeout);
      resolve({ code: -1, stdout, stderr: error.message });
    });
    child.on('close', (code) => {
      clearTimeout(timeout);
      resolve({ code, stdout, stderr });
    });
  });
}

(async () => {
  if (process.env.ECOREX_XIN_AGENT_ALLOWED !== '1') {
    fail('forbidden', 'Xin Assistant data is only available to EcoreX administrators.');
  }
  const input = parseArgs(process.argv.slice(2));
  const args = cliArgs(input);
  const target = invocation(args);
  const result = await run(target.command, target.args, target.cwd, target.envPatch || {});
  const parsed = parseJson(result.stdout);
  if (!parsed) {
    fail('invalid-json', 'xin_agent_cli.py did not return JSON.', {
      meta: { mode: target.mode, exitCode: result.code, stderr: String(result.stderr || '').slice(0, 800) }
    });
  }
  output(parsed);
})();
