import { api } from './client'
import type { ApiFormat, ModelMapping, ProviderAuthStrategy } from '../types/provider'

export type EnterpriseRole = 'admin' | 'member'
export type EnterpriseUserStatus = 'active' | 'disabled'

export type EnterprisePermissions = {
  canUseAgent: boolean
  canManageVersions: boolean
  allowedPermissionModes: string[]
}

export type EnterpriseUser = {
  id: string
  email: string
  displayName: string
  role: EnterpriseRole
  status: EnterpriseUserStatus
  mustChangePassword: boolean
  dailyTokenLimit: number | null
  permissions: EnterprisePermissions
  createdAt: string
  updatedAt: string
  lastLoginAt?: string
}

export type EnterpriseBootstrapStatus = {
  initialized: boolean
  defaultAdminEmail: string
  mustChangePassword: boolean
  usersCount: number
}

export type EnterpriseUsageSummary = {
  userId: string
  email: string
  displayName: string
  role: EnterpriseRole
  date: string
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheCreationTokens: number
  totalTokens: number
  dailyTokenLimit: number | null
}

export type EnterpriseAuditEvent = {
  id: string
  timestamp: string
  type: string
  actorUserId: string | null
  actorEmail: string | null
  targetUserId?: string | null
  details?: Record<string, unknown>
}

export type EnterpriseProviderSummary = {
  id: string
  presetId: string
  name: string
  authStrategy?: ProviderAuthStrategy
  baseUrl: string
  apiFormat: ApiFormat
  runtimeKind: 'anthropic_compatible' | 'openai_oauth'
  models: ModelMapping
  autoCompactWindow?: number
  modelContextWindows?: Record<string, number>
  notes?: string
  hasApiKey: boolean
  apiKeyPreview: string
}

export type EnterpriseProviderInput = {
  id?: string
  presetId: string
  name: string
  apiKey?: string
  authStrategy?: ProviderAuthStrategy
  baseUrl: string
  apiFormat: ApiFormat
  runtimeKind?: 'anthropic_compatible' | 'openai_oauth'
  models: ModelMapping
}

export type EnterpriseVersionPolicy = {
  targetVersion: string | null
  message: string
  force: boolean
  updatedAt: string | null
  updatedBy: string | null
}

export const enterpriseApi = {
  bootstrap: () => api.get<EnterpriseBootstrapStatus>('/api/enterprise/auth/bootstrap'),
  login: (email: string, password: string) =>
    api.post<{ token: string; user: EnterpriseUser }>('/api/enterprise/auth/login', { email, password }),
  logout: () => api.post<{ ok: boolean }>('/api/enterprise/auth/logout'),
  me: () => api.get<{ user: EnterpriseUser }>('/api/enterprise/auth/me'),
  changePassword: (currentPassword: string, newPassword: string) =>
    api.put<{ user: EnterpriseUser }>('/api/enterprise/auth/password', { currentPassword, newPassword }),
  listUsers: () => api.get<{ users: EnterpriseUser[] }>('/api/enterprise/users'),
  createUser: (input: {
    email: string
    displayName?: string
    role?: EnterpriseRole
    password?: string
    dailyTokenLimit?: number | null
  }) => api.post<{ user: EnterpriseUser; temporaryPassword: string }>('/api/enterprise/users', input),
  updateUser: (userId: string, input: Partial<{
    displayName: string
    role: EnterpriseRole
    status: EnterpriseUserStatus
    dailyTokenLimit: number | null
    permissions: Partial<EnterprisePermissions>
  }>) => api.put<{ user: EnterpriseUser }>(`/api/enterprise/users/${encodeURIComponent(userId)}`, input),
  resetPassword: (userId: string, password?: string) =>
    api.post<{ user: EnterpriseUser; temporaryPassword: string }>(
      `/api/enterprise/users/${encodeURIComponent(userId)}/reset-password`,
      password ? { password } : {},
    ),
  usage: () => api.get<{ usage: EnterpriseUsageSummary[] }>('/api/enterprise/usage'),
  auditLog: (limit = 200) => api.get<{ events: EnterpriseAuditEvent[] }>(`/api/enterprise/audit-log?limit=${limit}`),
  provider: () => api.get<{
    activeId: string | null
    provider: EnterpriseProviderSummary | null
    providers: EnterpriseProviderSummary[]
    presets: unknown[]
  }>('/api/enterprise/provider'),
  saveProvider: (input: EnterpriseProviderInput) =>
    api.put<{ provider: EnterpriseProviderSummary; activeId: string }>('/api/enterprise/provider', input),
  testProvider: (input: EnterpriseProviderInput) =>
    api.post<{ result: unknown }>('/api/enterprise/provider/test', input, { timeout: 120_000 }),
  versionPolicy: () => api.get<{ policy: EnterpriseVersionPolicy }>('/api/enterprise/version-policy'),
  updateVersionPolicy: (input: Partial<EnterpriseVersionPolicy>) =>
    api.put<{ policy: EnterpriseVersionPolicy }>('/api/enterprise/version-policy', input),
}
