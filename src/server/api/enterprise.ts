import { z } from 'zod'
import { ApiError, errorResponse } from '../middleware/errorHandler.js'
import {
  EnterpriseService,
  ENTERPRISE_PROVIDER_NAME,
  type EnterpriseSessionContext,
} from '../services/enterpriseService.js'
import { ProviderService } from '../services/providerService.js'
import { PROVIDER_PRESETS } from '../config/providerPresets.js'
import {
  ApiFormatSchema,
  ProviderAuthStrategySchema,
  ProviderRuntimeKindSchema,
  ModelMappingSchema,
  type SavedProvider,
} from '../types/provider.js'

const enterpriseService = new EnterpriseService()
const providerService = new ProviderService()

const LoginSchema = z.object({
  email: z.string().email(),
  password: z.string().min(1),
})

const ChangePasswordSchema = z.object({
  currentPassword: z.string().min(1),
  newPassword: z.string().min(12),
})

const UserCreateSchema = z.object({
  email: z.string().email(),
  displayName: z.string().optional(),
  role: z.enum(['admin', 'member']).default('member'),
  password: z.string().min(12).optional(),
  dailyTokenLimit: z.number().int().min(0).nullable().optional(),
  permissions: z.object({
    canUseAgent: z.boolean().optional(),
    canManageVersions: z.boolean().optional(),
    allowedPermissionModes: z.array(z.string()).optional(),
  }).optional(),
})

const UserUpdateSchema = z.object({
  displayName: z.string().optional(),
  role: z.enum(['admin', 'member']).optional(),
  status: z.enum(['active', 'disabled']).optional(),
  dailyTokenLimit: z.number().int().min(0).nullable().optional(),
  permissions: z.object({
    canUseAgent: z.boolean().optional(),
    canManageVersions: z.boolean().optional(),
    allowedPermissionModes: z.array(z.string()).optional(),
  }).optional(),
})

const ResetPasswordSchema = z.object({
  password: z.string().min(12).optional(),
})

const EnterpriseProviderSchema = z.object({
  id: z.string().optional(),
  presetId: z.string().min(1).default('custom'),
  name: z.string().min(1).default(ENTERPRISE_PROVIDER_NAME),
  apiKey: z.string().optional(),
  authStrategy: ProviderAuthStrategySchema.optional(),
  baseUrl: z.string().min(1),
  apiFormat: ApiFormatSchema.default('anthropic'),
  runtimeKind: ProviderRuntimeKindSchema.default('anthropic_compatible'),
  models: ModelMappingSchema,
  autoCompactWindow: z.number().int().min(16000).max(10000000).optional(),
  modelContextWindows: z.record(z.string().min(1), z.number().int().min(16000).max(10000000)).optional(),
  notes: z.string().optional(),
})

const VersionPolicySchema = z.object({
  targetVersion: z.string().nullable().optional(),
  message: z.string().optional(),
  force: z.boolean().optional(),
})

export async function handleEnterpriseApi(
  req: Request,
  url: URL,
  segments: string[],
): Promise<Response> {
  try {
    const resource = segments[2]
    const id = segments[3]
    const action = segments[4]

    if (resource === 'auth') {
      return await handleEnterpriseAuth(req, id)
    }

    if (resource === 'users') {
      const admin = await enterpriseService.requireAdmin(req)
      return await handleEnterpriseUsers(req, id, action, admin)
    }

    if (resource === 'usage') {
      await enterpriseService.requireAdmin(req)
      if (req.method !== 'GET') throw methodNotAllowed(req.method)
      return Response.json({ usage: await enterpriseService.getUsageSummary() })
    }

    if (resource === 'audit-log') {
      await enterpriseService.requireAdmin(req)
      if (req.method !== 'GET') throw methodNotAllowed(req.method)
      const limit = Number(url.searchParams.get('limit') || '200')
      return Response.json({ events: await enterpriseService.readAuditLog(limit) })
    }

    if (resource === 'provider') {
      const admin = await enterpriseService.requireAdmin(req)
      return await handleEnterpriseProvider(req, id, admin)
    }

    if (resource === 'version-policy') {
      const admin = await enterpriseService.requireAdmin(req)
      return await handleVersionPolicy(req, admin)
    }

    throw ApiError.notFound(`Unknown enterprise resource: ${resource || 'enterprise'}`)
  } catch (error) {
    if (error instanceof z.ZodError) {
      return errorResponse(ApiError.badRequest(error.issues.map((issue) => issue.message).join('; ')))
    }
    return errorResponse(error)
  }
}

