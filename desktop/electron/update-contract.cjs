const MAX_INDEX_BYTES = 64 * 1024;
const MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024 * 1024;
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const CERTIFICATE_THUMBPRINT = /^[0-9A-F]{40}$/u;
const R2_ORIGIN = "https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev";
const TARGETS = Object.freeze({
  "windows-x64": Object.freeze({ platform: "windows", architecture: "x64" }),
  "macos-arm64": Object.freeze({ platform: "macos", architecture: "arm64" }),
  "macos-x64": Object.freeze({ platform: "macos", architecture: "x64" }),
});

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function hasExactKeys(value, expected) {
  const actual = Object.keys(value).sort();
  expected = [...expected].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function stableParts(version) {
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u.exec(String(version));
  return match ? match.slice(1).map(Number) : null;
}

function expectedName(target, version) {
  if (target === "windows-x64") return `e-Mate-Setup-${version}-x64.exe`;
  if (target === "macos-arm64") return `e-Mate-${version}-arm64.dmg`;
  if (target === "macos-x64") return `e-Mate-${version}-x64.dmg`;
  return null;
}

function parseDownloadIndex(payload) {
  const source = String(payload);
  if (Buffer.byteLength(source, "utf8") > MAX_INDEX_BYTES) return null;
  try {
    const index = record(JSON.parse(source));
    if (!index || ![1, 2].includes(index.schema_version) || index.product !== "e-Mate") return null;
    const manual = index.schema_version === 2;
    if (!hasExactKeys(index, manual
      ? ["schema_version", "product", "version", "distribution_mode", "released_at", "downloads"]
      : ["schema_version", "product", "version", "released_at", "downloads"])) return null;
    if (manual && index.distribution_mode !== "unsigned-manual") return null;
    if (!stableParts(index.version) || typeof index.released_at !== "string" || Number.isNaN(Date.parse(index.released_at))) return null;
    if (!Array.isArray(index.downloads) || index.downloads.length !== 3) return null;

    const seen = new Set();
    const downloads = [];
    for (const candidate of index.downloads) {
      const download = record(candidate);
      if (!download) return null;
      const target = TARGETS[download.target];
      const authenticode = manual
        && download.target === "windows-x64"
        && Object.prototype.hasOwnProperty.call(download, "authenticode");
      if (!target || seen.has(download.target) || !hasExactKeys(download, [
        "target", "platform", "architecture", "file_name", "url", "size_bytes", "sha256",
        ...(authenticode ? ["authenticode"] : []),
      ])) return null;
      seen.add(download.target);
      if (
        download.platform !== target.platform
        || download.architecture !== target.architecture
        || typeof download.file_name !== "string"
        || !SAFE_NAME.test(download.file_name)
        || download.file_name !== expectedName(download.target, index.version)
        || download.url !== `${R2_ORIGIN}/desktop/v${index.version}/${download.file_name}`
        || !Number.isSafeInteger(download.size_bytes)
        || download.size_bytes < 1
        || download.size_bytes > MAX_DOWNLOAD_BYTES
        || typeof download.sha256 !== "string"
        || !SHA256.test(download.sha256)
      ) return null;
      if (authenticode) {
        const evidence = record(download.authenticode);
        if (
          !evidence
          || !hasExactKeys(evidence, ["status", "signer_certificate_thumbprint"])
          || evidence.status !== "verified"
          || typeof evidence.signer_certificate_thumbprint !== "string"
          || !CERTIFICATE_THUMBPRINT.test(evidence.signer_certificate_thumbprint)
        ) return null;
      }
      downloads.push(Object.freeze({
        target: download.target,
        platform: download.platform,
        architecture: download.architecture,
        file_name: download.file_name,
        url: download.url,
        size_bytes: download.size_bytes,
        sha256: download.sha256,
      }));
    }
    return seen.size === Object.keys(TARGETS).length
      ? Object.freeze({
          version: index.version,
          distributionMode: manual ? "unsigned-manual" : "signed-automatic",
          downloads: Object.freeze(downloads),
        })
      : null;
  } catch {
    return null;
  }
}

function isNewerStableVersion(candidate, current) {
  const next = stableParts(candidate);
  const installed = stableParts(current);
  if (!next || !installed) return false;
  // ponytail: the public desktop channel is stable-only; use semver if a prerelease channel is introduced.
  return next.some((part, index) => part !== installed[index] && (
    part > installed[index] && next.slice(0, index).every((value, prior) => value === installed[prior])
  ));
}

module.exports = { isNewerStableVersion, parseDownloadIndex };
