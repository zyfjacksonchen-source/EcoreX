const DEFAULT_CHAT_ENDPOINT = '/chat/completions';
const DEFAULT_RESPONSES_ENDPOINT = '/responses';
const DEFAULT_IMAGE_ENDPOINT = '/images/generations';
const DEFAULT_IMAGE_MODEL = 'gpt-image-2';
const DEFAULT_TIMEOUT_MS = 15 * 1000;
const MIN_TIMEOUT_MS = 1000;
const MAX_TIMEOUT_MS = 60 * 1000;
const DEFAULT_RETRIES = 1;
const MAX_RETRIES = 3;
const DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024;
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

function clampInteger(value, fallback, min, max) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(Math.max(Math.floor(number), min), max);
}

function normalizeBaseUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) throw new Error('Invalid baseUrl.');
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error('Invalid baseUrl.');
  }
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Invalid baseUrl.');
  if (url.username || url.password || url.hash) throw new Error('Invalid baseUrl.');
  url.search = '';
  return url.toString().replace(/\/+$/, '');
}

function endpointUrl(baseUrl, endpointPath) {
  const url = new URL(normalizeBaseUrl(baseUrl));
  const endpoint = String(endpointPath || '').replace(/^\/+/, '');
  let basePath = url.pathname.replace(/\/+$/, '');
  if (!basePath) basePath = '/v1';
  basePath = basePath.replace(/\/(chat\/completions|responses|images\/generations)$/i, '');
  if (!basePath) basePath = '/v1';
  url.pathname = `${basePath}/${endpoint}`.replace(/\/{2,}/g, '/');
  return url.toString();
}

function makeSecretList(values = []) {
  return [...new Set(values.map((value) => String(value || '').trim()).filter((value) => value.length >= 8))]
    .sort((a, b) => b.length - a.length)
    .slice(0, 30);
}

function redactText(value = '', secrets = []) {
  let text = String(value || '')
    .replace(/(authorization|cookie|token|api[_-]?key|auth[_-]?token)\s*[:=]\s*["']?[^"',\s]+/gi, '$1=[REDACTED]')
    .replace(/(bearer)\s+[a-z0-9._~+/-]+/gi, '$1 [REDACTED]')
    .replace(/(sk-[a-zA-Z0-9_-]{12,})/g, '[REDACTED_API_KEY]')
    .replace(/(sk-ant-[a-zA-Z0-9_-]{12,})/g, '[REDACTED_API_KEY]')
    .replace(/(ghp_[a-zA-Z0-9_]{20,})/g, '[REDACTED_TOKEN]')
    .replace(/[a-f0-9]{64}/gi, '[REDACTED_TOKEN]');
  for (const secret of makeSecretList(secrets)) {
    text = text.split(secret).join('[REDACTED_SECRET]');
  }
  return text;
}

function redactValue(value, secrets = []) {
  if (typeof value === 'string') return redactText(value, secrets);
  if (!value || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map((item) => redactValue(item, secrets));
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      /(token|secret|password|api[_-]?key|authorization|cookie)/i.test(key)
        ? '[REDACTED]'
        : redactValue(item, secrets)
    ])
  );
}

function limitText(value = '', limit = DEFAULT_MAX_RESPONSE_BYTES) {
  const text = String(value || '');
  const maxBytes = clampInteger(limit, DEFAULT_MAX_RESPONSE_BYTES, 1024, MAX_RESPONSE_BYTES);
  const bytes = Buffer.byteLength(text);
  if (bytes <= maxBytes) return { text, bytes, truncated: false };
  const buffer = Buffer.from(text);
  return {
    text: `${buffer.subarray(0, maxBytes).toString('utf8')}\n[response truncated to ${maxBytes} bytes]`,
    bytes,
    truncated: true
  };
}

