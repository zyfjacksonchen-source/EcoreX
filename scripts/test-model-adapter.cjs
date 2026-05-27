const assert = require('assert/strict');
const {
  DEFAULT_IMAGE_MODEL,
  createModelAdapter,
  endpointUrl,
  inferModelCapabilities,
  normalizeOpenAIResponse,
  parseOpenAIStream,
  retryDelayMs,
  shouldRetry
} = require('../electron/model-adapter.cjs');

async function main() {
  assert.equal(DEFAULT_IMAGE_MODEL, 'image-2');

  assert.equal(
    endpointUrl('https://api.example.test/v1/chat/completions', '/responses'),
    'https://api.example.test/v1/responses'
  );

  const secret = 'sk-testsecret1234567890';
  const fallbackCalls = [];
  const fallbackAdapter = createModelAdapter({
    fetchImpl: async (url, options) => {
      fallbackCalls.push({ url, body: JSON.parse(options.body), auth: options.headers.authorization });
      if (fallbackCalls.length === 1) {
        return new Response(JSON.stringify({ error: { message: `unsupported ${secret}` } }), { status: 404 });
      }
      return new Response(JSON.stringify({ model: 'fallback-model', output_text: 'ok' }), { status: 200 });
    },
    retries: 0,
    redactSecrets: [secret]
  });

  const fallbackResult = await fallbackAdapter.testProfile({
    baseUrl: 'https://api.example.test/v1/chat/completions',
    apiKey: secret,
    model: 'profile-model'
  });
  assert.equal(fallbackResult.ok, true);
  assert.equal(fallbackResult.endpoint, '/responses');
  assert.equal(fallbackResult.fallbackUsed, true);
  assert.equal(fallbackCalls.length, 2);
  assert.equal(fallbackCalls[0].auth, `Bearer ${secret}`);
  assert.equal(fallbackCalls[1].url, 'https://api.example.test/v1/responses');
  assert.equal(fallbackResult.capabilities.supportsResponses, true);
  assert.equal(fallbackResult.capabilities.supportsChatCompletions, true);

  const multiPart = normalizeOpenAIResponse('', {
    model: 'gpt-4o',
    choices: [{
      message: {
        content: [
          { type: 'text', text: 'hello ' },
          { type: 'output_text', text: { value: 'world' } },
          { type: 'image_url', image_url: { url: 'https://example.test/image.png' } }
        ],
        tool_calls: [{
          id: 'call_1',
          type: 'function',
          function: { name: 'lookup', arguments: '{"q":"x"}' }
        }]
      }
    }]
  });
  assert.equal(multiPart.text, 'hello world');
  assert.equal(multiPart.toolCalls.length, 1);
  assert.equal(multiPart.toolCalls[0].name, 'lookup');

  const sseText = [
    'event: response.output_text.delta',
    'data: {"type":"response.output_text.delta","delta":"he"}',
    '',
    'data: {"choices":[{"delta":{"content":[{"type":"text","text":"llo"}]}}]}',
    '',
    'event: response.output_text.done',
    'data: {"type":"response.output_text.done","text":"hello"}',
    '',
    'data: [DONE]',
    ''
  ].join('\n');
  const stream = parseOpenAIStream(sseText);
  assert.equal(stream.text, 'hello');
  assert.equal(stream.errorMessage, '');

  const errorStream = normalizeOpenAIResponse([
    'event: error',
    'data: {"error":{"message":"provider overloaded"}}',
    '',
    'data: [DONE]',
    ''
  ].join('\n'));
  assert.equal(errorStream.stream, true);
  assert.equal(errorStream.errorMessage, 'provider overloaded');

  const inferred = inferModelCapabilities({
    profile: { model: 'gpt-5.5', imageModel: DEFAULT_IMAGE_MODEL },
    request: { body: { stream: true } },
    endpoint: '/responses',
    type: 'responses'
  });
  assert.equal(inferred.supportsResponses, true);
  assert.equal(inferred.supportsVision, true);
  assert.equal(inferred.supportsImages, true);
  assert.equal(inferred.supportsStreaming, true);
  assert.ok(inferred.contextWindow >= 128000);

  assert.equal(shouldRetry({ statusCode: 429 }, 0, 2), true);
  assert.equal(shouldRetry({ statusCode: 401 }, 0, 2), false);
  assert.equal(shouldRetry({ networkError: true }, 2, 2), false);
  const retryDelay = retryDelayMs(2, { baseDelayMs: 500, maxDelayMs: 6000, jitterRatio: 0.35 });
  assert.ok(retryDelay >= 1000 && retryDelay <= 3000);

  const imageCalls = [];
  const imageAdapter = createModelAdapter({
    fetchImpl: async (_url, options) => {
      imageCalls.push(JSON.parse(options.body));
      return new Response(JSON.stringify({ model: imageCalls[0].model, data: [{ url: 'https://image.test/1.png' }] }), {
        status: 200
      });
    }
  });
  const imageResult = await imageAdapter.generateImage(
    { baseUrl: 'https://api.example.test/v1', apiKey: secret },
    { prompt: 'test image' }
  );
  assert.equal(imageResult.ok, true);
  assert.equal(imageCalls[0].model, DEFAULT_IMAGE_MODEL);
  assert.equal(imageResult.capabilities.supportsImages, true);
  assert.equal(imageResult.imageArtifacts.length, 1);

  await imageAdapter.generateImage(
    { baseUrl: 'https://api.example.test/v1', apiKey: secret, imageModel: 'gpt-image-2' },
    { prompt: 'legacy default image model' }
  );
  assert.equal(imageCalls[1].model, DEFAULT_IMAGE_MODEL);

  const imageFailureAdapter = createModelAdapter({
    fetchImpl: async () => new Response(JSON.stringify({ model: DEFAULT_IMAGE_MODEL, data: [] }), { status: 200 })
  });
  const imageFailureResult = await imageFailureAdapter.generateImage(
    { baseUrl: 'https://api.example.test/v1', apiKey: secret },
    { prompt: 'test image' }
  );
  assert.equal(imageFailureResult.ok, false);
  assert.match(imageFailureResult.error.message, /^Image generation failed:/);

  const redactionAdapter = createModelAdapter({
    fetchImpl: async () =>
      new Response(JSON.stringify({ error: { message: `bad authorization Bearer ${secret}` } }), { status: 500 }),
    retries: 0,
    redactSecrets: [secret]
  });
  const redactionResult = await redactionAdapter.chatCompletion({
    baseUrl: 'https://api.example.test/v1',
    apiKey: secret,
    model: 'profile-model'
  });
  assert.equal(redactionResult.ok, false);
  assert.equal(redactionResult.error.message.includes(secret), false);

  const streamErrorAdapter = createModelAdapter({
    fetchImpl: async () => new Response([
      'data: {"choices":[{"delta":{"content":""}}]}',
      '',
      'event: error',
      'data: {"error":{"message":"stream failed"}}',
      '',
      'data: [DONE]',
      ''
    ].join('\n'), { status: 200 }),
    retries: 0
  });
  const streamErrorResult = await streamErrorAdapter.chatCompletion({
    baseUrl: 'https://api.example.test/v1',
    apiKey: secret,
    model: 'profile-model'
  });
  assert.equal(streamErrorResult.ok, false);
  assert.match(streamErrorResult.error.message, /stream failed/);

  const limitAdapter = createModelAdapter({
    fetchImpl: async () => new Response('x'.repeat(4096), { status: 200 }),
    maxResponseBytes: 1024
  });
  const limitResult = await limitAdapter.chatCompletion({
    baseUrl: 'https://api.example.test/v1',
    model: 'profile-model'
  });
  assert.equal(limitResult.responseTruncated, true);

  console.log('model-adapter smoke tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
