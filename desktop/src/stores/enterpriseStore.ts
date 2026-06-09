import { create } from 'zustand'
import { setAuthToken } from '../api/client'
import { enterpriseApi, type EnterpriseBootstrapStatus, type EnterpriseUser } from '../api/enterprise'

const ENTERPRISE_TOKEN_KEY = 'ecorex-enterprise-token'

type EnterpriseStore = {
  bootstrap: EnterpriseBootstrapStatus | null
  token: string | null
  user: EnterpriseUser | null
  isLoading: boolean
  error: string | null
  initialize: () => Promise<void>
  login: (email: string, password: string) => Promise<EnterpriseUser>
  logout: () => Promise<void>
  changePassword: (currentPassword: string, newPassword: string) => Promise<EnterpriseUser>
  setUser: (user: EnterpriseUser | null) => void
}

function readStoredToken(): string | null {
  try {
    const token = localStorage.getItem(ENTERPRISE_TOKEN_KEY)
    return token?.trim() || null
  } catch {
    return null
  }
}

function storeToken(token: string | null) {
  setAuthToken(token)
  try {
    if (token) {
      localStorage.setItem(ENTERPRISE_TOKEN_KEY, token)
    } else {
      localStorage.removeItem(ENTERPRISE_TOKEN_KEY)
    }
  } catch {
    // localStorage is best-effort; the in-memory token still covers this run.
  }
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message.trim() ? error.message : fallback
}

export const useEnterpriseStore = create<EnterpriseStore>((set, get) => ({
  bootstrap: null,
  token: null,
  user: null,
  isLoading: false,
  error: null,

  initialize: async () => {
    set({ isLoading: true, error: null })
    try {
      const bootstrap = await enterpriseApi.bootstrap()
      const token = readStoredToken()
      if (!token) {
        storeToken(null)
        set({ bootstrap, token: null, user: null, isLoading: false, error: null })
        return
      }

      storeToken(token)
      try {
        const { user } = await enterpriseApi.me()
        set({ bootstrap, token, user, isLoading: false, error: null })
      } catch {
        storeToken(null)
        set({ bootstrap, token: null, user: null, isLoading: false, error: null })
      }
    } catch (error) {
      const message = errorMessage(error, 'Failed to initialize EcoreX enterprise login')
      set({ isLoading: false, error: message })
      throw error
    }
  },

  login: async (email, password) => {
    set({ isLoading: true, error: null })
    try {
      const { token, user } = await enterpriseApi.login(email, password)
      storeToken(token)
      set({ token, user, isLoading: false, error: null })
      return user
    } catch (error) {
      const message = errorMessage(error, 'Failed to sign in')
      storeToken(null)
      set({ token: null, user: null, isLoading: false, error: message })
      throw error
    }
  },

  logout: async () => {
    const token = get().token
    try {
      if (token) await enterpriseApi.logout()
    } finally {
      storeToken(null)
      set({ token: null, user: null })
    }
  },

  changePassword: async (currentPassword, newPassword) => {
    set({ isLoading: true, error: null })
    try {
      const { user } = await enterpriseApi.changePassword(currentPassword, newPassword)
      set({ user, isLoading: false, error: null })
      return user
    } catch (error) {
      const message = errorMessage(error, 'Failed to change password')
      set({ isLoading: false, error: message })
      throw error
    }
  },

  setUser: (user) => set({ user }),
}))
