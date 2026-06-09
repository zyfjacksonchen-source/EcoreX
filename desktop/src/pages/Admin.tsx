import { useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  Activity,
  KeyRound,
  LockKeyhole,
  RefreshCw,
  Rocket,
  Save,
  ScrollText,
  ShieldCheck,
  UserPlus,
  Users,
  type LucideIcon,
} from 'lucide-react'
import {
  enterpriseApi,
  type EnterpriseAuditEvent,
  type EnterpriseProviderInput,
  type EnterpriseProviderSummary,
  type EnterpriseRole,
  type EnterpriseUsageSummary,
  type EnterpriseUser,
  type EnterpriseVersionPolicy,
} from '../api/enterprise'
import { useEnterpriseStore } from '../stores/enterpriseStore'
import { useUIStore } from '../stores/uiStore'

type AdminSection = 'users' | 'usage' | 'logs' | 'provider' | 'version'

const SECTION_ITEMS: Array<{ id: AdminSection; label: string; icon: LucideIcon }> = [
  { id: 'users', label: 'Users', icon: Users },
  { id: 'usage', label: 'Usage', icon: Activity },
  { id: 'logs', label: 'Audit log', icon: ScrollText },
  { id: 'provider', label: 'Provider', icon: KeyRound },
  { id: 'version', label: 'Version', icon: Rocket },
]

const EMPTY_MODELS = {
  main: '',
  haiku: '',
  sonnet: '',
  opus: '',
}

