import * as fs from 'node:fs/promises'
import * as path from 'node:path'
import * as os from 'node:os'
import {
  createHash,
  randomBytes,
  randomUUID,
  scryptSync,
  timingSafeEqual,
} from 'node:crypto'
import { ApiError } from '../middleware/errorHandler.js'
import type { TokenUsage } from '../ws/events.js'

export const ENTERPRISE_ADMIN_EMAIL = 'admin@ecorex.local'
export const ENTERPRISE_DEFAULT_PASSWORD = 'EcoreX@2026!ChangeMe'
export const ENTERPRISE_PROVIDER_NAME = 'EcoreX Enterprise Provider'

export type EnterpriseRole = 'admin' | 'member'
export type EnterpriseUserStatus = 'active' | 'disabled'

export type EnterprisePermissions = {
  canUseAgent: boolean
  canManageVersions: boolean
  allowedPermissionModes: string[]
}

export type EnterprisePublicUser = {
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

type EnterpriseUserRecord = EnterprisePublicUser & {
  passwordHash: string
  passwordSalt: string
}

type EnterpriseSessionRecord = {
  id: string
  userId: string
  tokenHash: string
  createdAt: string
  expiresAt: string
  lastSeenAt: string
}

export type EnterpriseVersionPolicy = {
  targetVersion: string | null
  message: string
  force: boolean
  updatedAt: string | null
  updatedBy: string | null
}

type EnterpriseState = {
  schemaVersion: 1
  initialized: boolean
  users: EnterpriseUserRecord[]
  sessions: EnterpriseSessionRecord[]
  versionPolicy: EnterpriseVersionPolicy
  [key: string]: unknown
}

export type EnterpriseSessionContext = {
  sessionId: string
  user: EnterprisePublicUser
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

export type EnterpriseUsageEntry = {
  id: string
  date: string
  timestamp: string
  userId: string
  sessionId?: string
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheCreationTokens: number
  totalTokens: number
}

const SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000
const HASH_BYTES = 32
const STATE_RENAME_ATTEMPTS = 5
let enterpriseStateWriteQueue: Promise<void> = Promise.resolve()

const DEFAULT_VERSION_POLICY: EnterpriseVersionPolicy = {
  targetVersion: '0.1.1',
  message: 'EcoreX enterprise release channel',
  force: false,
  updatedAt: null,
  updatedBy: null,
}

export class EnterpriseService {
  private getConfigDir(): string {
    return process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude')
  }

  private getCcHahaDir(): string {
    return path.join(this.getConfigDir(), 'cc-haha')
  }

  private getStatePath(): string {
    return path.join(this.getCcHahaDir(), 'enterprise.json')
  }

  private getAuditPath(): string {
    return path.join(this.getCcHahaDir(), 'enterprise-audit.jsonl')
  }

  private getUsagePath(): string {
    return path.join(this.getCcHahaDir(), 'enterprise-usage.jsonl')
  }

  async isInitialized(): Promise<boolean> {
    try {
      const state = await this.readState()
      return state.initialized && state.users.length > 0
    } catch {
      return false
    }
  }

  async getBootstrapStatus(): Promise<{
    initialized: boolean
    defaultAdminEmail: string
    mustChangePassword: boolean
    usersCount: number
  }> {
    const state = await this.ensureBootstrapState()
    const defaultAdmin = state.users.find((user) => user.email === ENTERPRISE_ADMIN_EMAIL)
    return {
      initialized: state.initialized,
      defaultAdminEmail: ENTERPRISE_ADMIN_EMAIL,
      mustChangePassword: defaultAdmin?.mustChangePassword ?? false,
      usersCount: state.users.length,
    }
  }

  async login(email: string, password: string): Promise<{ token: string; user: EnterprisePublicUser }> {
    const state = await this.ensureBootstrapState()
    const normalizedEmail = normalizeEmail(email)
    const user = state.users.find((entry) => entry.email === normalizedEmail)
    if (!user || !verifySecret(password, user.passwordSalt, user.passwordHash)) {
      await this.appendAuditLog('auth.login_failed', {
        actorUserId: null,
        actorEmail: normalizedEmail || null,
        details: { email: normalizedEmail },
      })
      throw new ApiError(401, 'Invalid email or password', 'UNAUTHORIZED')
    }
    if (user.status !== 'active') {
      await this.appendAuditLog('auth.login_blocked_disabled', {
        actorUserId: user.id,
        actorEmail: user.email,
        targetUserId: user.id,
      })
      throw new ApiError(403, 'This enterprise account is disabled', 'FORBIDDEN')
    }

    const rawToken = `ecx_${randomBytes(32).toString('base64url')}`
    const now = new Date()
    const nowIso = now.toISOString()
    const session: EnterpriseSessionRecord = {
      id: randomUUID(),
      userId: user.id,
      tokenHash: hashToken(rawToken),
      createdAt: nowIso,
      expiresAt: new Date(now.getTime() + SESSION_TTL_MS).toISOString(),
      lastSeenAt: nowIso,
    }
    user.lastLoginAt = nowIso
    user.updatedAt = nowIso
    state.sessions = this.pruneExpiredSessions(state.sessions)
    state.sessions.push(session)
    await this.writeState(state)
    await this.appendAuditLog('auth.login', {
      actorUserId: user.id,
      actorEmail: user.email,
      targetUserId: user.id,
    })
    return { token: rawToken, user: toPublicUser(user) }
  }

  async logout(req: Request): Promise<void> {
    const token = extractBearerToken(req)
    if (!token) return
    const state = await this.readState()
    const tokenHash = hashToken(token)
    const session = state.sessions.find((entry) => entry.tokenHash === tokenHash)
    const user = session ? state.users.find((entry) => entry.id === session.userId) : null
    state.sessions = state.sessions.filter((entry) => entry.tokenHash !== tokenHash)
    await this.writeState(state)
    await this.appendAuditLog('auth.logout', {
      actorUserId: user?.id ?? null,
      actorEmail: user?.email ?? null,
      targetUserId: user?.id ?? null,
    })
  }

  async requireUser(req: Request, tokenOverride?: string | null): Promise<EnterpriseSessionContext> {
    const token = tokenOverride || extractBearerToken(req)
    if (!token) {
      throw new ApiError(401, 'Enterprise login required', 'UNAUTHORIZED')
    }

    const state = await this.readState()
    const tokenHash = hashToken(token)
    const session = state.sessions.find((entry) => entry.tokenHash === tokenHash)
    if (!session || Date.parse(session.expiresAt) <= Date.now()) {
      throw new ApiError(401, 'Enterprise session expired or invalid', 'UNAUTHORIZED')
    }

    const user = state.users.find((entry) => entry.id === session.userId)
    if (!user) {
      throw new ApiError(401, 'Enterprise session user no longer exists', 'UNAUTHORIZED')
    }
    if (user.status !== 'active') {
      throw new ApiError(403, 'This enterprise account is disabled', 'FORBIDDEN')
    }

    session.lastSeenAt = new Date().toISOString()
    state.sessions = this.pruneExpiredSessions(state.sessions)
    await this.writeState(state)

    return {
      sessionId: session.id,
      user: toPublicUser(user),
    }
  }

  async requireAdmin(req: Request, tokenOverride?: string | null): Promise<EnterpriseSessionContext> {
    const context = await this.requireUser(req, tokenOverride)
    if (context.user.role !== 'admin') {
      throw new ApiError(403, 'Enterprise admin access required', 'FORBIDDEN')
    }
    return context
  }

  async changePassword(
    context: EnterpriseSessionContext,
    currentPassword: string,
    newPassword: string,
  ): Promise<EnterprisePublicUser> {
    assertPasswordQuality(newPassword)
    const state = await this.readState()
    const user = requireUserRecord(state, context.user.id)
    if (!verifySecret(currentPassword, user.passwordSalt, user.passwordHash)) {
      throw new ApiError(400, 'Current password is incorrect', 'BAD_REQUEST')
    }
    const { salt, hash } = hashSecret(newPassword)
    user.passwordSalt = salt
    user.passwordHash = hash
    user.mustChangePassword = false
    user.updatedAt = new Date().toISOString()
    await this.writeState(state)
    await this.appendAuditLog('auth.password_changed', {
      actorUserId: user.id,
      actorEmail: user.email,
      targetUserId: user.id,
    })
    return toPublicUser(user)
  }

  async listUsers(): Promise<EnterprisePublicUser[]> {
    const state = await this.ensureBootstrapState()
    return state.users.map(toPublicUser)
  }

  async createUser(
    actor: EnterpriseSessionContext,
    input: {
      email: string
      displayName?: string
      role?: EnterpriseRole
      password?: string
      dailyTokenLimit?: number | null
      permissions?: Partial<EnterprisePermissions>
    },
  ): Promise<{ user: EnterprisePublicUser; temporaryPassword: string }> {
    const state = await this.readState()
    const email = normalizeEmail(input.email)
    if (!email) throw ApiError.badRequest('Email is required')
    if (state.users.some((entry) => entry.email === email)) {
      throw ApiError.conflict('Enterprise user already exists')
    }
    const temporaryPassword = input.password || generateTemporaryPassword()
    assertPasswordQuality(temporaryPassword)
    const { salt, hash } = hashSecret(temporaryPassword)
    const now = new Date().toISOString()
    const role = input.role === 'admin' ? 'admin' : 'member'
    const user: EnterpriseUserRecord = {
      id: randomUUID(),
      email,
      displayName: input.displayName?.trim() || email,
      role,
      status: 'active',
      mustChangePassword: true,
      dailyTokenLimit: normalizeDailyTokenLimit(input.dailyTokenLimit),
      permissions: buildPermissions(role, input.permissions),
      createdAt: now,
      updatedAt: now,
      passwordSalt: salt,
      passwordHash: hash,
    }
    state.users.push(user)
    await this.writeState(state)
    await this.appendAuditLog('user.created', {
      actorUserId: actor.user.id,
      actorEmail: actor.user.email,
      targetUserId: user.id,
      details: { email: user.email, role: user.role, dailyTokenLimit: user.dailyTokenLimit },
    })
    return { user: toPublicUser(user), temporaryPassword }
  }

  async updateUser(
    actor: EnterpriseSessionContext,
    userId: string,
    input: Partial<{
      displayName: string
      role: EnterpriseRole
      status: EnterpriseUserStatus
      dailyTokenLimit: number | null
      permissions: Partial<EnterprisePermissions>
    }>,
  ): Promise<EnterprisePublicUser> {
    const state = await this.readState()
    const user = requireUserRecord(state, userId)
    const previous = toPublicUser(user)
    if (input.displayName !== undefined) {
      const displayName = input.displayName.trim()
      if (!displayName) throw ApiError.badRequest('Display name is required')
      user.displayName = displayName
    }
    if (input.role !== undefined) {
      if (input.role !== 'admin' && input.role !== 'member') {
        throw ApiError.badRequest('Role must be admin or member')
      }
      user.role = input.role
      user.permissions = buildPermissions(input.role, user.permissions)
    }
    if (input.status !== undefined) {
      if (input.status !== 'active' && input.status !== 'disabled') {
        throw ApiError.badRequest('Status must be active or disabled')
      }
      user.status = input.status
    }
    if ('dailyTokenLimit' in input) {
      user.dailyTokenLimit = normalizeDailyTokenLimit(input.dailyTokenLimit)
    }
    if (input.permissions) {
      user.permissions = buildPermissions(user.role, {
        ...user.permissions,
        ...input.permissions,
      })
    }
    ensureAtLeastOneActiveAdmin(state)
    user.updatedAt = new Date().toISOString()
    if (user.status === 'disabled') {
      state.sessions = state.sessions.filter((entry) => entry.userId !== user.id)
    }
    await this.writeState(state)
    await this.appendAuditLog('user.updated', {
      actorUserId: actor.user.id,
      actorEmail: actor.user.email,
      targetUserId: user.id,
      details: diffPublicUser(previous, toPublicUser(user)),
    })
    return toPublicUser(user)
  }

  async resetPassword(
    actor: EnterpriseSessionContext,
    userId: string,
    newPassword?: string,
  ): Promise<{ user: EnterprisePublicUser; temporaryPassword: string }> {
    const temporaryPassword = newPassword || generateTemporaryPassword()
    assertPasswordQuality(temporaryPassword)
    const state = await this.readState()
    const user = requireUserRecord(state, userId)
    const { salt, hash } = hashSecret(temporaryPassword)
    user.passwordSalt = salt
    user.passwordHash = hash
    user.mustChangePassword = true
    user.updatedAt = new Date().toISOString()
    state.sessions = state.sessions.filter((entry) => entry.userId !== user.id)
    await this.writeState(state)
    await this.appendAuditLog('auth.password_reset', {
      actorUserId: actor.user.id,
      actorEmail: actor.user.email,
      targetUserId: user.id,
      details: { email: user.email },
    })
    return { user: toPublicUser(user), temporaryPassword }
  }

  async assertWithinDailyLimit(userId: string): Promise<void> {
    const state = await this.readState()
    const user = requireUserRecord(state, userId)
    if (user.role === 'admin' || user.dailyTokenLimit === null) return

    const date = localDateKey()
    const usedTokens = await this.getDailyTokenTotal(user.id, date)
    if (usedTokens < user.dailyTokenLimit) return

    await this.appendAuditLog('quota.blocked', {
      actorUserId: user.id,
      actorEmail: user.email,
      targetUserId: user.id,
      details: { date, usedTokens, dailyTokenLimit: user.dailyTokenLimit },
    })
    throw new ApiError(
      429,
      `Daily token limit reached (${usedTokens}/${user.dailyTokenLimit}). Contact your EcoreX administrator.`,
      'DAILY_TOKEN_LIMIT_REACHED',
    )
  }

  async recordUsage(input: {
    userId: string
    sessionId?: string
    usage: Partial<TokenUsage> & Record<string, unknown>
  }): Promise<EnterpriseUsageEntry | null> {
    const total = tokenUsageTotal(input.usage)
    if (total <= 0) return null
    const entry: EnterpriseUsageEntry = {
      id: randomUUID(),
      date: localDateKey(),
      timestamp: new Date().toISOString(),
      userId: input.userId,
      ...(input.sessionId ? { sessionId: input.sessionId } : {}),
      inputTokens: readNumber(input.usage.input_tokens),
      outputTokens: readNumber(input.usage.output_tokens),
      cacheReadTokens: readNumber(input.usage.cache_read_tokens ?? input.usage.cache_read_input_tokens),
      cacheCreationTokens: readNumber(input.usage.cache_creation_tokens ?? input.usage.cache_creation_input_tokens),
      totalTokens: total,
    }
    await appendJsonLine(this.getUsagePath(), entry)
    return entry
  }

  async getUsageSummary(): Promise<Array<{
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
  }>> {
    const [state, entries] = await Promise.all([
      this.ensureBootstrapState(),
      readJsonLines<EnterpriseUsageEntry>(this.getUsagePath()),
    ])
    const usersById = new Map(state.users.map((user) => [user.id, user]))
    const buckets = new Map<string, EnterpriseUsageEntry>()
    for (const entry of entries) {
      const key = `${entry.userId}:${entry.date}`
      const current = buckets.get(key)
      if (!current) {
        buckets.set(key, { ...entry })
      } else {
        current.inputTokens += entry.inputTokens
        current.outputTokens += entry.outputTokens
        current.cacheReadTokens += entry.cacheReadTokens
        current.cacheCreationTokens += entry.cacheCreationTokens
        current.totalTokens += entry.totalTokens
      }
    }
    return [...buckets.values()]
      .sort((a, b) => b.date.localeCompare(a.date) || b.totalTokens - a.totalTokens)
      .map((entry) => {
        const user = usersById.get(entry.userId)
        return {
          userId: entry.userId,
          email: user?.email ?? 'unknown',
          displayName: user?.displayName ?? 'Unknown user',
          role: user?.role ?? 'member',
          date: entry.date,
          inputTokens: entry.inputTokens,
          outputTokens: entry.outputTokens,
          cacheReadTokens: entry.cacheReadTokens,
          cacheCreationTokens: entry.cacheCreationTokens,
          totalTokens: entry.totalTokens,
          dailyTokenLimit: user?.dailyTokenLimit ?? null,
        }
      })
  }

  async readAuditLog(limit = 200): Promise<EnterpriseAuditEvent[]> {
    const entries = await readJsonLines<EnterpriseAuditEvent>(this.getAuditPath())
    return entries.slice(-Math.max(1, Math.min(limit, 1000))).reverse()
  }

  async getVersionPolicy(): Promise<EnterpriseVersionPolicy> {
    const state = await this.ensureBootstrapState()
    return state.versionPolicy
  }

  async updateVersionPolicy(
    actor: EnterpriseSessionContext,
    input: Partial<EnterpriseVersionPolicy>,
  ): Promise<EnterpriseVersionPolicy> {
    const state = await this.readState()
    const next: EnterpriseVersionPolicy = {
      targetVersion: typeof input.targetVersion === 'string'
        ? input.targetVersion.trim() || null
        : input.targetVersion === null
          ? null
          : state.versionPolicy.targetVersion,
      message: typeof input.message === 'string'
        ? input.message.trim()
        : state.versionPolicy.message,
      force: typeof input.force === 'boolean'
        ? input.force
        : state.versionPolicy.force,
      updatedAt: new Date().toISOString(),
      updatedBy: actor.user.id,
    }
    state.versionPolicy = next
    await this.writeState(state)
    await this.appendAuditLog('version_policy.updated', {
      actorUserId: actor.user.id,
      actorEmail: actor.user.email,
      details: next,
    })
    return next
  }

  async appendAuditLog(
    type: string,
    input: {
      actorUserId: string | null
      actorEmail: string | null
      targetUserId?: string | null
      details?: Record<string, unknown>
    },
  ): Promise<void> {
    const event: EnterpriseAuditEvent = {
      id: randomUUID(),
      timestamp: new Date().toISOString(),
      type,
      actorUserId: input.actorUserId,
      actorEmail: input.actorEmail,
      ...(input.targetUserId !== undefined ? { targetUserId: input.targetUserId } : {}),
      ...(input.details ? { details: redactSensitiveDetails(input.details) } : {}),
    }
    await appendJsonLine(this.getAuditPath(), event)
  }

  private async ensureBootstrapState(): Promise<EnterpriseState> {
    const state = await this.readState()
    if (state.users.length > 0) {
      if (!state.initialized) {
        state.initialized = true
        await this.writeState(state)
      }
      return state
    }

    const now = new Date().toISOString()
    const { salt, hash } = hashSecret(ENTERPRISE_DEFAULT_PASSWORD)
    const admin: EnterpriseUserRecord = {
      id: randomUUID(),
      email: ENTERPRISE_ADMIN_EMAIL,
      displayName: 'EcoreX Administrator',
      role: 'admin',
      status: 'active',
      mustChangePassword: true,
      dailyTokenLimit: null,
      permissions: buildPermissions('admin'),
      createdAt: now,
      updatedAt: now,
      passwordSalt: salt,
      passwordHash: hash,
    }
    state.initialized = true
    state.users = [admin]
    state.sessions = []
    state.versionPolicy = { ...DEFAULT_VERSION_POLICY }
    await this.writeState(state)
    await this.appendAuditLog('enterprise.bootstrap_admin_created', {
      actorUserId: admin.id,
      actorEmail: admin.email,
      targetUserId: admin.id,
      details: { email: admin.email },
    })
    return state
  }

  private async readState(): Promise<EnterpriseState> {
    try {
      const raw = await fs.readFile(this.getStatePath(), 'utf-8')
      return normalizeState(JSON.parse(raw) as Record<string, unknown>)
    } catch (error) {
      if (error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT') {
        return emptyState()
      }
      throw error
    }
  }

  private async writeState(state: EnterpriseState): Promise<void> {
    const writeOperation = enterpriseStateWriteQueue
      .catch(() => undefined)
      .then(() => this.writeStateNow(state))
    enterpriseStateWriteQueue = writeOperation.catch(() => undefined)
    await writeOperation
  }

  private async writeStateNow(state: EnterpriseState): Promise<void> {
    const filePath = this.getStatePath()
    const dir = path.dirname(filePath)
    await fs.mkdir(dir, { recursive: true })
    const tmpFile = `${filePath}.tmp.${Date.now()}.${randomUUID()}`
    try {
      await fs.writeFile(tmpFile, JSON.stringify(normalizeState(state), null, 2) + '\n', 'utf-8')
      await renameWithRetry(tmpFile, filePath)
    } catch (error) {
      await fs.unlink(tmpFile).catch(() => {})
      throw ApiError.internal(`Failed to write enterprise config: ${error}`)
    }
  }

  private pruneExpiredSessions(sessions: EnterpriseSessionRecord[]): EnterpriseSessionRecord[] {
    const now = Date.now()
    return sessions.filter((session) => Date.parse(session.expiresAt) > now)
  }

  private async getDailyTokenTotal(userId: string, date: string): Promise<number> {
    const entries = await readJsonLines<EnterpriseUsageEntry>(this.getUsagePath())
    return entries
      .filter((entry) => entry.userId === userId && entry.date === date)
      .reduce((sum, entry) => sum + entry.totalTokens, 0)
  }
}

async function renameWithRetry(from: string, to: string): Promise<void> {
  let lastError: unknown
  for (let attempt = 0; attempt < STATE_RENAME_ATTEMPTS; attempt += 1) {
    try {
      await fs.rename(from, to)
      return
    } catch (error) {
      lastError = error
      const code = (error as NodeJS.ErrnoException).code
      if (process.platform !== 'win32' || (code !== 'EPERM' && code !== 'EACCES' && code !== 'EBUSY')) {
        throw error
      }
      await new Promise((resolve) => setTimeout(resolve, 20 * (attempt + 1)))
    }
  }
  throw lastError
}

function emptyState(): EnterpriseState {
  return {
    schemaVersion: 1,
    initialized: false,
    users: [],
    sessions: [],
    versionPolicy: { ...DEFAULT_VERSION_POLICY },
  }
}

function normalizeState(value: Record<string, unknown>): EnterpriseState {
  const users = Array.isArray(value.users)
    ? value.users.map(normalizeUser).filter((user): user is EnterpriseUserRecord => Boolean(user))
    : []
  const sessions = Array.isArray(value.sessions)
    ? value.sessions.map(normalizeSession).filter((session): session is EnterpriseSessionRecord => Boolean(session))
    : []
  return {
    ...value,
    schemaVersion: 1,
    initialized: value.initialized === true || users.length > 0,
    users,
    sessions,
    versionPolicy: normalizeVersionPolicy(value.versionPolicy),
  }
}

function normalizeUser(value: unknown): EnterpriseUserRecord | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const id = typeof record.id === 'string' && record.id.trim() ? record.id : randomUUID()
  const email = normalizeEmail(record.email)
  if (!email) return null
  const role: EnterpriseRole = record.role === 'admin' ? 'admin' : 'member'
  return {
    id,
    email,
    displayName: typeof record.displayName === 'string' && record.displayName.trim()
      ? record.displayName.trim()
      : email,
    role,
    status: record.status === 'disabled' ? 'disabled' : 'active',
    mustChangePassword: record.mustChangePassword !== false,
    dailyTokenLimit: normalizePersistedDailyTokenLimit(record.dailyTokenLimit),
    permissions: buildPermissions(role, isPermissions(record.permissions) ? record.permissions : undefined),
    createdAt: typeof record.createdAt === 'string' ? record.createdAt : new Date().toISOString(),
    updatedAt: typeof record.updatedAt === 'string' ? record.updatedAt : new Date().toISOString(),
    ...(typeof record.lastLoginAt === 'string' ? { lastLoginAt: record.lastLoginAt } : {}),
    passwordHash: typeof record.passwordHash === 'string' ? record.passwordHash : '',
    passwordSalt: typeof record.passwordSalt === 'string' ? record.passwordSalt : '',
  }
}

function normalizeSession(value: unknown): EnterpriseSessionRecord | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  if (
    typeof record.id !== 'string' ||
    typeof record.userId !== 'string' ||
    typeof record.tokenHash !== 'string' ||
    typeof record.expiresAt !== 'string'
  ) {
    return null
  }
  return {
    id: record.id,
    userId: record.userId,
    tokenHash: record.tokenHash,
    createdAt: typeof record.createdAt === 'string' ? record.createdAt : new Date().toISOString(),
    expiresAt: record.expiresAt,
    lastSeenAt: typeof record.lastSeenAt === 'string' ? record.lastSeenAt : new Date().toISOString(),
  }
}

