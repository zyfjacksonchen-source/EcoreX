const DEFAULT_CHAT_ENDPOINT = '/chat/completions';
const DEFAULT_RESPONSES_ENDPOINT = '/responses';
const DEFAULT_IMAGE_ENDPOINT = '/images/generations';
const DEFAULT_IMAGE_MODEL = 'gpt-image-2';
const LEGACY_IMAGE_MODEL_ALIASES = new Map([
  ['image-2', DEFAULT_IMAGE_MODEL]
]);
const DEFAULT_TIMEOUT_MS = 15 * 1000;
const MIN_TIMEOUT_MS = 1000;
const MAX_TIMEOUT_MS = 2 * 60 * 1000;
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

function normalizeImageModelName(value = '') {
  const model = String(value || '').trim();
  if (!model) return DEFAULT_IMAGE_MODEL;
  return LEGACY_IMAGE_MODEL_ALIASES.get(model.toLowerCase()) || model;
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

function firstFiniteInteger(values = []) {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number) && number > 0) return Math.floor(number);
  }
  return null;
}

function optionalBoolean(...values) {
  for (const value of values) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'string') {
      if (/^(true|yes|1)$/i.test(value.trim())) return true;
      if (/^(false|no|0)$/i.test(value.trim())) return false;
    }
  }
  return null;
}

function errorMessageFromData(data, text, statusCode) {
  const errorValue = data?.error?.message || data?.message || data?.error;
  if (typeof errorValue === 'string') return errorValue;
  if (errorValue && typeof errorValue === 'object') return JSON.stringify(errorValue);
  return text || (statusCode ? `HTTP ${statusCode}` : 'Request failed.');
}

function textFromTextValue(value) {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return '';
  if (typeof value.value === 'string') return value.value;
  if (typeof value.text === 'string') return value.text;
  if (typeof value.content === 'string') return value.content;
  return '';
}

function textFromContentPart(part) {
  if (typeof part === 'string') return part;
  if (Array.isArray(part)) return part.map(textFromContentPart).join('');
  if (!part || typeof part !== 'object') return '';
  if (/(tool|function)_?call/i.test(String(part.type || ''))) return '';
  const directText =
    textFromTextValue(part.text) ||
    textFromTextValue(part.output_text) ||
    textFromTextValue(part.input_text) ||
    textFromTextValue(part.delta);
  if (directText) return directText;
  if (typeof part.content === 'string') return part.content;
  if (Array.isArray(part.content)) return part.content.map(textFromContentPart).join('');
  if (part.message) return textFromContentPart(part.message.content);
  return '';
}

function extractOpenAIText(data) {
  if (!data || typeof data !== 'object') return '';
  if (typeof data.output_text === 'string') return data.output_text;
  if (typeof data.delta === 'string') return data.delta;
  if (typeof data.text === 'string' && /(?:text|message|delta|output|completion)/i.test(String(data.type || ''))) return data.text;
  if (Array.isArray(data.choices)) {
    return data.choices
      .map((choice) => {
        if (typeof choice?.text === 'string') return choice.text;
        return (
          textFromContentPart(choice?.message?.content) ||
          textFromContentPart(choice?.delta?.content) ||
          textFromContentPart(choice?.message) ||
          textFromContentPart(choice?.delta)
        );
      })
      .join('');
  }
  if (Array.isArray(data.output)) {
    return data.output
      .map((item) => textFromContentPart(item?.content) || textFromContentPart(item))
      .join('');
  }
  if (Array.isArray(data.content)) return data.content.map(textFromContentPart).join('');
  return textFromContentPart(data.message?.content);
}

