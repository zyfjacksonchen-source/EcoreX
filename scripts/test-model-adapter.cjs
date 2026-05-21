const assert = require('assert/strict');
const {
  DEFAULT_IMAGE_MODEL,
  createModelAdapter,
  endpointUrl
} = require('../electron/model-adapter.cjs');

async function main() {
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
