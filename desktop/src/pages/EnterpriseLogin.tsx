import { useState, type FormEvent } from 'react'
import { Eye, EyeOff, Moon, Sun } from 'lucide-react'
import { useEnterpriseStore } from '../stores/enterpriseStore'
import { useUIStore } from '../stores/uiStore'
import { publicAssetPath } from '../lib/publicAsset'

type EnterpriseLoginProps = {
  onAuthenticated: () => void
}

export function EnterpriseLogin({ onAuthenticated }: EnterpriseLoginProps) {
  const bootstrap = useEnterpriseStore((s) => s.bootstrap)
  const user = useEnterpriseStore((s) => s.user)
  const login = useEnterpriseStore((s) => s.login)
  const changePassword = useEnterpriseStore((s) => s.changePassword)
  const isLoading = useEnterpriseStore((s) => s.isLoading)
  const storeError = useEnterpriseStore((s) => s.error)
  const theme = useUIStore((s) => s.theme)
  const toggleTheme = useUIStore((s) => s.toggleTheme)
  const [email, setEmail] = useState(bootstrap?.defaultAdminEmail || 'admin@ecorex.local')
  const [password, setPassword] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const mustChangePassword = Boolean(user?.mustChangePassword)

  const handleLogin = async (event: FormEvent) => {
    event.preventDefault()
    setLocalError(null)
    try {
      const nextUser = await login(email, password)
      if (nextUser.mustChangePassword) {
        setCurrentPassword(password)
        return
      }
      onAuthenticated()
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : 'Login failed')
    }
  }

  const handlePasswordChange = async (event: FormEvent) => {
    event.preventDefault()
    setLocalError(null)
    if (newPassword !== confirmPassword) {
      setLocalError('New passwords do not match.')
      return
    }
    try {
      const nextUser = await changePassword(currentPassword, newPassword)
      if (!nextUser.mustChangePassword) onAuthenticated()
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : 'Password change failed')
    }
  }

  return (
    <div className="app-shell-viewport flex min-h-0 bg-[var(--color-surface)] text-[var(--color-text-primary)]">
      <div className="flex flex-1 items-center justify-center px-5 py-8">
        <div className="w-full max-w-[440px]">
          <div className="mb-7 flex items-center justify-between">
            <div className="flex min-w-0 items-center gap-3">
              <img src={publicAssetPath('app-icon.png')} alt="" className="h-10 w-10 shrink-0" />
              <div className="min-w-0">
                <div className="text-[22px] font-bold leading-tight text-[var(--color-text-primary)]">EcoreX</div>
                <div className="truncate text-sm text-[var(--color-text-secondary)]">Advertising agency AI Agent by Yixin R&D</div>
              </div>
            </div>
            <button
              type="button"
              onClick={toggleTheme}
              className="inline-flex h-10 w-10 items-center justify-center rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-container-lowest)] text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)]"
              aria-label="Toggle theme"
              title="Toggle theme"
            >
              {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
            </button>
          </div>

          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-container-lowest)] p-6 shadow-[var(--shadow-dropdown)]">
            {!mustChangePassword ? (
              <form onSubmit={handleLogin} className="space-y-4">
                <div>
                  <h1 className="text-lg font-semibold">Enterprise sign in</h1>
                  <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
                    Use your EcoreX enterprise account to enter the workbench.
                  </p>
                </div>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-semibold uppercase text-[var(--color-text-tertiary)]">Email</span>
                  <input
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    className="h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-3 text-sm outline-none focus:border-[var(--color-border-focus)]"
                    autoComplete="email"
                  />
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-semibold uppercase text-[var(--color-text-tertiary)]">Password</span>
                  <div className="flex h-11 items-center rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] focus-within:border-[var(--color-border-focus)]">
                    <input
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      type={showPassword ? 'text' : 'password'}
                      className="min-w-0 flex-1 bg-transparent px-3 text-sm outline-none"
                      autoComplete="current-password"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((value) => !value)}
                      className="mr-1 inline-flex h-9 w-9 items-center justify-center rounded-md text-[var(--color-text-tertiary)] hover:bg-[var(--color-surface-hover)]"
                      aria-label={showPassword ? 'Hide password' : 'Show password'}
                    >
                      {showPassword ? <EyeOff size={17} /> : <Eye size={17} />}
                    </button>
                  </div>
                </label>
                {(localError || storeError) && (
                  <div className="rounded-md border border-[var(--color-error-container)] bg-[var(--color-error-container)] px-3 py-2 text-sm text-[var(--color-on-error-container)]">
                    {localError || storeError}
                  </div>
                )}
                <button
                  type="submit"
                  disabled={isLoading}
                  className="h-11 w-full rounded-lg bg-[var(--color-brand)] px-4 text-sm font-semibold text-[var(--color-btn-primary-fg)] shadow-[var(--shadow-button-primary)] transition-colors hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {isLoading ? 'Signing in...' : 'Sign in'}
                </button>
              </form>
            ) : (
              <form onSubmit={handlePasswordChange} className="space-y-4">
                <div>
                  <h1 className="text-lg font-semibold">Change bootstrap password</h1>
                  <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
                    The default administrator password must be changed before using EcoreX.
                  </p>
                </div>
                <PasswordField label="Current password" value={currentPassword} onChange={setCurrentPassword} />
                <PasswordField label="New password" value={newPassword} onChange={setNewPassword} />
                <PasswordField label="Confirm new password" value={confirmPassword} onChange={setConfirmPassword} />
                {(localError || storeError) && (
                  <div className="rounded-md border border-[var(--color-error-container)] bg-[var(--color-error-container)] px-3 py-2 text-sm text-[var(--color-on-error-container)]">
                    {localError || storeError}
                  </div>
                )}
                <button
                  type="submit"
                  disabled={isLoading}
                  className="h-11 w-full rounded-lg bg-[var(--color-brand)] px-4 text-sm font-semibold text-[var(--color-btn-primary-fg)] shadow-[var(--shadow-button-primary)] disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {isLoading ? 'Updating...' : 'Update password'}
                </button>
              </form>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function PasswordField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-semibold uppercase text-[var(--color-text-tertiary)]">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        type="password"
        className="h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-3 text-sm outline-none focus:border-[var(--color-border-focus)]"
        autoComplete="new-password"
      />
    </label>
  )
}
