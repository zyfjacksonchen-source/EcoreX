function externalHttpUrl(value, runtimeOrigin) {
  try {
    const target = new URL(value);
    if (target.protocol !== "https:" && target.protocol !== "http:") return null;
    if (target.origin === new URL(runtimeOrigin).origin) return null;
    if (["localhost", "127.0.0.1", "[::1]", "::1"].includes(target.hostname)) return null;
    return target.toString();
  } catch {
    return null;
  }
}

module.exports = { externalHttpUrl };
