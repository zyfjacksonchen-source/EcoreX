const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const DEFAULT_RUNTIME_PORT = 8765;

function packagedBackendPath(resourcesPath) {
  const executable = process.platform === "win32" ? "emate-backend.exe" : "emate-backend";
  const candidates = [
    path.join(resourcesPath, "runtime", "bin", executable),
    path.join(resourcesPath, "runtime", executable),
  ];
  return candidates.find((candidate) => fs.existsSync(candidate)) ?? null;
}

function developmentPython() {
  if (process.env.EMATE_PYTHON) return process.env.EMATE_PYTHON;
  return process.platform === "win32" ? "python" : "python3";
}

function installedSlotExists(dataDir) {
  try {
    const pointerPath = path.join(dataDir, "slot-pointers.json");
    const metadata = fs.lstatSync(pointerPath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 16 * 1024) return false;
    const value = JSON.parse(fs.readFileSync(pointerPath, "utf8"));
    return typeof value.current === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.current);
  } catch {
    return false;
  }
}

function installedReleaseMatches(dataDir, releaseDir) {
  try {
    const safeId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
    const pointerPath = path.join(dataDir, "slot-pointers.json");
    const pointerMetadata = fs.lstatSync(pointerPath);
    const manifestPath = path.join(releaseDir, "release-manifest.json");
    const manifestMetadata = fs.lstatSync(manifestPath);
    if (
      !pointerMetadata.isFile() || pointerMetadata.isSymbolicLink() || pointerMetadata.size > 16 * 1024
      || !manifestMetadata.isFile() || manifestMetadata.isSymbolicLink() || manifestMetadata.size > 1024 * 1024
    ) return false;
    const pointers = JSON.parse(fs.readFileSync(pointerPath, "utf8"));
    if (
      !pointers || typeof pointers !== "object" || Array.isArray(pointers)
      || Object.keys(pointers).some((key) => !["current", "previous", "known_good"].includes(key))
      || typeof pointers.current !== "string"
      || !safeId.test(pointers.current)
      || !Array.isArray(pointers.known_good)
      || pointers.known_good.length < 1
      || pointers.known_good.length > 3
      || new Set(pointers.known_good).size !== pointers.known_good.length
      || pointers.known_good.some((slotId) => typeof slotId !== "string" || !safeId.test(slotId))
      || !pointers.known_good.includes(pointers.current)
      || (
        pointers.previous !== undefined
        && pointers.previous !== null
        && (typeof pointers.previous !== "string" || !safeId.test(pointers.previous))
      )
    ) return false;
    const slotPath = path.join(dataDir, "slots", pointers.current, ".slot.json");
    const pythonPath = process.platform === "win32"
      ? path.join(dataDir, "slots", pointers.current, "payload", "bin", "pack-python", "python.exe")
      : path.join(dataDir, "slots", pointers.current, "payload", "bin", "pack-python", "bin", "python3");
    const slotMetadata = fs.lstatSync(slotPath);
    const pythonMetadata = fs.lstatSync(pythonPath);
    if (
      !slotMetadata.isFile() || slotMetadata.isSymbolicLink() || slotMetadata.size > 64 * 1024
      || !pythonMetadata.isFile() || pythonMetadata.isSymbolicLink() || pythonMetadata.size < 1
    ) return false;
    const slot = JSON.parse(fs.readFileSync(slotPath, "utf8"));
    const release = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    return (
      typeof release.release_id === "string"
      && /^release-stable-[0-9a-f]{24}$/.test(release.release_id)
      && typeof release.version === "string"
      && /^[0-9]+\.[0-9]+\.[0-9]+$/.test(release.version)
      && typeof release.build_digest === "string"
      && /^[0-9a-f]{64}$/.test(release.build_digest)
      && slot.release_id === release.release_id
      && slot.version === release.version
      && slot.build_digest === release.build_digest
    );
  } catch {
    return false;
  }
}

function runtimeOwnerNonce(dataDir) {
  try {
    const receiptPath = path.join(dataDir, "bootstrap", "runtime-owner.json");
    const metadata = fs.lstatSync(receiptPath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 1024) return null;
    const value = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
    return value.schema_version === 1 && /^[A-Za-z0-9_-]{43}$/.test(value.nonce)
      ? value.nonce
      : null;
  } catch {
    return null;
  }
}

function runtimeResponds(port, dataDir) {
  return new Promise((resolve) => {
    const nonce = runtimeOwnerNonce(dataDir);
    if (!nonce) {
      resolve(false);
      return;
    }
    const challenge = crypto.randomBytes(32).toString("base64url");
    const expected = crypto.createHmac("sha256", Buffer.from(nonce, "base64url"))
      .update("e-mate.runtime-owner.v1\0", "ascii")
      .update(challenge, "ascii")
      .digest();
    let settled = false;
    let deadline;
    const finish = (accepted) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      resolve(accepted);
    };
    const request = http.get({
      hostname: "127.0.0.1",
      port,
      path: "/api/v1/runtime-owner",
      headers: { "X-EcoreX-Owner-Challenge": challenge },
    }, (response) => {
      response.once("error", () => finish(false));
      const supplied = response.headers["x-ecorex-runtime-owner"];
      const proof = typeof supplied === "string" ? Buffer.from(supplied, "base64url") : Buffer.alloc(0);
      const accepted = (
        response.statusCode === 204
        && proof.length === expected.length
        && crypto.timingSafeEqual(proof, expected)
      );
      finish(accepted);
      response.destroy();
    });
    deadline = setTimeout(() => {
      request.destroy();
      finish(false);
    }, 1_500);
    request.once("error", () => finish(false));
  });
}