function normalizeVersionPolicy(value: unknown): EnterpriseVersionPolicy {
  if (!value || typeof value !== 'object') return { ...DEFAULT_VERSION_POLICY }
  const record = value as Record<string, unknown>
  return {
    targetVersion: typeof record.targetVersion === 'string'
      ? record.targetVersion
      : record.targetVersion === null
        ? null
        : DEFAULT_VERSION_POLICY.targetVersion,
    message: typeof record.message === 'string' ? record.message : DEFAULT_VERSION_POLICY.message,
    force: typeof record.force === 'boolean' ? record.force : DEFAULT_VERSION_POLICY.force,
    updatedAt: typeof record.updatedAt === 'string' ? record.updatedAt : null,
    updatedBy: typeof record.updatedBy === 'string' ? record.updatedBy : null,
  }
}

function toPublicUser(user: EnterpriseUserRecord): EnterprisePublicUser {
  return {
    id: user.id,
    email: user.email,
    displayName: user.displayName,
    role: user.role,
    status: user.status,
    mustChangePassword: user.mustChangePassword,
    dailyTokenLimit: user.dailyTokenLimit,
    permissions: user.permissions,
    createdAt: user.createdAt,
    updatedAt: user.updatedAt,
    ...(user.lastLoginAt ? { lastLoginAt: user.lastLoginAt } : {}),
  }
}

