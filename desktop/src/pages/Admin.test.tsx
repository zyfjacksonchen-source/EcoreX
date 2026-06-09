import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { Admin } from './Admin'
import { enterpriseApi } from '../api/enterprise'
import { useEnterpriseStore } from '../stores/enterpriseStore'

vi.mock('../api/enterprise', () => ({
  enterpriseApi: {
    listUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    resetPassword: vi.fn(),
    usage: vi.fn(),
    auditLog: vi.fn(),
    provider: vi.fn(),
    saveProvider: vi.fn(),
    versionPolicy: vi.fn(),
    updateVersionPolicy: vi.fn(),
  },
}))

const adminUser = enterpriseUser({
  id: 'admin-1',
  email: 'admin@ecorex.local',
  displayName: 'Admin',
  role: 'admin',
})

const memberUser = enterpriseUser({
  id: 'member-1',
  email: 'buyer@example.com',
  displayName: 'Media Buyer',
  role: 'member',
  dailyTokenLimit: 2000,
})

describe('Admin page', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockAdminData()
    useEnterpriseStore.setState({
      bootstrap: null,
      token: 'ecx-admin',
      user: adminUser,
      isLoading: false,
      error: null,
      logout: vi.fn(),
    })
  })

  afterEach(() => {
    cleanup()
  })

  it('renders admin-only enterprise management data and provider governance', async () => {
    render(<Admin />)

    expect(await screen.findByText('Media Buyer')).toBeInTheDocument()
    expect(screen.getByText('buyer@example.com')).toBeInTheDocument()
    expect(screen.getByText('1,500')).toBeInTheDocument()
    expect(screen.getByDisplayValue('2000')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Provider' }))

    expect(await screen.findByText('Unified enterprise provider')).toBeInTheDocument()
    expect(screen.getByText('Current API key: sk-e...1234')).toBeInTheDocument()
    expect(screen.getByDisplayValue('https://api.example.com/anthropic')).toBeInTheDocument()
  })

  it('creates users with parsed daily token limits and shows the temporary password', async () => {
    vi.mocked(enterpriseApi.createUser).mockResolvedValue({
      user: enterpriseUser({
        id: 'member-2',
        email: 'planner@example.com',
        displayName: 'Planner',
        role: 'member',
        dailyTokenLimit: 5000,
      }),
      temporaryPassword: 'EcoreX@2026!Temp',
    })

    render(<Admin />)
    await screen.findByText('Media Buyer')

    fireEvent.change(screen.getByPlaceholderText('Email'), {
      target: { value: 'planner@example.com' },
    })
    fireEvent.change(screen.getByPlaceholderText('Display name'), {
      target: { value: 'Planner' },
    })
    fireEvent.change(screen.getByPlaceholderText('Daily limit'), {
      target: { value: '5000' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Create/ }))

    await waitFor(() => {
      expect(enterpriseApi.createUser).toHaveBeenCalledWith({
        email: 'planner@example.com',
        displayName: 'Planner',
        role: 'member',
        dailyTokenLimit: 5000,
      })
    })
    expect(await screen.findByText(/Temporary password:/)).toHaveTextContent('EcoreX@2026!Temp')
  })

  it('blocks non-admin users from enterprise management', () => {
    useEnterpriseStore.setState({
      token: 'ecx-member',
      user: { ...memberUser, role: 'member' },
    })

    render(<Admin />)

    expect(screen.getByRole('heading', { name: 'Admin access required' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Provider' })).not.toBeInTheDocument()
    expect(enterpriseApi.listUsers).not.toHaveBeenCalled()
  })
})

function mockAdminData() {
  vi.mocked(enterpriseApi.listUsers).mockResolvedValue({ users: [adminUser, memberUser] })
  vi.mocked(enterpriseApi.usage).mockResolvedValue({
    usage: [{
      userId: 'member-1',
      email: 'buyer@example.com',
      displayName: 'Media Buyer',
      role: 'member',
      date: todayKey(),
      inputTokens: 700,
      outputTokens: 500,
      cacheReadTokens: 200,
      cacheCreationTokens: 100,
      totalTokens: 1500,
      dailyTokenLimit: 2000,
    }],
  })
  vi.mocked(enterpriseApi.auditLog).mockResolvedValue({
    events: [{
      id: 'audit-1',
      timestamp: '2026-06-09T00:00:00.000Z',
      type: 'user.created',
      actorUserId: 'admin-1',
      actorEmail: 'admin@ecorex.local',
      targetUserId: 'member-1',
      details: { role: 'member' },
    }],
  })
  vi.mocked(enterpriseApi.provider).mockResolvedValue({
    activeId: 'provider-1',
    provider: {
      id: 'provider-1',
      presetId: 'custom',
      name: 'EcoreX Enterprise Provider',
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
      hasApiKey: true,
      apiKeyPreview: 'sk-e...1234',
    },
    providers: [],
    presets: [],
  })
  vi.mocked(enterpriseApi.versionPolicy).mockResolvedValue({
    policy: {
      targetVersion: '0.1.1',
      message: 'Enterprise rollout',
      force: false,
      updatedAt: null,
      updatedBy: null,
    },
  })
}

function enterpriseUser(input: {
  id: string
  email: string
  displayName: string
  role: 'admin' | 'member'
  dailyTokenLimit?: number | null
}) {
  return {
    id: input.id,
    email: input.email,
    displayName: input.displayName,
    role: input.role,
    status: 'active' as const,
    mustChangePassword: false,
    dailyTokenLimit: input.dailyTokenLimit ?? null,
    permissions: {
      canUseAgent: true,
      canManageVersions: input.role === 'admin',
      allowedPermissionModes: ['default', 'plan', 'acceptEdits'],
    },
    createdAt: '2026-06-09T00:00:00.000Z',
    updatedAt: '2026-06-09T00:00:00.000Z',
  }
}

function todayKey(): string {
  const today = new Date()
  return `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
}
