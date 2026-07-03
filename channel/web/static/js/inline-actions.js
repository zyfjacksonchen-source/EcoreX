(function (root) {
    'use strict';

    function chooseLang(opts) {
        opts = opts || {};
        if (opts.lang) return String(opts.lang);
        try {
            if (root.__cowResolveLang__ && typeof root.__cowResolveLang__ === 'function') {
                return String(root.__cowResolveLang__() || 'zh');
            }
        } catch (err) {}
        return 'zh';
    }

    function localActionText(zh, en, opts) {
        return chooseLang(opts) === 'zh' ? zh : en;
    }

    function escapeHtmlLocal(value) {
        const text = String(value == null ? '' : value);
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function cssEscapeLocal(value) {
        if (root.CSS && typeof root.CSS.escape === 'function') {
            return root.CSS.escape(String(value));
        }
        return String(value == null ? '' : value).replace(/["\\]/g, '\\$&');
    }

    function deps(opts) {
        opts = opts || {};
        return {
            document: opts.document || root.document,
            escapeHtml: typeof opts.escapeHtml === 'function' ? opts.escapeHtml : escapeHtmlLocal,
            cssEscape: typeof opts.cssEscape === 'function' ? opts.cssEscape : cssEscapeLocal,
        };
    }

    function normalizeInlineActionPlan(input, opts) {
        opts = opts || {};
        const tr = (zh, en) => localActionText(zh, en, opts);
        const plan = (input && typeof input === 'object') ? input : {};
        const permission = (plan.permission && typeof plan.permission === 'object') ? plan.permission : null;
        const rawType = String(plan.type || '');
        const livePermissionRequest = rawType === 'tool_permission_request' || !!(plan.permissionRequestId || plan.permission_request_id);
        const nextAction = String(
            plan.nextAction ||
            plan.next_action ||
            (permission ? 'view_capability_policy' : '') ||
            (livePermissionRequest ? 'confirm_permission' : '') ||
            ''
        );
        const kind = String(
            plan.kind ||
            (rawType === 'tool_permission_request' ? 'permission' : rawType) ||
            (permission ? 'permission' : '') ||
            (livePermissionRequest ? 'permission' : '') ||
            (nextAction.indexOf('provider') >= 0 ? 'model_config' : '') ||
            (nextAction.indexOf('repair') >= 0 ? 'repair' : '') ||
            ''
        );
        const state = String(
            plan.state ||
            plan.status ||
            (permission ? 'permission_denied' : '') ||
            (livePermissionRequest ? 'waiting_permission' : '') ||
            ''
        );
        const id = String(
            plan.id ||
            plan.actionPlanId ||
            plan.permissionRequestId ||
            plan.permission_request_id ||
            (permission && (permission.request_id || permission.permission_request_id)) ||
            `${kind || 'action'}:${nextAction || state || 'unknown'}`
        ).slice(0, 160);
        const capability = String(plan.capability || (permission && permission.capability) || '');
        const action = String(plan.action || plan.requestedAction || (permission && permission.action) || '');
        const title = String(
            plan.title ||
            plan.diagnosticSummary ||
            plan.diagnostic_summary ||
            (livePermissionRequest ? tr('需要权限确认', 'Permission required') : '') ||
            (permission ? tr('需要权限确认', 'Permission required') : '') ||
            (nextAction === 'configure_model_provider' ? tr('需要配置模型凭据', 'Model credentials required') : '') ||
            (kind === 'repair' ? tr('需要修复依赖', 'Runtime repair required') : '') ||
            tr('需要处理', 'Action required')
        );
        const message = String(
            plan.message ||
            plan.reason ||
            plan.diagnosticSummary ||
            plan.diagnostic_summary ||
            (permission && permission.reason) ||
            ''
        );
        const actionLabel = String(
            plan.actionLabel ||
            plan.action_label ||
            (nextAction === 'confirm_permission' ? tr('处理权限', 'Review') : '') ||
            (nextAction === 'configure_model_provider' ? tr('去配置模型', 'Configure models') : '') ||
            (nextAction === 'connector_login' ? tr('去连接', 'Connect') : '') ||
            (nextAction.indexOf('repair') >= 0 ? tr('查看修复', 'View repair') : '') ||
            (permission ? tr('查看权限策略', 'View policy') : '') ||
            ''
        );
        const permissionRequestId = String(
            plan.permissionRequestId ||
            plan.permission_request_id ||
            (permission && (permission.request_id || permission.permission_request_id)) ||
            ''
        );
        const packId = String(plan.packId || plan.pack_id || '');
        const logRef = String(plan.logRef || plan.log_ref || plan.logPath || plan.log_path || '');
        return {
            id,
            kind,
            state,
            nextAction,
            actionLabel,
            title,
            message,
            capability,
            action,
            permissionRequestId,
            packId,
            logRef,
            retryable: plan.retryable !== false,
            source: opts.source || plan.source || '',
        };
    }

    function inlineActionTone(plan) {
        const kind = String((plan && plan.kind) || '');
        const state = String((plan && plan.state) || '').toLowerCase();
        if (kind === 'permission' || state.indexOf('permission') >= 0 || state === 'blocked') return 'warn';
        if (kind === 'model_config' || String(plan.nextAction || '') === 'configure_model_provider') return 'info';
        if (kind === 'repair' || String(plan.nextAction || '').indexOf('repair') >= 0) return 'repair';
        if (kind === 'connector' || String(plan.nextAction || '').indexOf('connector') >= 0) return 'connector';
        return 'info';
    }

    function inlineActionIcon(plan) {
        const tone = inlineActionTone(plan);
        if (tone === 'warn') return 'fa-shield-halved';
        if (tone === 'repair') return 'fa-screwdriver-wrench';
        if (tone === 'connector') return 'fa-plug-circle-bolt';
        return 'fa-circle-info';
    }

    function renderInlineActionRowHtml(rawPlan, opts) {
        opts = opts || {};
        const d = deps(opts);
        const tr = (zh, en) => localActionText(zh, en, opts);
        const plan = normalizeInlineActionPlan(rawPlan, opts);
        const tone = inlineActionTone(plan);
        const details = [
            plan.capability ? `${tr('能力', 'Capability')}: ${plan.capability}` : '',
            plan.action ? `${tr('动作', 'Action')}: ${plan.action}` : '',
            plan.packId ? `${tr('包', 'Pack')}: ${plan.packId}` : '',
            plan.logRef ? `${tr('日志', 'Log')}: ${plan.logRef}` : '',
        ].filter(Boolean).join(' · ');
        const buttons = [];
        if (plan.nextAction === 'confirm_permission' && plan.permissionRequestId) {
            buttons.push(`<button type="button" class="inline-action-btn is-primary" data-inline-action-command="permission-allow" data-permission-request-id="${d.escapeHtml(plan.permissionRequestId)}">${tr('允许一次', 'Allow once')}</button>`);
            buttons.push(`<button type="button" class="inline-action-btn" data-inline-action-command="permission-deny" data-permission-request-id="${d.escapeHtml(plan.permissionRequestId)}">${tr('拒绝', 'Deny')}</button>`);
        } else if (plan.nextAction === 'configure_model_provider') {
            buttons.push(`<button type="button" class="inline-action-btn is-primary" data-inline-action-command="open-models">${d.escapeHtml(plan.actionLabel || tr('去配置模型', 'Configure models'))}</button>`);
        } else if (plan.nextAction === 'connector_login') {
            buttons.push(`<button type="button" class="inline-action-btn is-primary" data-inline-action-command="open-channels">${d.escapeHtml(plan.actionLabel || tr('去连接', 'Connect'))}</button>`);
        } else if (plan.nextAction === 'view_capability_policy') {
            buttons.push(`<button type="button" class="inline-action-btn" data-inline-action-command="view-capability-policy" data-pack-id="${d.escapeHtml(plan.packId)}">${d.escapeHtml(plan.actionLabel || tr('查看权限策略', 'View policy'))}</button>`);
        } else if (plan.nextAction && plan.nextAction !== 'none') {
            buttons.push(`<button type="button" class="inline-action-btn" data-inline-action-command="inspect-capability" data-pack-id="${d.escapeHtml(plan.packId)}">${d.escapeHtml(plan.actionLabel || tr('查看', 'Inspect'))}</button>`);
        }
        return `
<div class="agent-step inline-action-row is-${d.escapeHtml(tone)}" data-inline-action-row="1" data-inline-action-id="${d.escapeHtml(plan.id)}" data-inline-action-kind="${d.escapeHtml(plan.kind)}" data-inline-action-next="${d.escapeHtml(plan.nextAction)}">
    <div class="inline-action-icon"><i class="fas ${inlineActionIcon(plan)}"></i></div>
    <div class="inline-action-body">
        <div class="inline-action-title">${d.escapeHtml(plan.title)}</div>
        ${plan.message ? `<div class="inline-action-message">${d.escapeHtml(plan.message)}</div>` : ''}
        ${details ? `<div class="inline-action-meta">${d.escapeHtml(details)}</div>` : ''}
    </div>
    ${buttons.length ? `<div class="inline-action-actions">${buttons.join('')}</div>` : ''}
</div>`;
    }

    function inlineActionPlansFromProjection(projection, opts) {
        if (!projection || typeof projection !== 'object') return [];
        const plans = [];
        if (Array.isArray(projection.action_plans)) {
            projection.action_plans.forEach(plan => plans.push(normalizeInlineActionPlan(plan, { ...opts, source: 'runtime_projection' })));
        }
        const visual = projection.visualWorkflow || projection.visual_workflow;
        if (visual && typeof visual === 'object') {
            ['ocr', 'vision', 'imagegen'].forEach(key => {
                const item = visual[key];
                if (item && item.nextAction && item.nextAction !== 'none' && item.state !== 'ready') {
                    plans.push(normalizeInlineActionPlan({
                        ...item,
                        kind: key === 'ocr' ? 'repair' : 'model_config',
                        id: `visual:${key}:${item.nextAction}`,
                        title: item.diagnosticSummary || item.actionLabel || key,
                    }, { ...opts, source: 'visual_workflow' }));
                }
            });
        }
        return plans;
    }

    function inlineActionPlansFromSubmitError(payload, opts) {
        opts = opts || {};
        const tr = (zh, en) => localActionText(zh, en, opts);
        const data = (payload && typeof payload === 'object') ? payload : {};
        if (data.inlineActionPlan) return [normalizeInlineActionPlan(data.inlineActionPlan, { ...opts, source: 'submit_error' })];
        if (Array.isArray(data.actionPlans)) return data.actionPlans.map(plan => normalizeInlineActionPlan(plan, { ...opts, source: 'submit_error' }));
        if (data.permission && typeof data.permission === 'object') {
            return [normalizeInlineActionPlan({
                kind: 'permission',
                state: 'permission_denied',
                nextAction: 'view_capability_policy',
                actionLabel: tr('查看权限策略', 'View policy'),
                title: tr('当前权限阻止了这个操作', 'Permission blocked this action'),
                message: data.message || data.permission.reason || '',
                capability: data.permission.capability || '',
                action: data.permission.action || '',
                permission: data.permission,
            }, { ...opts, source: 'submit_error' })];
        }
        const code = String(data.code || data.error_type || data.errorType || '').toLowerCase();
        if (code === 'needs_provider_credentials' || code.indexOf('credential') >= 0) {
            return [normalizeInlineActionPlan({
                kind: 'model_config',
                state: 'needs_provider_credentials',
                nextAction: 'configure_model_provider',
                title: tr('缺少模型凭据', 'Model credentials required'),
                message: data.message || '',
            }, { ...opts, source: 'submit_error' })];
        }
        return [];
    }

    function renderInlineActionRows(plans, container, opts) {
        if (!container || !Array.isArray(plans) || plans.length === 0) return false;
        const d = deps(opts);
        const normalized = plans.map(plan => normalizeInlineActionPlan(plan, opts)).filter(plan => plan.id);
        if (!normalized.length) return false;
        normalized.forEach(plan => {
            const selector = `[data-inline-action-row="1"][data-inline-action-id="${d.cssEscape(plan.id)}"]`;
            const existing = container.querySelector(selector);
            const wrapper = d.document.createElement('div');
            wrapper.innerHTML = renderInlineActionRowHtml(plan, opts).trim();
            const next = wrapper.firstElementChild;
            if (!next) return;
            if (existing) existing.replaceWith(next);
            else container.appendChild(next);
        });
        return true;
    }

    function syncInlineActionRows(plans, container, opts) {
        if (!container) return false;
        const normalized = Array.isArray(plans)
            ? plans.map(plan => normalizeInlineActionPlan(plan, opts)).filter(plan => plan.id)
            : [];
        const expected = new Set(normalized.map(plan => plan.id));
        let changed = false;
        container.querySelectorAll('[data-inline-action-row="1"]').forEach(row => {
            const id = row.dataset.inlineActionId || '';
            if (!expected.has(id)) {
                row.remove();
                changed = true;
            }
        });
        return renderInlineActionRows(normalized, container, opts) || changed;
    }

    root.EcoreXInlineActions = {
        localActionText,
        normalizeInlineActionPlan,
        inlineActionTone,
        inlineActionIcon,
        renderInlineActionRowHtml,
        inlineActionPlansFromProjection,
        inlineActionPlansFromSubmitError,
        renderInlineActionRows,
        syncInlineActionRows,
    };
})(window);