class BackendManager extends EventEmitter {
  constructor({ packaged, resourcesPath, dataDir, port = DEFAULT_RUNTIME_PORT }) {
    super();
    this.packaged = packaged;
    this.resourcesPath = resourcesPath;
    this.dataDir = dataDir;
    this.port = port;
    this.child = null;
    this.starting = null;
  }

  get origin() {
    return `http://127.0.0.1:${this.port}`;
  }

  async start() {
    if (this.child) return this.origin;
    if (this.starting) return this.starting;
    this.starting = this.#start();
    try {
      return await this.starting;
    } finally {
      this.starting = null;
    }
  }

  async #start() {
    fs.mkdirSync(this.dataDir, { recursive: true, mode: 0o700 });

    let command;
    let args;
    let cwd;
    if (this.packaged) {
      command = packagedBackendPath(this.resourcesPath);
      if (!command) throw new Error("The packaged e-Mate Bootstrap is missing.");
      const releaseDir = path.join(this.resourcesPath, "runtime", "release");
      if (!fs.existsSync(path.join(releaseDir, "release-manifest.json"))) {
        throw new Error("The packaged e-Mate release seed is missing.");
      }
      if (installedReleaseMatches(this.dataDir, releaseDir)) {
        args = ["--launch-installed", "--install-root", this.dataDir, "--no-open"];
      } else {
        args = ["--local-release", releaseDir, "--install-root", this.dataDir, "--no-open"];
      }
      cwd = this.dataDir;
    } else if (process.env.EMATE_DEV_BOOTSTRAP) {
      command = path.resolve(process.env.EMATE_DEV_BOOTSTRAP);
      if (!fs.existsSync(command)) throw new Error("EMATE_DEV_BOOTSTRAP is unavailable.");
      const releaseDir = process.env.EMATE_DEV_RELEASE_DIR;
      args = installedSlotExists(this.dataDir)
        ? ["--launch-installed", "--install-root", this.dataDir, "--no-open"]
        : ["--local-release", path.resolve(releaseDir || ""), "--install-root", this.dataDir, "--no-open"];
      if (!installedSlotExists(this.dataDir) && (!releaseDir || !fs.existsSync(path.join(releaseDir, "release-manifest.json")))) {
        throw new Error("EMATE_DEV_RELEASE_DIR must contain a signed release.");
      }
      cwd = this.dataDir;
    } else {
      const payload = process.cwd();
      const isVerifiedPayload = process.env.ECOREX_BOOTSTRAPPED === "1"
        && path.basename(payload) === "payload"
        && path.basename(path.dirname(path.dirname(payload))) === "slots";
      if (!isVerifiedPayload) {
        throw new Error("Development Runtime must be launched through the signed e-Mate Bootstrap.");
      }
      command = developmentPython();
      args = ["-m", "ecorex.server.cli", "serve", "--host", "127.0.0.1", "--port", String(this.port)];
      cwd = payload;
    }

    const child = spawn(command, args, {
      cwd,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: "ignore",
      windowsHide: true,
    });
    this.child = child;
    let startupFailure = null;
    child.once("error", () => {
      startupFailure = new Error("e-Mate Runtime could not be launched.");
    });
    child.once("exit", (code) => {
      if (this.child === child) this.child = null;
      if (code !== 0) startupFailure = new Error(`e-Mate Bootstrap stopped during startup (${code ?? "terminated"}).`);
      this.emit("exit", code);
    });

    const deadline = Date.now() + 5 * 60_000;
    while (Date.now() < deadline) {
      if (await runtimeResponds(this.port, this.dataDir)) {
        this.emit("ready", this.origin);
        return this.origin;
      }
      if (startupFailure) throw startupFailure;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    this.stop();
    throw new Error("e-Mate Runtime did not become ready within 5 minutes.");
  }

  async restart() {
    await this.stop();
    return this.start();
  }

  stop() {
    const child = this.child;
    this.child = null;
    if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
    return new Promise((resolve) => {
      let settled = false;
      let timer = null;
      const finish = () => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        resolve();
      };
      child.once("exit", finish);
      child.kill("SIGTERM");
      timer = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
        setTimeout(finish, 1_000).unref();
      }, 5_000);
      timer.unref();
    });
  }
}

module.exports = {
  BackendManager,
  DEFAULT_RUNTIME_PORT,
  installedReleaseMatches,
  installedSlotExists,
  packagedBackendPath,
  runtimeOwnerNonce,
  runtimeResponds,
};