function requireUserRecord(state: EnterpriseState, userId: string): EnterpriseUserRecord {
  const user = state.users.find((entry) => entry.id === userId)
  if (!user) throw ApiError.notFound(`Enterprise user not found: ${userId}`)
  return user
}

function ensureAtLeastOneActiveAdmin(state: EnterpriseState): void {
  const activeAdmins = state.users.filter((user) => user.role === 'admin' && user.status === 'active')
  if (activeAdmins.length === 0) {
    throw ApiError.conflict('At least one active enterprise admin is required')
  }
}

function buildPermissions(
  role: EnterpriseRole,
  overrides: Partial<EnterprisePermissions> = {},
): EnterprisePermissions {
  return {
    canUseAgent: overrides.canUseAgent ?? true,
    canManageVersions: role === 'admin' ? true : overrides.canManageVersions === true,
    allowedPermissionModes: Array.isArray(overrides.allowedPermissionModes)
      ? overrides.allowedPermissionModes.filter((entry): entry is string => typeof entry === 'string')
      : role === 'admin'
        ? ['default', 'plan', 'acceptEdits', 'bypassPermissions']
        : ['default', 'plan', 'acceptEdits'],
  }
}

function isPermissions(value: unknown): value is Partial<EnterprisePermissions> {
  return Boolean(value && typeof value === 'object')
}