function extractOpenAIToolCalls(data) {
  if (!data || typeof data !== 'object') return [];
  const calls = [];
  const pushCall = (call) => {
    if (!call || typeof call !== 'object') return;
    const type = String(call.type || '').trim();
    const name =
      call.name ||
      call.function?.name ||
      call.tool_name ||
      call.server_label ||
      call.mcp_tool_call?.name ||
      null;
    if (!type && !name && !call.id && !call.call_id) return;
    calls.push({
      id: call.id || call.call_id || null,
      type: type || (call.function ? 'function_call' : 'tool_call'),
      name,
      status: call.status || null,
      arguments: call.arguments || call.function?.arguments || call.input || null
    });
  };
  for (const choice of Array.isArray(data.choices) ? data.choices : []) {
    for (const call of choice?.message?.tool_calls || []) pushCall(call);
    for (const call of choice?.delta?.tool_calls || []) pushCall(call);
    pushCall(choice?.message?.function_call);
    pushCall(choice?.delta?.function_call);
  }
  for (const item of Array.isArray(data.output) ? data.output : []) {
    pushCall(item);
    for (const part of Array.isArray(item?.content) ? item.content : []) pushCall(part);
  }
  pushCall(data.tool_call);
  pushCall(data.function_call);
  return calls;
}

function extractOpenAIUsage(data) {
  if (!data || typeof data !== 'object') return null;
  const usage = data.usage || data.response?.usage || null;
  if (!usage || typeof usage !== 'object') return null;
  return {
    inputTokens: firstFiniteInteger([usage.input_tokens, usage.prompt_tokens, usage.promptTokens]),
    outputTokens: firstFiniteInteger([usage.output_tokens, usage.completion_tokens, usage.completionTokens]),
    totalTokens: firstFiniteInteger([usage.total_tokens, usage.totalTokens])
  };
}

function extractImageArtifacts(data) {
  if (!data || typeof data !== 'object') return [];
  const artifacts = [];
  const pushArtifact = (item) => {
    if (!item || typeof item !== 'object') return;
    const url = typeof item.url === 'string' ? item.url : item.image_url?.url;
    const b64 = typeof item.b64_json === 'string'
      ? item.b64_json
      : (typeof item.image_base64 === 'string' ? item.image_base64 : '');
    if (!url && !b64) return;
    artifacts.push({
      url: url || null,
      b64_json: b64 || null,
      mimeType: item.mime_type || item.mimeType || item.image_url?.mime_type || null,
      revisedPrompt: item.revised_prompt || item.revisedPrompt || null
    });
  };
  for (const item of Array.isArray(data.data) ? data.data : []) pushArtifact(item);
  for (const item of Array.isArray(data.images) ? data.images : []) pushArtifact(item);
  for (const item of Array.isArray(data.output) ? data.output : []) {
    pushArtifact(item);
    for (const part of Array.isArray(item?.content) ? item.content : []) pushArtifact(part);
  }
  pushArtifact(data.image);
  return artifacts;
}

function extractOpenAIModel(data) {
  if (!data || typeof data !== 'object') return null;
  return data.model || data.response?.model || null;
}

function extractOpenAIError(data) {
  if (!data || typeof data !== 'object') return '';
  const error = data.error || data.response?.error;
  if (typeof error === 'string') return error;
  if (error && typeof error === 'object') return error.message || JSON.stringify(error);
  if (/error/i.test(String(data.type || '')) && typeof data.message === 'string') return data.message;
  if (/error/i.test(String(data.event || '')) && typeof data.message === 'string') return data.message;
  return '';
}