async function handleEnterpriseAuth(req: Request, action?: string): Promise<Response> {
  if (action === 'bootstrap' && req.method === 'GET') {
    return Response.json(await enterpriseService.getBootstrapStatus())
  }

  if (action === 'login' && req.method === 'POST') {
    const input = LoginSchema.parse(await parseJsonBody(req))
    return Response.json(await enterpriseService.login(input.email, input.password))
  }

  if (action === 'logout' && req.method === 'POST') {
    await enterpriseService.logout(req)
    return Response.json({ ok: true })
  }

  if (action === 'me' && req.method === 'GET') {
    const context = await enterpriseService.requireUser(req)
    return Response.json({ user: context.user })
  }

  if (action === 'password' && req.method === 'PUT') {
    const context = await enterpriseService.requireUser(req)
    const input = ChangePasswordSchema.parse(await parseJsonBody(req))
    const user = await enterpriseService.changePassword(
      context,
      input.currentPassword,
      input.newPassword,
    )
    return Response.json({ user })
  }

  throw methodNotAllowed(req.method)
}

async function handleEnterpriseUsers(
  req: Request,
  userId: string | undefined,
  action: string | undefined,
  admin: EnterpriseSessionContext,
): Promise<Response> {
  if (!userId) {
    if (req.method === 'GET') {
      return Response.json({ users: await enterpriseService.listUsers() })
    }
    if (req.method === 'POST') {
      const input = UserCreateSchema.parse(await parseJsonBody(req))
      const result = await enterpriseService.createUser(admin, input)
      return Response.json(result, { status: 201 })
    }
    throw methodNotAllowed(req.method)
  }

  if (action === 'reset-password') {
    if (req.method !== 'POST') throw methodNotAllowed(req.method)
    const input = ResetPasswordSchema.parse(await parseJsonBody(req))
    return Response.json(await enterpriseService.resetPassword(admin, userId, input.password))
  }

  if (req.method === 'PUT') {
    const input = UserUpdateSchema.parse(await parseJsonBody(req))
    return Response.json({ user: await enterpriseService.updateUser(admin, userId, input) })
  }

  throw methodNotAllowed(req.method)
}