async function readLimitedText(response, maxBytes) {
  const limit = clampInteger(maxBytes, DEFAULT_MAX_RESPONSE_BYTES, 1024, MAX_RESPONSE_BYTES);
  if (!response.body || typeof response.body.getReader !== 'function') {
    return limitText(await response.text(), limit);
  }

  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  let truncated = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = Buffer.from(value);
      if (total + chunk.length > limit) {
        chunks.push(chunk.subarray(0, Math.max(0, limit - total)));
        total += chunk.length;
        truncated = true;
        await reader.cancel();
        break;
      }
      chunks.push(chunk);
      total += chunk.length;
    }
  } finally {
    if (reader.releaseLock) reader.releaseLock();
  }
  const text = Buffer.concat(chunks).toString('utf8');
  return {
    text: truncated ? `${text}\n[response truncated to ${limit} bytes]` : text,
    bytes: total,
    truncated
  };
}

function parseJson(text) {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

function errorMessageFromData(data, text, statusCode) {
  const errorValue = data?.error?.message || data?.message || data?.error;
  if (typeof errorValue === 'string') return errorValue;
  if (errorValue && typeof errorValue === 'object') return JSON.stringify(errorValue);
  return text || (statusCode ? `HTTP ${statusCode}` : 'Request failed.');
}

function shouldRetry(result, attempt, retries) {
  if (attempt >= retries) return false;
  if (result?.timedOut || result?.networkError) return true;
  return [408, 409, 425, 429, 500, 502, 503, 504].includes(Number(result?.statusCode));
}

function shouldFallbackToResponses(result) {
  if (!result || result.ok) return false;
  if ([404, 405].includes(Number(result.statusCode))) return true;
  const message = String(result.error?.message || '').toLowerCase();
  return Number(result.statusCode) === 400 && /(unsupported|not supported|not compatible|responses)/i.test(message);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class ModelAdapter {
  constructor(options = {}) {
    this.fetchImpl = options.fetchImpl || globalThis.fetch;
    this.defaultTimeoutMs = clampInteger(options.timeoutMs, DEFAULT_TIMEOUT_MS, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS);
    this.defaultRetries = clampInteger(options.retries, DEFAULT_RETRIES, 0, MAX_RETRIES);
    this.defaultMaxResponseBytes = clampInteger(
      options.maxResponseBytes,
      DEFAULT_MAX_RESPONSE_BYTES,
      1024,
      MAX_RESPONSE_BYTES
    );
    this.redactSecrets = makeSecretList(options.redactSecrets || []);
  }

  async request(profile = {}, request = {}, options = {}) {
    if (typeof this.fetchImpl !== 'function') throw new Error('Fetch is unavailable in the Electron main process.');

    const type = String(options.type || request.type || 'custom');
    const endpoint = options.endpoint || request.endpoint || DEFAULT_CHAT_ENDPOINT;
    const model = request.body?.model || options.model || profile.model || profile.imageModel || null;
    const timeoutMs = clampInteger(options.timeoutMs, this.defaultTimeoutMs, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS);
    const retries = clampInteger(options.retries, this.defaultRetries, 0, MAX_RETRIES);
    const maxResponseBytes = clampInteger(
      options.maxResponseBytes,
      this.defaultMaxResponseBytes,
      1024,
      MAX_RESPONSE_BYTES
    );
    const secrets = makeSecretList([profile.apiKey, ...(options.redactSecrets || []), ...this.redactSecrets]);
    const url = endpointUrl(profile.baseUrl, endpoint);
    const startedAt = Date.now();
    let lastResult = null;

    for (let attempt = 0; attempt <= retries; attempt += 1) {
      const attemptStartedAt = Date.now();
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const headers = { 'content-type': 'application/json' };
        if (profile.apiKey) headers.authorization = `Bearer ${profile.apiKey}`;
        const response = await this.fetchImpl(url, {
          method: 'POST',
          headers,
          body: JSON.stringify(request.body || {}),
          signal: controller.signal
        });
        const limited = await readLimitedText(response, maxResponseBytes);
        const redactedText = redactText(limited.text, secrets);
        const data = parseJson(limited.text);
        const safeData = data ? redactValue(data, secrets) : null;
        const latencyMs = Date.now() - attemptStartedAt;
        const statusCode = response.status || null;
        lastResult = {
          ok: Boolean(response.ok),
          type,
          endpoint,
          statusCode,
          latencyMs,
          totalLatencyMs: Date.now() - startedAt,
          attempts: attempt + 1,
          model: safeData?.model || model,
          data: response.ok ? safeData : null,
          responseText: response.ok && !safeData ? redactedText : '',
          responseBytes: limited.bytes,
          responseTruncated: limited.truncated,
          error: response.ok
            ? null
            : {
                message: redactText(errorMessageFromData(safeData, redactedText, statusCode), secrets).slice(0, 2000),
                statusCode
              }
        };
      } catch (error) {
        const timedOut = error?.name === 'AbortError';
        lastResult = {
          ok: false,
          type,
          endpoint,
          statusCode: null,
          latencyMs: Date.now() - attemptStartedAt,
          totalLatencyMs: Date.now() - startedAt,
          attempts: attempt + 1,
          model,
          data: null,
          responseText: '',
          responseBytes: 0,
          responseTruncated: false,
          timedOut,
          networkError: !timedOut,
          error: {
            message: redactText(timedOut ? 'Request timed out.' : error?.message || 'Request failed.', secrets),
            statusCode: null
          }
        };
      } finally {
        clearTimeout(timeout);
      }

      if (!shouldRetry(lastResult, attempt, retries)) break;
      await delay(Math.min(1000 * 2 ** attempt, 3000));
    }

    return {
      ...lastResult,
      totalLatencyMs: Date.now() - startedAt
    };
  }

  chatCompletion(profile = {}, body = {}, options = {}) {
    return this.request(
      profile,
      {
        type: 'chat',
        endpoint: DEFAULT_CHAT_ENDPOINT,
        body: {
          ...body,
          model: body.model || profile.model
        }
      },
      { ...options, type: 'chat', endpoint: DEFAULT_CHAT_ENDPOINT }
    );
  }

  responses(profile = {}, body = {}, options = {}) {
    return this.request(
      profile,
      {
        type: 'responses',
        endpoint: DEFAULT_RESPONSES_ENDPOINT,
        body: {
          ...body,
          model: body.model || profile.model
        }
      },
      { ...options, type: 'responses', endpoint: DEFAULT_RESPONSES_ENDPOINT }
    );
  }

  generateImage(profile = {}, body = {}, options = {}) {
    return this.request(
      profile,
      {
        type: 'image',
        endpoint: DEFAULT_IMAGE_ENDPOINT,
        body: {
          ...body,
          model: body.model || profile.imageModel || DEFAULT_IMAGE_MODEL
        }
      },
      { ...options, type: 'image', endpoint: DEFAULT_IMAGE_ENDPOINT }
    );
  }

  async testProfile(profile = {}, options = {}) {
    const startedAt = Date.now();
    const chatResult = await this.chatCompletion(
      profile,
      {
        model: profile.model,
        messages: [{ role: 'user', content: 'ping' }],
        max_tokens: 1,
        temperature: 0
      },
      options
    );
    if (!shouldFallbackToResponses(chatResult)) {
      return {
        ...chatResult,
        totalLatencyMs: Date.now() - startedAt,
        fallbackUsed: false
      };
    }

    const remainingTimeoutMs = Math.max(MIN_TIMEOUT_MS, Number(options.timeoutMs || this.defaultTimeoutMs) - (Date.now() - startedAt));
    const responsesResult = await this.responses(
      profile,
      {
        model: profile.model,
        input: 'ping',
        max_output_tokens: 1
      },
      {
        ...options,
        timeoutMs: remainingTimeoutMs
      }
    );
    return {
      ...responsesResult,
      totalLatencyMs: Date.now() - startedAt,
      fallbackUsed: true,
      fallbackFrom: DEFAULT_CHAT_ENDPOINT
    };
  }
}

function createModelAdapter(options = {}) {
  return new ModelAdapter(options);
}

module.exports = {
  DEFAULT_CHAT_ENDPOINT,
  DEFAULT_RESPONSES_ENDPOINT,
  DEFAULT_IMAGE_ENDPOINT,
  DEFAULT_IMAGE_MODEL,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_MAX_RESPONSE_BYTES,
  MAX_RESPONSE_BYTES,
  ModelAdapter,
  createModelAdapter,
  endpointUrl,
  normalizeBaseUrl,
  redactText,
  redactValue
};
