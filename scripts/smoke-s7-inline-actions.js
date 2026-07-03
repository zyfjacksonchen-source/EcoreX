#!/usr/bin/env node
// Execute the S7 inline-action helpers from console.js against a tiny DOM shim.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const consolePath = path.join(root, 'channel', 'web', 'static', 'js', 'console.js');
const inlineActionsPath = path.join(root, 'channel', 'web', 'static', 'js', 'inline-actions.js');
const source = fs.readFileSync(consolePath, 'utf8');
const inlineActionsSource = fs.readFileSync(inlineActionsPath, 'utf8');
const start = source.indexOf('function runtimeProjectionAssistantMessage');
const end = source.indexOf("\nfetch('/config')", start);
if (start < 0 || end < 0 || end <= start) {
  throw new Error('Unable to extract S7 runtime projection helper block from console.js');
}
const projectionStart = source.indexOf('function runtimeProjectionBotSelector');
const projectionEnd = source.indexOf('\nfunction normalizeRuntimeProjectionHistoryPayload', projectionStart);
if (projectionStart < 0 || projectionEnd < 0 || projectionEnd <= projectionStart) {
  throw new Error('Unable to extract S7 projection render helper block from console.js');
}
const applyStart = source.indexOf('function applyRuntimeProjectionSnapshot');
const applyEnd = source.indexOf('\n    async function refreshRuntimeProjectionSnapshot', applyStart);
if (applyStart < 0 || applyEnd < 0 || applyEnd <= applyStart) {
  throw new Error('Unable to extract applyRuntimeProjectionSnapshot from console.js');
}
const applyBlock = source.slice(applyStart, applyEnd);
if (!applyBlock.includes('syncInlineActionRows(inlineActionPlansFromProjection(projection), stepsEl);')) {
  throw new Error('applyRuntimeProjectionSnapshot must sync inline action rows');
}
if (applyBlock.includes('renderInlineActionRows(inlineActionPlansFromProjection(projection), stepsEl);')) {
  throw new Error('applyRuntimeProjectionSnapshot still uses append-only inline action rendering');
}

class FakeRow {
  constructor(html) {
    this.html = String(html || '');
    this.dataset = {
      inlineActionId: this._attr('data-inline-action-id'),
      inlineActionCommand: this._attr('data-inline-action-command'),
    };
    this.parent = null;
  }

  _attr(name) {
    const match = this.html.match(new RegExp(`${name}="([^"]*)"`));
    return match ? match[1] : '';
  }

  replaceWith(next) {
    if (!this.parent) return;
    const index = this.parent.children.indexOf(this);
    if (index >= 0) {
      next.parent = this.parent;
      this.parent.children[index] = next;
      this.parent = null;
    }
  }

  remove() {
    if (!this.parent) return;
    const index = this.parent.children.indexOf(this);
    if (index >= 0) this.parent.children.splice(index, 1);
    this.parent = null;
  }
}

class FakeWrapper {
  constructor() {
    this.firstElementChild = null;
  }

  set innerHTML(value) {
    this.firstElementChild = new FakeRow(value);
  }
}

class FakeContainer {
  constructor() {
    this.children = [];
  }

  appendChild(node) {
    node.parent = this;
    this.children.push(node);
    return node;
  }

  querySelector(selector) {
    const match = String(selector || '').match(/data-inline-action-id="([^"]+)"/);
    if (!match) return null;
    return this.children.find(row => row.dataset.inlineActionId === match[1]) || null;
  }

  querySelectorAll(selector) {
    if (String(selector || '').includes('data-inline-action-row')) return this.children.slice();
    return [];
  }
}

const context = {
  console,
  currentLang: 'zh',
  window: null,
  localizeCancelMarker: value => value,
  canonicalArtifactDedupeKey: artifact => JSON.stringify(artifact),
  escapeHtml: value => String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;'),
  cssEscape: value => String(value == null ? '' : value).replace(/["\\]/g, '\\$&'),
  document: {
    createElement(tag) {
      if (tag !== 'div') throw new Error(`Unexpected test element: ${tag}`);
      return new FakeWrapper();
    },
  },
};
context.window = context;
vm.createContext(context);
vm.runInContext(inlineActionsSource, context, { filename: 'inline-actions.js' });
vm.runInContext(source.slice(start, end), context, { filename: 'console.js.inline-actions' });
vm.runInContext(source.slice(projectionStart, projectionEnd), context, { filename: 'console.js.projection-actions' });

const permissionPlan = {
  id: 'permission:perm-s7',
  kind: 'permission',
  state: 'waiting_permission',
  nextAction: 'confirm_permission',
  permissionRequestId: 'perm-s7',
  title: 'Permission required',
};
const policyPlan = {
  id: 'capability_policy:office-pdf:install',
  kind: 'capability_policy',
  state: 'blocked',
  nextAction: 'view_capability_policy',
  actionLabel: 'View policy',
  packId: 'office-pdf',
  title: 'Capability blocked',
};

const projectionOnly = {
  request_id: 'req-s7',
  turn_id: 'req-s7',
  state: 'waiting_permission',
  messages: [],
  action_plans: [permissionPlan],
};
const projectionOnlyRenderable = context.runtimeProjectionHasRenderableContent(projectionOnly, null);
const botData = context.runtimeProjectionBotMessageData(projectionOnly, null);
if (!projectionOnlyRenderable) throw new Error('projection-only action plan was not renderable');
if (!botData.action_plans || botData.action_plans.length !== 1) {
  throw new Error('runtimeProjectionBotMessageData did not carry action_plans');
}

const submitPlans = context.inlineActionPlansFromSubmitError({
  message: 'Blocked by permission policy',
  permission: { capability: 'bash', action: 'run' },
});
if (submitPlans[0].nextAction !== 'view_capability_policy') {
  throw new Error(`Unexpected submit permission action: ${submitPlans[0].nextAction}`);
}
const submitRow = context.renderInlineActionRowHtml(submitPlans[0]);
if (!submitRow.includes('data-inline-action-command="view-capability-policy"')) {
  throw new Error('submit permission row did not render a policy button');
}
if (submitRow.includes('open_permissions') || submitRow.includes('open-permissions')) {
  throw new Error('submit permission row still references the removed open_permissions action');
}

const container = new FakeContainer();
context.renderInlineActionRows([permissionPlan, policyPlan], container);
if (container.children.length !== 2) throw new Error('expected two rendered action rows');
context.syncInlineActionRows([policyPlan], container);
const permissionRowRemovedAfterTerminalSync = (
  container.children.length === 1
  && container.children[0].dataset.inlineActionId === 'capability_policy:office-pdf:install'
);
if (!permissionRowRemovedAfterTerminalSync) {
  throw new Error('terminal projection sync did not remove stale permission row');
}

process.stdout.write(JSON.stringify({
  status: 'passed',
  projectionOnlyRenderable,
  botDataActionPlans: botData.action_plans.length,
  submitPermissionAction: submitPlans[0].nextAction,
  permissionRowRemovedAfterTerminalSync,
}, null, 2) + '\n');