function normalizeEmail(value: unknown): string {
  return typeof value === 'string' ? value.trim().toLowerCase() : ''
}

function normalizeDailyTokenLimit(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed) || parsed < 0) {
    throw ApiError.badRequest('Daily token limit must be a non-negative number or null')
  }
  return Math.floor(parsed)
}

function normalizePersistedDailyTokenLimit(value: unknown): number | null {
  try {
    return normalizeDailyTokenLimit(value)
  } catch {
    return null
  }
}

function assertPasswordQuality(password: string): void {
  if (typeof password !== 'string' || password.length < 12) {
    throw ApiError.badRequest('Password must be at least 12 characters')
  }
}

function hashSecret(secret: string): { salt: string; hash: string } {
  const salt = randomBytes(16).toString('base64')
  const hash = scryptSync(secret, salt, HASH_BYTES).toString('base64')
  return { salt, hash }
}

function verifySecret(secret: string, salt: string, expectedHash: string): boolean {
  if (!salt || !expectedHash) return false
  const actual = scryptSync(secret, salt, HASH_BYTES)
  const expected = Buffer.from(expectedHash, 'base64')
  if (actual.length !== expected.length) return false
  return timingSafeEqual(actual, expected)
}

function hashToken(token: string): string {
  return createHash('sha256').update(token).digest('hex')
}

