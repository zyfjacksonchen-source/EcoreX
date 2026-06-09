import { afterEach, beforeEach, describe, expect, it } from 'bun:test'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

import { handleEnterpriseApi } from '../api/enterprise.js'
import { handleProvidersApi } from '../api/providers.js'
import { handleSettingsApi } from '../api/settings.js'
import {
  EnterpriseService,
  ENTERPRISE_ADMIN_EMAIL,
  ENTERPRISE_DEFAULT_PASSWORD,
} from '../services/enterpriseService.js'

const ORIGINAL_CLAUDE_CONFIG_DIR = process.env.CLAUDE_CONFIG_DIR

type RequestOptions = {
  method?: string
  token?: string
  body?: unknown
}

let tempConfigDir = ''

beforeEach(async () => {
  tempConfigDir = await mkdtemp(join(tmpdir(), 'ecorex-enterprise-test-'))
  process.env.CLAUDE_CONFIG_DIR = tempConfigDir
})

afterEach(async () => {
  if (ORIGINAL_CLAUDE_CONFIG_DIR === undefined) {
    delete process.env.CLAUDE_CONFIG_DIR
  } else {
    process.env.CLAUDE_CONFIG_DIR = ORIGINAL_CLAUDE_CONFIG_DIR
  }
  await rm(tempConfigDir, { recursive: true, force: true })
})

describe('enterprise bootstrap and auth', () => {
  it('bootstraps the default admin, stores only secret hashes, and requires a password change', async () => {
    const bootstrap = await enterprise('/api/enterprise/auth/bootstrap')
    expect(bootstrap.status).toBe(200)
    const bootstrapBody = await bootstrap.json()
    expect(bootstrapBody).toEqual({
      initialized: true,
      defaultAdminEmail: ENTERPRISE_ADMIN_EMAIL,
      mustChangePassword: true,
      usersCount: 1,
    })

    const login = await loginDefaultAdmin()
    expect(login.user.email).toBe(ENTERPRISE_ADMIN_EMAIL)
    expect(login.user.role).toBe('admin')
    expect(login.user.mustChangePassword).toBe(true)
    expect(login.token).toStartWith('ecx_')

    const rawState = await readFile(enterpriseStatePath(), 'utf8')
    expect(rawState).not.toContain(ENTERPRISE_DEFAULT_PASSWORD)
    expect(rawState).not.toContain(login.token)
    expect(rawState).toContain('passwordHash')
    expect(rawState).toContain('tokenHash')
  })

  it('supports forced password change, current user restore, and logout invalidation', async () => {
    const login = await loginDefaultAdmin()
    const newPassword = 'EcoreX@2026!AdminChanged'

    const change = await enterprise('/api/enterprise/auth/password', {
      method: 'PUT',
      token: login.token,
      body: {
        currentPassword: ENTERPRISE_DEFAULT_PASSWORD,
        newPassword,
      },
    })
    expect(change.status).toBe(200)
    expect((await change.json()).user.mustChangePassword).toBe(false)

    const changedLogin = await loginAdmin(newPassword)
    const me = await enterprise('/api/enterprise/auth/me', {
      token: changedLogin.token,
    })
    expect(me.status).toBe(200)
    expect((await me.json()).user.email).toBe(ENTERPRISE_ADMIN_EMAIL)

    const logout = await enterprise('/api/enterprise/auth/logout', {
      method: 'POST',
      token: changedLogin.token,
    })
    expect(logout.status).toBe(200)

    const afterLogout = await enterprise('/api/enterprise/auth/me', {
      token: changedLogin.token,
    })
    expect(afterLogout.status).toBe(401)
  })
})

