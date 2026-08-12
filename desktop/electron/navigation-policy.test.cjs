const assert = require("node:assert/strict");
const test = require("node:test");

const { externalHttpUrl } = require("./navigation-policy.cjs");

test("private Runtime and artifact URLs never leave through the system browser", () => {
  const runtimeOrigin = "http://127.0.0.1:8765";

  assert.equal(externalHttpUrl(`${runtimeOrigin}/api/v1/artifacts/art_1/preview`, runtimeOrigin), null);
  assert.equal(externalHttpUrl("http://localhost:8765/api/v1/artifacts/art_1/preview", runtimeOrigin), null);
  assert.equal(externalHttpUrl("http://[::1]:8765/api/v1/artifacts/art_1/preview", runtimeOrigin), null);
  assert.equal(externalHttpUrl("https://example.com/help", runtimeOrigin), "https://example.com/help");
});

test("non-web and malformed URLs remain closed", () => {
  assert.equal(externalHttpUrl("file:///tmp/private.png", "http://127.0.0.1:8765"), null);
  assert.equal(externalHttpUrl("not a url", "http://127.0.0.1:8765"), null);
});