export function Admin() {
  const currentUser = useEnterpriseStore((s) => s.user)
  const logout = useEnterpriseStore((s) => s.logout)
  const addToast = useUIStore((s) => s.addToast)
  const [section, setSection] = useState<AdminSection>('users')
  const [users, setUsers] = useState<EnterpriseUser[]>([])
  const [usage, setUsage] = useState<EnterpriseUsageSummary[]>([])
  const [auditEvents, setAuditEvents] = useState<EnterpriseAuditEvent[]>([])
  const [provider, setProvider] = useState<EnterpriseProviderSummary | null>(null)
  const [versionPolicy, setVersionPolicy] = useState<EnterpriseVersionPolicy | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [temporaryPassword, setTemporaryPassword] = useState<string | null>(null)
  const [newUser, setNewUser] = useState({
    email: '',
    displayName: '',
    role: 'member' as EnterpriseRole,
    dailyTokenLimit: '',
  })
  const [providerForm, setProviderForm] = useState<EnterpriseProviderInput>({
    presetId: 'custom',
    name: 'EcoreX Enterprise Provider',
    baseUrl: '',
    apiKey: '',
    apiFormat: 'anthropic',
    runtimeKind: 'anthropic_compatible',
    models: EMPTY_MODELS,
  })
  const [versionForm, setVersionForm] = useState({
    targetVersion: '0.1.1',
    message: '',
    force: false,
  })

  const usageByUser = useMemo(() => {
    const today = new Date()
    const date = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
    return new Map(
      usage
        .filter((entry) => entry.date === date)
        .map((entry) => [entry.userId, entry]),
    )
  }, [usage])

  const loadAdminData = async () => {
    if (currentUser?.role !== 'admin') return
    setIsLoading(true)
    setError(null)
    try {
      const [usersRes, usageRes, auditRes, providerRes, versionRes] = await Promise.all([
        enterpriseApi.listUsers(),
        enterpriseApi.usage(),
        enterpriseApi.auditLog(250),
        enterpriseApi.provider(),
        enterpriseApi.versionPolicy(),
      ])
      setUsers(usersRes.users)
      setUsage(usageRes.usage)
      setAuditEvents(auditRes.events)
      setProvider(providerRes.provider)
      setVersionPolicy(versionRes.policy)
      if (providerRes.provider) {
        setProviderForm({
          id: providerRes.provider.id,
          presetId: providerRes.provider.presetId,
          name: providerRes.provider.name,
          baseUrl: providerRes.provider.baseUrl,
          apiKey: '',
          authStrategy: providerRes.provider.authStrategy,
          apiFormat: providerRes.provider.apiFormat,
          runtimeKind: providerRes.provider.runtimeKind,
          models: providerRes.provider.models,
        })
      }
      setVersionForm({
        targetVersion: versionRes.policy.targetVersion ?? '',
        message: versionRes.policy.message,
        force: versionRes.policy.force,
      })
    } catch (error) {
      setError(error instanceof Error ? error.message : 'Failed to load admin data')
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    void loadAdminData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentUser?.id, currentUser?.role])

  if (currentUser?.role !== 'admin') {
    return (
      <div className="flex flex-1 items-center justify-center bg-[var(--color-surface)] p-8 text-[var(--color-text-secondary)]">
        <div className="max-w-md rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-container-lowest)] p-6 text-center">
          <ShieldCheck className="mx-auto mb-3 text-[var(--color-brand)]" size={30} />
          <h1 className="text-lg font-semibold text-[var(--color-text-primary)]">Admin access required</h1>
          <p className="mt-2 text-sm">Enterprise management is available to EcoreX administrators only.</p>
        </div>
      </div>
    )
  }

  const handleCreateUser = async (event: FormEvent) => {
    event.preventDefault()
    setTemporaryPassword(null)
    try {
      const result = await enterpriseApi.createUser({
        email: newUser.email,
        displayName: newUser.displayName || undefined,
        role: newUser.role,
        dailyTokenLimit: parseDailyLimit(newUser.dailyTokenLimit),
      })
      setTemporaryPassword(result.temporaryPassword)
      setNewUser({ email: '', displayName: '', role: 'member', dailyTokenLimit: '' })
      addToast({ type: 'success', message: 'User created' })
      await loadAdminData()
    } catch (error) {
      addToast({ type: 'error', message: error instanceof Error ? error.message : 'Failed to create user' })
    }
  }

  const handleUpdateUser = async (userId: string, input: Parameters<typeof enterpriseApi.updateUser>[1]) => {
    try {
      await enterpriseApi.updateUser(userId, input)
      addToast({ type: 'success', message: 'User updated' })
      await loadAdminData()
    } catch (error) {
      addToast({ type: 'error', message: error instanceof Error ? error.message : 'Failed to update user' })
    }
  }

  const handleResetPassword = async (userId: string) => {
    try {
      const result = await enterpriseApi.resetPassword(userId)
      setTemporaryPassword(result.temporaryPassword)
      addToast({ type: 'success', message: 'Password reset' })
      await loadAdminData()
    } catch (error) {
      addToast({ type: 'error', message: error instanceof Error ? error.message : 'Failed to reset password' })
    }
  }

  const handleSaveProvider = async (event: FormEvent) => {
    event.preventDefault()
    try {
      const payload: EnterpriseProviderInput = {
        ...providerForm,
        apiKey: providerForm.apiKey?.trim() || undefined,
        name: providerForm.name.trim() || 'EcoreX Enterprise Provider',
        baseUrl: providerForm.baseUrl.trim(),
        models: {
          main: providerForm.models.main.trim(),
          haiku: providerForm.models.haiku.trim(),
          sonnet: providerForm.models.sonnet.trim(),
          opus: providerForm.models.opus.trim(),
        },
      }
      const result = await enterpriseApi.saveProvider(payload)
      setProvider(result.provider)
      addToast({ type: 'success', message: 'Enterprise provider saved' })
      await loadAdminData()
    } catch (error) {
      addToast({ type: 'error', message: error instanceof Error ? error.message : 'Failed to save provider' })
    }
  }

  const handleSaveVersionPolicy = async (event: FormEvent) => {
    event.preventDefault()
    try {
      const result = await enterpriseApi.updateVersionPolicy({
        targetVersion: versionForm.targetVersion.trim() || null,
        message: versionForm.message,
        force: versionForm.force,
      })
      setVersionPolicy(result.policy)
      addToast({ type: 'success', message: 'Version policy saved' })
      await loadAdminData()
    } catch (error) {
      addToast({ type: 'error', message: error instanceof Error ? error.message : 'Failed to save version policy' })
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-[var(--color-surface)] text-[var(--color-text-primary)]">
      <div className="flex shrink-0 items-center justify-between border-b border-[var(--color-border)] px-6 py-4">
        <div>
          <h1 className="text-xl font-bold">Enterprise Admin</h1>
          <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
            Manage EcoreX users, quotas, provider governance, audit logs, and version policy.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="admin-icon-button" type="button" onClick={() => void loadAdminData()} title="Refresh">
            <RefreshCw size={17} className={isLoading ? 'animate-spin' : ''} />
          </button>
          <button className="admin-icon-button" type="button" onClick={() => void logout()} title="Logout">
            <LockKeyhole size={17} />
          </button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <nav className="w-[190px] shrink-0 border-r border-[var(--color-border)] p-3">
          {SECTION_ITEMS.map((item) => {
            const Icon = item.icon
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setSection(item.id)}
                className={`mb-1 flex h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm transition-colors ${
                  section === item.id
                    ? 'bg-[var(--color-surface-selected)] font-semibold text-[var(--color-text-primary)]'
                    : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)]'
                }`}
              >
                <Icon size={17} />
                {item.label}
              </button>
            )
          })}
        </nav>

        <div className="min-w-0 flex-1 overflow-y-auto p-6">
          {error ? (
            <div className="mb-4 rounded-lg border border-[var(--color-error-container)] bg-[var(--color-error-container)] px-3 py-2 text-sm text-[var(--color-on-error-container)]">
              {error}
            </div>
          ) : null}
          {temporaryPassword ? (
            <div className="mb-4 rounded-lg border border-[var(--color-warning-container)] bg-[var(--color-warning-container)] px-3 py-2 text-sm text-[var(--color-text-primary)]">
              Temporary password: <span className="font-mono font-semibold">{temporaryPassword}</span>
            </div>
          ) : null}

          {section === 'users' && (
            <section className="space-y-5">
              <form onSubmit={handleCreateUser} className="grid gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-container-lowest)] p-4 md:grid-cols-[1fr_1fr_140px_150px_auto]">
                <input className="admin-input" placeholder="Email" value={newUser.email} onChange={(e) => setNewUser((s) => ({ ...s, email: e.target.value }))} />
                <input className="admin-input" placeholder="Display name" value={newUser.displayName} onChange={(e) => setNewUser((s) => ({ ...s, displayName: e.target.value }))} />
                <select className="admin-input" value={newUser.role} onChange={(e) => setNewUser((s) => ({ ...s, role: e.target.value as EnterpriseRole }))}>
                  <option value="member">member</option>
                  <option value="admin">admin</option>
                </select>
                <input className="admin-input" placeholder="Daily limit" value={newUser.dailyTokenLimit} onChange={(e) => setNewUser((s) => ({ ...s, dailyTokenLimit: e.target.value }))} />
                <button className="admin-primary-button" type="submit">
                  <UserPlus size={16} />
                  Create
                </button>
              </form>

              <div className="overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-container-lowest)]">
                <table className="w-full min-w-[780px] text-left text-sm">
                  <thead className="bg-[var(--color-surface-container)] text-xs uppercase text-[var(--color-text-tertiary)]">
                    <tr>
                      <th className="px-4 py-3">User</th>
                      <th className="px-4 py-3">Role</th>
                      <th className="px-4 py-3">Status</th>
                      <th className="px-4 py-3">Today</th>
                      <th className="px-4 py-3">Daily limit</th>
                      <th className="px-4 py-3">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.map((user) => (
                      <tr key={user.id} className="border-t border-[var(--color-border)]">
                        <td className="px-4 py-3">
                          <div className="font-medium">{user.displayName}</div>
                          <div className="text-xs text-[var(--color-text-tertiary)]">{user.email}</div>
                        </td>
                        <td className="px-4 py-3">
                          <select className="admin-input h-9" value={user.role} onChange={(e) => void handleUpdateUser(user.id, { role: e.target.value as EnterpriseRole })}>
                            <option value="member">member</option>
                            <option value="admin">admin</option>
                          </select>
                        </td>
                        <td className="px-4 py-3">
                          <button
                            type="button"
                            className={`rounded-md px-2.5 py-1 text-xs font-semibold ${user.status === 'active' ? 'bg-[var(--color-success-container)] text-[var(--color-success)]' : 'bg-[var(--color-error-container)] text-[var(--color-on-error-container)]'}`}
                            onClick={() => void handleUpdateUser(user.id, { status: user.status === 'active' ? 'disabled' : 'active' })}
                          >
                            {user.status}
                          </button>
                        </td>
                        <td className="px-4 py-3">{formatTokens(usageByUser.get(user.id)?.totalTokens ?? 0)}</td>
                        <td className="px-4 py-3">
                          <DailyLimitInput user={user} onSave={(limit) => handleUpdateUser(user.id, { dailyTokenLimit: limit })} />
                        </td>
                        <td className="px-4 py-3">
                          <button className="admin-secondary-button" type="button" onClick={() => void handleResetPassword(user.id)}>
                            Reset
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {section === 'usage' && (
            <SimpleTable
              columns={['Date', 'User', 'Role', 'Input', 'Output', 'Cache', 'Total', 'Limit']}
              rows={usage.map((entry) => [
                entry.date,
                `${entry.displayName} (${entry.email})`,
                entry.role,
                formatTokens(entry.inputTokens),
                formatTokens(entry.outputTokens),
                formatTokens(entry.cacheReadTokens + entry.cacheCreationTokens),
                formatTokens(entry.totalTokens),
                entry.dailyTokenLimit === null ? 'Unlimited' : formatTokens(entry.dailyTokenLimit),
              ])}
            />
          )}

          {section === 'logs' && (
            <SimpleTable
              columns={['Time', 'Event', 'Actor', 'Target', 'Details']}
              rows={auditEvents.map((event) => [
                new Date(event.timestamp).toLocaleString(),
                event.type,
                event.actorEmail ?? '-',
                event.targetUserId ?? '-',
                event.details ? JSON.stringify(event.details) : '-',
              ])}
            />
          )}

          {section === 'provider' && (
            <form onSubmit={handleSaveProvider} className="max-w-3xl space-y-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-container-lowest)] p-5">
              <div>
                <h2 className="text-base font-semibold">Unified enterprise provider</h2>
                <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
                  Members use this active provider automatically. API keys are never shown back in full.
                </p>
              </div>
              {provider?.apiKeyPreview ? (
                <div className="rounded-md bg-[var(--color-surface-container)] px-3 py-2 text-sm text-[var(--color-text-secondary)]">
                  Current API key: {provider.apiKeyPreview}
                </div>
              ) : null}
              <div className="grid gap-3 md:grid-cols-2">
                <Field label="Name" value={providerForm.name} onChange={(value) => setProviderForm((s) => ({ ...s, name: value }))} />
                <label className="admin-field">
                  API format
                  <select className="admin-input" value={providerForm.apiFormat} onChange={(e) => setProviderForm((s) => ({ ...s, apiFormat: e.target.value as EnterpriseProviderInput['apiFormat'] }))}>
                    <option value="anthropic">Anthropic compatible</option>
                    <option value="openai_chat">OpenAI Chat</option>
                    <option value="openai_responses">OpenAI Responses</option>
                  </select>
                </label>
                <Field label="Base URL" value={providerForm.baseUrl} onChange={(value) => setProviderForm((s) => ({ ...s, baseUrl: value }))} />
                <Field label="API key" value={providerForm.apiKey || ''} type="password" onChange={(value) => setProviderForm((s) => ({ ...s, apiKey: value }))} placeholder={provider ? 'Leave blank to keep current key' : ''} />
                {(['main', 'haiku', 'sonnet', 'opus'] as const).map((key) => (
                  <Field
                    key={key}
                    label={`${key} model`}
                    value={providerForm.models[key]}
                    onChange={(value) => setProviderForm((s) => ({ ...s, models: { ...s.models, [key]: value } }))}
                  />
                ))}
              </div>
              <button className="admin-primary-button" type="submit">
                <Save size={16} />
                Save provider
              </button>
            </form>
          )}

          {section === 'version' && (
            <form onSubmit={handleSaveVersionPolicy} className="max-w-2xl space-y-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-container-lowest)] p-5">
              <div>
                <h2 className="text-base font-semibold">Version push policy</h2>
                <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
                  Current policy target: {versionPolicy?.targetVersion ?? 'none'}
                </p>
              </div>
              <Field label="Target version" value={versionForm.targetVersion} onChange={(value) => setVersionForm((s) => ({ ...s, targetVersion: value }))} />
              <label className="admin-field">
                Message
                <textarea className="admin-input min-h-[96px] py-2" value={versionForm.message} onChange={(e) => setVersionForm((s) => ({ ...s, message: e.target.value }))} />
              </label>
              <label className="flex items-center gap-2 text-sm text-[var(--color-text-secondary)]">
                <input type="checkbox" checked={versionForm.force} onChange={(e) => setVersionForm((s) => ({ ...s, force: e.target.checked }))} />
                Force this version for enterprise users
              </label>
              <button className="admin-primary-button" type="submit">
                <Save size={16} />
                Save policy
              </button>
            </form>
          )}
        </div>
      </div>
    </div>
  )
}

function DailyLimitInput({
  user,
  onSave,
}: {
  user: EnterpriseUser
  onSave: (limit: number | null) => Promise<void>
}) {
  const [value, setValue] = useState(user.dailyTokenLimit === null ? '' : String(user.dailyTokenLimit))
  useEffect(() => {
    setValue(user.dailyTokenLimit === null ? '' : String(user.dailyTokenLimit))
  }, [user.dailyTokenLimit])
  return (
    <div className="flex items-center gap-2">
      <input className="admin-input h-9 w-28" value={value} placeholder="Unlimited" onChange={(e) => setValue(e.target.value)} />
      <button className="admin-secondary-button" type="button" onClick={() => void onSave(parseDailyLimit(value))}>
        Save
      </button>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
  type = 'text',
  placeholder,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  type?: string
  placeholder?: string
}) {
  return (
    <label className="admin-field">
      {label}
      <input className="admin-input" value={value} type={type} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}

function SimpleTable({ columns, rows }: { columns: string[]; rows: string[][] }) {
  return (
    <div className="overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-container-lowest)]">
      <table className="w-full min-w-[760px] text-left text-sm">
        <thead className="bg-[var(--color-surface-container)] text-xs uppercase text-[var(--color-text-tertiary)]">
          <tr>{columns.map((column) => <th key={column} className="px-4 py-3">{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr><td className="px-4 py-6 text-[var(--color-text-secondary)]" colSpan={columns.length}>No records</td></tr>
          ) : rows.map((row, rowIndex) => (
            <tr key={rowIndex} className="border-t border-[var(--color-border)]">
              {row.map((cell, cellIndex) => <td key={cellIndex} className="max-w-[360px] truncate px-4 py-3">{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function parseDailyLimit(value: string): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : null
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value)
}