function extractBearerToken(req: Request): string | null {
  const authorization = req.headers.get('Authorization') || ''
  const match = authorization.match(/^Bearer\s+(.+)$/i)
  return match ? match[1].trim() : null
}

function generateTemporaryPassword(): string {
  return `EcoreX@${new Date().getFullYear()}!${randomBytes(6).toString('base64url')}`
}

function localDateKey(date = new Date()): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function readNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}

function tokenUsageTotal(usage: Partial<TokenUsage> & Record<string, unknown>): number {
  return (
    readNumber(usage.input_tokens) +
    readNumber(usage.output_tokens) +
    readNumber(usage.cache_read_tokens ?? usage.cache_read_input_tokens) +
    readNumber(usage.cache_creation_tokens ?? usage.cache_creation_input_tokens)
  )
}

async function appendJsonLine(filePath: string, value: unknown): Promise<void> {
  await fs.mkdir(path.dirname(filePath), { recursive: true })
  await fs.appendFile(filePath, `${JSON.stringify(value)}\n`, 'utf-8')
}

async function readJsonLines<T>(filePath: string): Promise<T[]> {
  try {
    const raw = await fs.readFile(filePath, 'utf-8')
    return raw
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .flatMap((line) => {
        try {
          return [JSON.parse(line) as T]
        } catch {
          return []
        }
      })
  } catch (error) {
    if (error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT') {
      return []
    }
    throw error
  }
}

function diffPublicUser(
  before: EnterprisePublicUser,
  after: EnterprisePublicUser,
): Record<string, unknown> {
  const diff: Record<string, unknown> = {}
  for (const key of Object.keys(after) as Array<keyof EnterprisePublicUser>) {
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
      diff[key] = { before: before[key], after: after[key] }
    }
  }
  return diff
}

function redactSensitiveDetails(details: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(details).map(([key, value]) => {
      const lowered = key.toLowerCase()
      if (
        lowered.includes('password') ||
        lowered.includes('token') ||
        lowered.includes('apikey') ||
        lowered.includes('api_key') ||
        lowered.includes('secret')
      ) {
        return [key, '[redacted]']
      }
      return [key, value]
    }),
  )
}