describe('enterprise admin user management', () => {
  it('lets admins create, update, disable, and reset member accounts with audit history', async () => {
    const admin = await readyAdmin()
    const memberPassword = 'EcoreX@2026!MemberOne'

    const created = await enterprise('/api/enterprise/users', {
      method: 'POST',
      token: admin.token,
      body: {
        email: 'member@example.com',
        displayName: 'Media Buyer',
        password: memberPassword,
        dailyTokenLimit: 1000,
      },
    })
    expect(created.status).toBe(201)
    const createdBody = await created.json()
    expect(createdBody.temporaryPassword).toBe(memberPassword)
    expect(createdBody.user).toMatchObject({
      email: 'member@example.com',
      displayName: 'Media Buyer',
      role: 'member',
      status: 'active',
      dailyTokenLimit: 1000,
      mustChangePassword: true,
    })

    const userId = createdBody.user.id
    const updated = await enterprise(`/api/enterprise/users/${userId}`, {
      method: 'PUT',
      token: admin.token,
      body: {
        displayName: 'Planner',
        role: 'admin',
        dailyTokenLimit: null,
      },
    })
    expect(updated.status).toBe(200)
    expect((await updated.json()).user).toMatchObject({
      displayName: 'Planner',
      role: 'admin',
      dailyTokenLimit: null,
    })

    const reset = await enterprise(`/api/enterprise/users/${userId}/reset-password`, {
      method: 'POST',
      token: admin.token,
      body: { password: 'EcoreX@2026!ResetOne' },
    })
    expect(reset.status).toBe(200)
    expect((await reset.json()).user.mustChangePassword).toBe(true)

    const disabled = await enterprise(`/api/enterprise/users/${userId}`, {
      method: 'PUT',
      token: admin.token,
      body: { role: 'member', status: 'disabled' },
    })
    expect(disabled.status).toBe(200)
    expect((await disabled.json()).user.status).toBe('disabled')

    const blockedLogin = await enterprise('/api/enterprise/auth/login', {
      method: 'POST',
      body: {
        email: 'member@example.com',
        password: 'EcoreX@2026!ResetOne',
      },
    })
    expect(blockedLogin.status).toBe(403)

    const audit = await enterprise('/api/enterprise/audit-log?limit=20', {
      token: admin.token,
    })
    expect(audit.status).toBe(200)
    const auditTypes = (await audit.json()).events.map((event: { type: string }) => event.type)
    expect(auditTypes).toContain('user.created')
    expect(auditTypes).toContain('user.updated')
    expect(auditTypes).toContain('auth.password_reset')
  })
})

describe('enterprise provider governance', () => {
  it('allows only admins to manage provider/API key settings and never returns raw API keys', async () => {
    const admin = await readyAdmin()
    const member = await createAndLoginMember(admin.token)
    const apiKey = 'sk-ecorex-enterprise-secret-1234'

    const memberProviderWrite = await enterprise('/api/enterprise/provider', {
      method: 'PUT',
      token: member.token,
      body: providerBody(apiKey),
    })
    expect(memberProviderWrite.status).toBe(403)

    const legacyProviderWrite = await providers('/api/providers', {
      method: 'POST',
      token: member.token,
      body: providerBody(apiKey),
    })
    expect(legacyProviderWrite.status).toBe(403)

    const sensitiveSettingsWrite = await settings('/api/settings/user', {
      method: 'PUT',
      token: member.token,
      body: {
        env: {
          ANTHROPIC_AUTH_TOKEN: apiKey,
        },
      },
    })
    expect(sensitiveSettingsWrite.status).toBe(403)

    const safeSettingsWrite = await settings('/api/settings/user', {
      method: 'PUT',
      token: member.token,
      body: { theme: 'dark' },
    })
    expect(safeSettingsWrite.status).toBe(200)

    const adminProviderWrite = await enterprise('/api/enterprise/provider', {
      method: 'PUT',
      token: admin.token,
      body: providerBody(apiKey),
    })
    expect(adminProviderWrite.status).toBe(201)
    const providerResponse = await adminProviderWrite.json()
    expect(JSON.stringify(providerResponse)).not.toContain(apiKey)
    expect(providerResponse.provider.hasApiKey).toBe(true)
    expect(providerResponse.provider.apiKeyPreview).toBe('sk-e...1234')

    const providerState = await readFile(join(tempConfigDir, 'cc-haha', 'providers.json'), 'utf8')
    expect(providerState).toContain(apiKey)

    const audit = await enterprise('/api/enterprise/audit-log', {
      token: admin.token,
    })
    const auditRaw = JSON.stringify(await audit.json())
    expect(auditRaw).toContain('provider.created')
    expect(auditRaw).not.toContain(apiKey)
  })
})

describe('enterprise quota and usage', () => {
  it('aggregates all token buckets and blocks the next member turn at the daily limit', async () => {
    const admin = await readyAdmin()
    const member = await createAndLoginMember(admin.token, { dailyTokenLimit: 10 })
    const service = new EnterpriseService()

    await service.recordUsage({
      userId: member.user.id,
      sessionId: 'session-1',
      usage: {
        input_tokens: 3,
        output_tokens: 4,
        cache_read_tokens: 2,
        cache_creation_tokens: 1,
      },
    })

    const usage = await enterprise('/api/enterprise/usage', {
      token: admin.token,
    })
    expect(usage.status).toBe(200)
    const usageBody = await usage.json()
    expect(usageBody.usage[0]).toMatchObject({
      userId: member.user.id,
      inputTokens: 3,
      outputTokens: 4,
      cacheReadTokens: 2,
      cacheCreationTokens: 1,
      totalTokens: 10,
      dailyTokenLimit: 10,
    })

    await expect(service.assertWithinDailyLimit(member.user.id)).rejects.toMatchObject({
      statusCode: 429,
      code: 'DAILY_TOKEN_LIMIT_REACHED',
    })

    await service.assertWithinDailyLimit(admin.user.id)

    const audit = await enterprise('/api/enterprise/audit-log', {
      token: admin.token,
    })
    const auditTypes = (await audit.json()).events.map((event: { type: string }) => event.type)
    expect(auditTypes).toContain('quota.blocked')
  })
})

