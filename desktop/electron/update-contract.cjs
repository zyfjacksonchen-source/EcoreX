function parseUpdateVersion(metadata) {
  const match = /^version:\s*["']?([^\s"']+)["']?\s*$/m.exec(String(metadata));
  return match?.[1] ?? null;
}

function scalar(value) {
  const candidate = String(value).trim();
  if ((candidate.startsWith('"') && candidate.endsWith('"'))
      || (candidate.startsWith("'") && candidate.endsWith("'"))) {
    return candidate.slice(1, -1);
  }
  return candidate;
}

function parseMacUpdateMetadata(metadata) {
  const source = String(metadata);
  if (Buffer.byteLength(source, "utf8") > 64 * 1024) return null;
  const version = parseUpdateVersion(source);
  if (!stableParts(version)) return null;
  const files = [];
  let current = null;
  for (const line of source.split(/\r?\n/u)) {
    const start = /^\s*-\s+url:\s*(.+?)\s*$/u.exec(line);
    if (start) {
      if (current) files.push(current);
      current = { url: scalar(start[1]), sha512: null, size: null };
      continue;
    }
    if (!current) continue;
    const digest = /^\s+sha512:\s*(.+?)\s*$/u.exec(line);
    if (digest) current.sha512 = scalar(digest[1]);
    const size = /^\s+size:\s*(\d+)\s*$/u.exec(line);
    if (size) current.size = Number(size[1]);
  }
  if (current) files.push(current);
  if (!files.length || files.some((file) => (
    !/^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:dmg|zip)$/u.test(file.url)
    || !/^[A-Za-z0-9+/]{86}==$/u.test(file.sha512 ?? "")
    || Buffer.from(file.sha512, "base64").length !== 64
    || !Number.isSafeInteger(file.size)
    || file.size < 1
    || file.size > 16 * 1024 * 1024 * 1024
  ))) return null;
  return { version, files };
}

function stableParts(version) {
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(String(version));
  return match ? match.slice(1).map(Number) : null;
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

module.exports = { isNewerStableVersion, parseMacUpdateMetadata, parseUpdateVersion };