async function handleEnterpriseProvider(
  req: Request,
  action: string | undefined,
  admin: EnterpriseSessionContext,
): Promise<Response> {
  if (!action) {
    if (req.method === 'GET') {
      const { providers, activeId } = await providerService.listProviders()
      const activeProvider = activeId
        ? providers.find((provider) => provider.id === activeId) ?? null
        : null
      return Response.json({
        activeId,
        provider: activeProvider ? sanitizeProvider(activeProvider) : null,
        providers: providers.map(sanitizeProvider),
        presets: PROVIDER_PRESETS,
      })
    }

    if (req.method === 'PUT') {
      const input = EnterpriseProviderSchema.parse(await parseJsonBody(req))
      const { providers, activeId } = await providerService.listProviders()
      const targetId =
        input.id ||
        (activeId && providers.some((provider) => provider.id === activeId) ? activeId : null)

      if (targetId) {
        const updated = await providerService.updateProvider(targetId, {
          name: input.name,
          ...(input.apiKey !== undefined ? { apiKey: input.apiKey } : {}),
          ...(input.authStrategy !== undefined ? { authStrategy: input.authStrategy } : {}),
          baseUrl: input.baseUrl,
          apiFormat: input.apiFormat,
          runtimeKind: input.runtimeKind,
          models: input.models,
          ...(input.autoCompactWindow !== undefined ? { autoCompactWindow: input.autoCompactWindow } : {}),
          ...(input.modelContextWindows !== undefined ? { modelContextWindows: input.modelContextWindows } : {}),
          ...(input.notes !== undefined ? { notes: input.notes } : {}),
        })
        await providerService.activateProvider(updated.id)
        await enterpriseService.appendAuditLog('provider.updated', {
          actorUserId: admin.user.id,
          actorEmail: admin.user.email,
          details: {
            providerId: updated.id,
            providerName: updated.name,
            baseUrl: updated.baseUrl,
            apiFormat: updated.apiFormat,
            apiKeyChanged: input.apiKey !== undefined,
          },
        })
        return Response.json({ provider: sanitizeProvider(updated), activeId: updated.id })
      }

      if (!input.apiKey && input.runtimeKind !== 'openai_oauth') {
        throw ApiError.badRequest('API key is required when creating the enterprise provider')
      }
      const created = await providerService.addProvider({
        presetId: input.presetId,
        name: input.name,
        apiKey: input.apiKey ?? '',
        ...(input.authStrategy !== undefined ? { authStrategy: input.authStrategy } : {}),
        baseUrl: input.baseUrl,
        apiFormat: input.apiFormat,
        runtimeKind: input.runtimeKind,
        models: input.models,
        ...(input.autoCompactWindow !== undefined ? { autoCompactWindow: input.autoCompactWindow } : {}),
        ...(input.modelContextWindows !== undefined ? { modelContextWindows: input.modelContextWindows } : {}),
        ...(input.notes !== undefined ? { notes: input.notes } : {}),
      })
      await providerService.activateProvider(created.id)
      await enterpriseService.appendAuditLog('provider.created', {
        actorUserId: admin.user.id,
        actorEmail: admin.user.email,
        details: {
          providerId: created.id,
          providerName: created.name,
          baseUrl: created.baseUrl,
          apiFormat: created.apiFormat,
          apiKeyChanged: true,
        },
      })
      return Response.json({ provider: sanitizeProvider(created), activeId: created.id }, { status: 201 })
    }

    throw methodNotAllowed(req.method)
  }

  if (action === 'test' && req.method === 'POST') {
    const input = EnterpriseProviderSchema.parse(await parseJsonBody(req))
    if (!input.apiKey) throw ApiError.badRequest('API key is required to test a provider')
    const result = await providerService.testProviderConfig({
      baseUrl: input.baseUrl,
      apiKey: input.apiKey,
      modelId: input.models.main,
      ...(input.authStrategy !== undefined ? { authStrategy: input.authStrategy } : {}),
      apiFormat: input.apiFormat,
    })
    return Response.json({ result })
  }

  throw methodNotAllowed(req.method)
}

async function handleVersionPolicy(
  req: Request,
  admin: EnterpriseSessionContext,
): Promise<Response> {
  if (req.method === 'GET') {
    return Response.json({ policy: await enterpriseService.getVersionPolicy() })
  }
  if (req.method === 'PUT') {
    const input = VersionPolicySchema.parse(await parseJsonBody(req))
    return Response.json({ policy: await enterpriseService.updateVersionPolicy(admin, input) })
  }
  throw methodNotAllowed(req.method)
}

function sanitizeProvider(provider: SavedProvider): Record<string, unknown> {
  return {
    id: provider.id,
    presetId: provider.presetId,
    name: provider.name,
    authStrategy: provider.authStrategy,
    baseUrl: provider.baseUrl,
    apiFormat: provider.apiFormat,
    runtimeKind: provider.runtimeKind,
    models: provider.models,
    autoCompactWindow: provider.autoCompactWindow,
    modelContextWindows: provider.modelContextWindows,
    notes: provider.notes,
    hasApiKey: provider.apiKey.length > 0,
    apiKeyPreview: maskApiKey(provider.apiKey),
  }
}

function maskApiKey(apiKey: string): string {
  if (!apiKey) return ''
  if (apiKey.length <= 8) return '****'
  return `${apiKey.slice(0, 4)}...${apiKey.slice(-4)}`
}

async function parseJsonBody(req: Request): Promise<Record<string, unknown>> {
  try {
    return (await req.json()) as Record<string, unknown>
  } catch {
    throw ApiError.badRequest('Invalid JSON body')
  }
}

function methodNotAllowed(method: string): ApiError {
  return new ApiError(405, `Method ${method} not allowed`, 'METHOD_NOT_ALLOWED')
}