describe('enterprise version policy and persistence', () => {
  it('updates version policy through admin APIs and preserves unknown persisted fields', async () => {
    const admin = await readyAdmin()
    const statePath = enterpriseStatePath()
    const state = JSON.parse(await readFile(statePath, 'utf8')) as Record<string, unknown>
    state.vendorExtension = { keep: true }
    const users = state.users as Array<Record<string, unknown>>
    users[0]!.dailyTokenLimit = 'invalid-old-fixture-value'
    await writeFile(statePath, JSON.stringify(state, null, 2), 'utf8')

    const updated = await enterprise('/api/enterprise/version-policy', {
      method: 'PUT',
      token: admin.token,
      body: {
        targetVersion: '0.1.1',
        message: 'Push EcoreX enterprise release',
        force: true,
      },
    })
    expect(updated.status).toBe(200)
    expect((await updated.json()).policy).toMatchObject({
      targetVersion: '0.1.1',
      message: 'Push EcoreX enterprise release',
      force: true,
      updatedBy: admin.user.id,
    })

    const persisted = JSON.parse(await readFile(statePath, 'utf8')) as Record<string, unknown>
    expect(persisted.vendorExtension).toEqual({ keep: true })
    expect((persisted.users as Array<Record<string, unknown>>)[0]!.dailyTokenLimit).toBeNull()
  })
})

async function readyAdmin(): Promise<{ token: string; user: Record<string, any> }> {
  const login = await loginDefaultAdmin()
  const password = 'EcoreX@2026!AdminReady'
  const change = await enterprise('/api/enterprise/auth/password', {
    method: 'PUT',
    token: login.token,
    body: {
      currentPassword: ENTERPRISE_DEFAULT_PASSWORD,
      newPassword: password,
    },
  })
  expect(change.status).toBe(200)
  return await loginAdmin(password)
}

async function loginDefaultAdmin(): Promise<{ token: string; user: Record<string, any> }> {
  return await loginAdmin(ENTERPRISE_DEFAULT_PASSWORD)
}

async function loginAdmin(password: string): Promise<{ token: string; user: Record<string, any> }> {
  const response = await enterprise('/api/enterprise/auth/login', {
    method: 'POST',
    body: {
      email: ENTERPRISE_ADMIN_EMAIL,
      password,
    },
  })
  expect(response.status).toBe(200)
  return await response.json()
}

async function createAndLoginMember(
  adminToken: string,
  input: { dailyTokenLimit?: number | null } = {},
): Promise<{ token: string; user: Record<string, any> }> {
  const email = `member-${Date.now()}-${Math.random().toString(36).slice(2)}@example.com`
  const password = 'EcoreX@2026!MemberLogin'
  const created = await enterprise('/api/enterprise/users', {
    method: 'POST',
    token: adminToken,
    body: {
      email,
      password,
      dailyTokenLimit: input.dailyTokenLimit ?? 1000,
    },
  })
  expect(created.status).toBe(201)
  const login = await enterprise('/api/enterprise/auth/login', {
    method: 'POST',
    body: { email, password },
  })
  expect(login.status).toBe(200)
  return await login.json()
}

function providerBody(apiKey: string) {
  return {
    presetId: 'custom',
    name: 'EcoreX Enterprise Provider',
    apiKey,
    authStrategy: 'auth_token',
    baseUrl: 'https://api.example.com/anthropic',
    apiFormat: 'anthropic',
    runtimeKind: 'anthropic_compatible',
    models: {
      main: 'ecorex-main',
      haiku: 'ecorex-fast',
      sonnet: 'ecorex-main',
      opus: 'ecorex-deep',
    },
  }
}

async function enterprise(path: string, options: RequestOptions = {}): Promise<Response> {
  return await callHandler(handleEnterpriseApi, path, options)
}

async function providers(path: string, options: RequestOptions = {}): Promise<Response> {
  return await callHandler(handleProvidersApi, path, options)
}

async function settings(path: string, options: RequestOptions = {}): Promise<Response> {
  return await callHandler(handleSettingsApi, path, options)
}

async function callHandler(
  handler: (req: Request, url: URL, segments: string[]) => Promise<Response>,
  path: string,
  options: RequestOptions,
): Promise<Response> {
  const url = new URL(path, 'http://localhost')
  const headers = new Headers()
  if (options.token) headers.set('Authorization', `Bearer ${options.token}`)
  if (options.body !== undefined) headers.set('Content-Type', 'application/json')
  const req = new Request(url, {
    method: options.method || 'GET',
    headers,
    ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
  })
  return await handler(req, url, url.pathname.split('/').filter(Boolean))
}

function enterpriseStatePath(): string {
  return join(tempConfigDir, 'cc-haha', 'enterprise.json')
}