function parseServerSentEventData(text = '') {
  const frames = [];
  let event = '';
  let dataLines = [];
  const pushFrame = () => {
    if (!dataLines.length) return;
    frames.push({ event, data: dataLines.join('\n') });
    event = '';
    dataLines = [];
  };
  for (const line of String(text || '').split(/\r?\n/)) {
    if (!line.trim()) {
      pushFrame();
      continue;
    }
    if (line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator >= 0 ? line.slice(0, separator) : line;
    const value = separator >= 0 ? line.slice(separator + 1).replace(/^ /, '') : '';
    if (field === 'event') event = value;
    if (field === 'data') dataLines.push(value);
  }
  pushFrame();
  return frames;
}

function extractOpenAIStreamText(data, eventName = '') {
  if (!data || typeof data !== 'object') return '';
  const type = String(data.type || eventName || '');
  if (/error/i.test(type)) return '';
  if (/delta/i.test(type) && typeof data.delta === 'string') return data.delta;
  if (/delta/i.test(type) && typeof data.text === 'string') return data.text;
  if (Array.isArray(data.choices)) {
    return data.choices
      .map((choice) => textFromContentPart(choice?.delta?.content) || (typeof choice?.text === 'string' ? choice.text : ''))
      .join('');
  }
  return '';
}

function parseOpenAIStream(text = '') {
  const frames = parseServerSentEventData(text);
  if (!frames.length) return null;
  const textParts = [];
  const toolCalls = [];
  let parsedFrames = 0;
  let model = null;
  let errorMessage = '';
  for (const frame of frames) {
    const trimmed = String(frame.data || '').trim();
    if (!trimmed || trimmed === '[DONE]') continue;
    const data = parseJson(trimmed);
    if (!data) continue;
    parsedFrames += 1;
    if (frame.event) data.event = frame.event;
    model = model || extractOpenAIModel(data);
    errorMessage = errorMessage || extractOpenAIError(data);
    toolCalls.push(...extractOpenAIToolCalls(data));
    const textPart = extractOpenAIStreamText(data, frame.event);
    if (textPart) textParts.push(textPart);
  }
  if (!parsedFrames) return null;
  return {
    stream: true,
    model,
    text: textParts.join(''),
    errorMessage,
    toolCalls
  };
}

function heuristicContextWindow(modelName = '') {
  const model = String(modelName || '').toLowerCase();
  if (!model) return null;
  if (/gemini-(?:1\.5|2|2\.5)|gpt-4\.1|gpt-5|o3|o4|qwen.*(?:long|coder|plus|max)|kimi|moonshot/.test(model)) return 1_000_000;
  if (/claude-(?:3|4)|sonnet|opus|haiku/.test(model)) return 200_000;
  if (/gpt-4o|gpt-4\.5|gpt-4-turbo|deepseek|doubao|glm-4/.test(model)) return 128_000;
  if (/gpt-4-32k/.test(model)) return 32_768;
  if (/gpt-3\.5|gpt-4/.test(model)) return 16_384;
  if (/image|dall-e|imagen|flux|sdxl|midjourney/.test(model)) return null;
  return 128_000;
}

function inferModelCapabilities({ profile = {}, request = {}, options = {}, endpoint = '', type = '', model = '', data = null, stream = false } = {}) {
  const caps = profile.capabilities && typeof profile.capabilities === 'object' ? profile.capabilities : {};
  const requestBody = request.body || {};
  const resolvedModel = model || requestBody.model || options.model || profile.model || '';
  const imageModel = normalizeImageModelName(profile.imageModel || profile.imageModelName || (type === 'image' ? resolvedModel : '') || DEFAULT_IMAGE_MODEL);
  const modelText = String(resolvedModel || '').toLowerCase();
  const routedModelText = `${resolvedModel} ${imageModel}`.toLowerCase();
  const endpointText = String(endpoint || '').toLowerCase();
  const typeText = String(type || '').toLowerCase();
  const isImageModel = typeText === 'image' || /(gpt-image|image|dall-e|imagen|flux|sdxl|midjourney)/i.test(String(resolvedModel || ''));
  const supportsResponses = optionalBoolean(
    profile.supportsResponses,
    caps.supportsResponses,
    options.supportsResponses
  ) ?? (endpointText.includes('responses') || /gpt-[45]|o[134]|claude|responses|gemini|qwen|deepseek/i.test(routedModelText));
  const supportsChatCompletions = optionalBoolean(
    profile.supportsChatCompletions,
    caps.supportsChatCompletions,
    options.supportsChatCompletions
  ) ?? !isImageModel;
  const supportsVision = optionalBoolean(
    profile.supportsVision,
    caps.supportsVision,
    options.supportsVision
  ) ?? /gpt-4o|gpt-4\.1|gpt-4\.5|gpt-5|o3|o4|vision|gemini|claude-3|claude-4|qwen.*vl|deepseek.*vl/i.test(modelText);
  const supportsImages = optionalBoolean(
    profile.supportsImages,
    caps.supportsImages,
    options.supportsImages
  ) ?? Boolean(imageModel || isImageModel);
  const supportsStreaming = optionalBoolean(
    profile.supportsStreaming,
    caps.supportsStreaming,
    options.supportsStreaming
  ) ?? Boolean(stream || requestBody.stream || typeText === 'responses' || typeText === 'chat');
  const contextWindow = firstFiniteInteger([
    profile.contextWindow,
    profile.context_window,
    profile.maxContextTokens,
    profile.max_context_tokens,
    caps.contextWindow,
    caps.context_window,
    data?.model_details?.context_window,
    data?.model?.context_window
  ]) || heuristicContextWindow(resolvedModel);
  return {
    model: resolvedModel || null,
    imageModel: normalizeImageModelName(imageModel || DEFAULT_IMAGE_MODEL),
    supportsResponses: Boolean(supportsResponses),
    supportsChatCompletions: Boolean(supportsChatCompletions),
    supportsVision: Boolean(supportsVision),
    supportsImages: Boolean(supportsImages),
    supportsStreaming: Boolean(supportsStreaming),
    contextWindow
  };
}

function normalizeAdapterErrorMessage({ type = '', message = '', statusCode = null, timedOut = false, networkError = false } = {}) {
  let base = String(message || '').trim();
  if (!base) {
    if (timedOut) base = 'Request timed out.';
    else if (networkError) base = 'Network request failed.';
    else base = statusCode ? `HTTP ${statusCode}` : 'Request failed.';
  }
  const kind = String(type || '').toLowerCase();
  if (kind === 'image' && !/^image generation failed:/i.test(base)) return `Image generation failed: ${base}`;
  if (kind === 'responses' && !/^responses api request failed:/i.test(base)) return `Responses API request failed: ${base}`;
  if (kind === 'chat' && !/^chat completion request failed:/i.test(base)) return `Chat completion request failed: ${base}`;
  return base;
}

function normalizeOpenAIResponse(text = '', data = null) {
  if (data) {
    return {
      data,
      stream: false,
      model: extractOpenAIModel(data),
      text: extractOpenAIText(data),
      errorMessage: extractOpenAIError(data),
      toolCalls: extractOpenAIToolCalls(data),
      usage: extractOpenAIUsage(data),
      imageArtifacts: extractImageArtifacts(data)
    };
  }
  const stream = parseOpenAIStream(text);
  if (stream) {
    return {
      data: {
        stream: true,
        model: stream.model,
        text: stream.text
      },
      stream: true,
      model: stream.model,
      text: stream.text,
      errorMessage: stream.errorMessage,
      toolCalls: stream.toolCalls || [],
      usage: null,
      imageArtifacts: []
    };
  }
  return {
    data: null,
    stream: false,
    model: null,
    text: '',
    errorMessage: '',
    toolCalls: [],
    usage: null,
    imageArtifacts: []
  };
}

function shouldRetry(result, attempt, retries) {
  if (attempt >= retries) return false;
  if (result?.timedOut || result?.networkError) return true;
  return [408, 409, 425, 429, 500, 502, 503, 504].includes(Number(result?.statusCode));
}

function retryDelayMs(attempt, options = {}) {
  const baseDelayMs = clampInteger(options.baseDelayMs, 500, 100, 5000);
  const maxDelayMs = clampInteger(options.maxDelayMs, 6000, baseDelayMs, 30000);
  const jitterRatio = Math.max(0, Math.min(Number(options.jitterRatio) || 0.35, 0.8));
  const exponential = Math.min(baseDelayMs * 2 ** Math.max(0, Number(attempt) || 0), maxDelayMs);
  const jitterWindow = Math.round(exponential * jitterRatio);
  const jitter = jitterWindow ? Math.floor(Math.random() * (jitterWindow * 2 + 1)) - jitterWindow : 0;
  return Math.max(100, Math.min(maxDelayMs, exponential + jitter));
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
    const retrySchedule = [];

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
        const normalized = normalizeOpenAIResponse(limited.text, data);
        const safeData = normalized.data ? redactValue(normalized.data, secrets) : null;
        let normalizedText = redactText(normalized.text, secrets);
        let normalizedFailureMessage = normalized.errorMessage || '';
        if (type === 'image' && response.ok && !normalizedFailureMessage && !normalized.imageArtifacts.length) {
          normalizedFailureMessage = 'Image generation response did not include image data or a URL.';
        }
        const responseOk = Boolean(response.ok && !normalized.errorMessage);
        const finalResponseOk = Boolean(responseOk && !normalizedFailureMessage);
        if (!finalResponseOk) normalizedText = '';
        const latencyMs = Date.now() - attemptStartedAt;
        const statusCode = response.status || null;
        const capabilityMetadata = inferModelCapabilities({
          profile,
          request,
          options,
          endpoint,
          type,
          model: normalized.model || safeData?.model || model,
          data: normalized.data || data,
          stream: normalized.stream
        });
        const failureMessage = finalResponseOk
          ? ''
          : normalizeAdapterErrorMessage({
              type,
              message: normalizedFailureMessage || errorMessageFromData(safeData, redactedText, statusCode),
              statusCode
            });
        lastResult = {
          ok: finalResponseOk,
          type,
          endpoint,
          statusCode,
          latencyMs,
          totalLatencyMs: Date.now() - startedAt,
          attempts: attempt + 1,
          model: normalized.model || safeData?.model || model,
          capabilities: capabilityMetadata,
          capabilityMetadata,
          data: finalResponseOk ? safeData : null,
          text: responseOk ? normalizedText : '',
          stream: Boolean(normalized.stream),
          toolCalls: finalResponseOk ? redactValue(normalized.toolCalls || [], secrets) : [],
          usage: finalResponseOk ? normalized.usage : null,
          imageArtifacts: finalResponseOk ? redactValue(normalized.imageArtifacts || [], secrets) : [],
          responseText: finalResponseOk && !safeData ? redactedText : '',
          responseBytes: limited.bytes,
          responseTruncated: limited.truncated,
          error: finalResponseOk
            ? null
            : {
                message: redactText(
                  failureMessage,
                  secrets
                ).slice(0, 2000),
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
          capabilities: inferModelCapabilities({ profile, request, options, endpoint, type, model }),
          capabilityMetadata: inferModelCapabilities({ profile, request, options, endpoint, type, model }),
          data: null,
          text: '',
          stream: false,
          toolCalls: [],
          usage: null,
          imageArtifacts: [],
          responseText: '',
          responseBytes: 0,
          responseTruncated: false,
          timedOut,
          networkError: !timedOut,
          error: {
            message: redactText(
              normalizeAdapterErrorMessage({
                type,
                message: timedOut ? 'Request timed out.' : error?.message || 'Request failed.',
                timedOut,
                networkError: !timedOut
              }),
              secrets
            ),
            statusCode: null
          }
        };
      } finally {
        clearTimeout(timeout);
      }

      if (!shouldRetry(lastResult, attempt, retries)) break;
      const waitMs = retryDelayMs(attempt);
      retrySchedule.push({
        attempt: attempt + 1,
        nextAttempt: attempt + 2,
        delayMs: waitMs,
        statusCode: lastResult?.statusCode || null,
        timedOut: Boolean(lastResult?.timedOut),
        networkError: Boolean(lastResult?.networkError)
      });
      await delay(waitMs);
    }

    return {
      ...lastResult,
      retrySchedule,
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
          model: normalizeImageModelName(body.model || profile.imageModel || profile.imageModelName || DEFAULT_IMAGE_MODEL)
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
  normalizeImageModelName,
  inferModelCapabilities,
  extractOpenAIText,
  extractOpenAIToolCalls,
  extractImageArtifacts,
  normalizeOpenAIResponse,
  parseOpenAIStream,
  retryDelayMs,
  shouldRetry,
  redactText,
  redactValue
};
