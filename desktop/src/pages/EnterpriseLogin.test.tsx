import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { EnterpriseLogin } from './EnterpriseLogin'
import { enterpriseApi } from '../api/enterprise'
import { setAuthToken } from '../api/client'
import { useEnterpriseStore } from '../stores/enterpriseStore'
import { useUIStore } from '../stores/uiStore'

vi.mock('../api/client', () => ({
  setAuthToken: vi.fn(),
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

vi.mock('../api/enterprise', () => ({
  enterpriseApi: {
    bootstrap: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
    me: vi.fn(),
    changePassword: vi.fn(),
  },
}))

const adminUser = {
  id: 'admin-1',
  email: 'admin@ecorex.local',
  displayName: 'EcoreX Administrator',
  role: 'admin' as const,
  status: 'active' as const,
  mustChangePassword: true,
  dailyTokenLimit: null,
  permissions: {
    canUseAgent: true,
    canManageVersions: true,
    allowedPermissionModes: ['default', 'plan', 'acceptEdits', 'bypassPermissions'],
  },
  createdAt: '2026-06-09T00:00:00.000Z',
  updatedAt: '2026-06-09T00:00:00.000Z',
}

describe('EnterpriseLogin', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    useEnterpriseStore.setState({
      bootstrap: {
        initialized: true,
        defaultAdminEmail: 'admin@ecorex.local',
        mustChangePassword: true,
        usersCount: 1,
      },
      token: null,
      user: null,
      isLoading: false,
      error: null,
    })
    useUIStore.setState({ theme: 'light' })
  })

  afterEach(() => {
    cleanup()
  })

  it('forces the bootstrap administrator to change the default password before entering the workbench', async () => {
    vi.mocked(enterpriseApi.login).mockResolvedValue({
      token: 'ecx-token',
      user: adminUser,
    })
    vi.mocked(enterpriseApi.changePassword).mockResolvedValue({
      user: { ...adminUser, mustChangePassword: false },
    })
    const onAuthenticated = vi.fn()

    render(<EnterpriseLogin onAuthenticated={onAuthenticated} />)

    expect(screen.getByText('EcoreX')).toBeInTheDocument()
    expect(screen.getByDisplayValue('admin@ecorex.local')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'EcoreX@2026!ChangeMe' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('heading', { name: 'Change bootstrap password' })).toBeInTheDocument()
    expect(setAuthToken).toHaveBeenCalledWith('ecx-token')

    fireEvent.change(screen.getByLabelText('New password'), {
      target: { value: 'EcoreX@2026!Changed' },
    })
    fireEvent.change(screen.getByLabelText('Confirm new password'), {
      target: { value: 'EcoreX@2026!Changed' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Update password' }))

    await waitFor(() => {
      expect(enterpriseApi.changePassword).toHaveBeenCalledWith(
        'EcoreX@2026!ChangeMe',
        'EcoreX@2026!Changed',
      )
      expect(onAuthenticated).toHaveBeenCalledTimes(1)
    })
  })

  it('keeps users on the password-change form when confirmation does not match', async () => {
    vi.mocked(enterpriseApi.login).mockResolvedValue({
      token: 'ecx-token',
      user: adminUser,
    })

    render(<EnterpriseLogin onAuthenticated={vi.fn()} />)

    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'EcoreX@2026!ChangeMe' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    await screen.findByRole('heading', { name: 'Change bootstrap password' })
    fireEvent.change(screen.getByLabelText('New password'), {
      target: { value: 'EcoreX@2026!Changed' },
    })
    fireEvent.change(screen.getByLabelText('Confirm new password'), {
      target: { value: 'EcoreX@2026!Different' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Update password' }))

    expect(await screen.findByText('New passwords do not match.')).toBeInTheDocument()
    expect(enterpriseApi.changePassword).not.toHaveBeenCalled()
  })
})
