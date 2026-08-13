const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

const DEFAULT_RUNTIME_PORT = 8765;
const MAX_RUNTIME_PORT_ATTEMPTS = 3;
const STARTUP_DIAGNOSTIC_TOKEN_ENV = "ECOREX_RUNTIME_STARTUP_DIAGNOSTIC_TOKEN";
const SAFE_DIAGNOSTIC = /^[a-z][a-z0-9_]{0,127}$/;
const SAFE_RUNTIME_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA256 = /^[a-f0-9]{64}$/;

class RuntimeStartupError extends Error {
  constructor(diagnosticCode, { exitCode = null, phase = "runtime", stage = null } = {}) {
    super("e-Mate Runtime could not start.");
    this.name = "RuntimeStartupError";
    this.diagnosticCode = SAFE_DIAGNOSTIC.test(diagnosticCode) ? diagnosticCode : "runtime_startup_failed";
    this.exitCode = Number.isSafeInteger(exitCode) ? exitCode : null;
    this.phase = ["package", "port", "runtime", "spawn"].includes(phase) ? phase : "runtime";
    this.stage = typeof stage === "string" && SAFE_DIAGNOSTIC.test(stage) ? stage : null;
  }
}

function diagnosticDirectory(dataDir) {
  return path.join(dataDir, ".runtime-startup");
}

function prepareRuntimeDiagnosticDirectory(dataDir) {
  const directory = diagnosticDirectory(dataDir);
  try {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const metadata = fs.lstatSync(directory);
    return metadata.isDirectory() && !metadata.isSymbolicLink();
  } catch {
    return false;
  }
}

function consumeRuntimeStartupStage(dataDir, token) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) return null;
  const target = path.join(diagnosticDirectory(dataDir), `${token}.json`);
  try {
    const metadata = fs.lstatSync(target);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size < 1 || metadata.size > 512) return null;
    const value = JSON.parse(fs.readFileSync(target, "utf8"));
    return (
      value?.schema_version === 1
      && value.token === token
      && typeof value.stage === "string"
      && SAFE_DIAGNOSTIC.test(value.stage)
      && Object.keys(value).length === 3
    ) ? value.stage : null;
  } catch {
    return null;
  } finally {
    try { fs.unlinkSync(target); } catch { /* Missing or protected advisory evidence is ignored. */ }
  }
}

function runtimeDiagnosticCode(error) {
  return error instanceof RuntimeStartupError ? error.diagnosticCode : "runtime_startup_failed";
}

function runtimeExitDiagnosticCode(exitCode) {
  if (!Number.isSafeInteger(exitCode)) return "runtime_exit_terminated";
  return exitCode < 0 ? `runtime_exit_n${Math.abs(exitCode)}` : `runtime_exit_${exitCode}`;
}

function writeRuntimeFailureDiagnostic(dataDir, error) {
  const normalized = error instanceof RuntimeStartupError ? error : new RuntimeStartupError("runtime_startup_failed");
  const directory = path.join(dataDir, "diagnostics");
  let temporary = null;
  try {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const metadata = fs.lstatSync(directory);
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) return false;
    const target = path.join(directory, "runtime-startup.json");
    temporary = `${target}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify({
      schema_version: 1,
      status: "failed",
      diagnostic_code: normalized.diagnosticCode,
      phase: normalized.phase,
      stage: normalized.stage,
      exit_code: normalized.exitCode,
    })}\n`, { mode: 0o600 });
    fs.renameSync(temporary, target);
    return true;
  } catch {
    try { if (temporary) fs.unlinkSync(temporary); } catch { /* No partial diagnostic remains. */ }
    return false;
  }
}

function clearRuntimeFailureDiagnostic(dataDir) {
  try { fs.unlinkSync(path.join(dataDir, "diagnostics", "runtime-startup.json")); } catch { /* No stale diagnostic. */ }
}

function availableLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0, exclusive: true }, () => {
      const address = server.address();
      const port = address && typeof address === "object" ? address.port : null;
      server.close(() => {
        if (!Number.isSafeInteger(port) || port < 1) reject(new Error("loopback_port_unavailable"));
        else resolve(port);
      });
    });
  });
}

function runtimeIdentity(runtimeRoot) {
  try {
    const markerPath = path.join(runtimeRoot, ".slot.json");
    const metadata = fs.lstatSync(markerPath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size < 1 || metadata.size > 256 * 1024) return null;
    const marker = JSON.parse(fs.readFileSync(markerPath, "utf8"));
    const identity = {
      release_id: marker.release_id,
      build_digest: marker.build_digest,
      artifact_id: marker.artifact_id,
      artifact_sha256: marker.artifact_sha256,
      payload_digest: marker.payload_digest,
    };
    return (
      SAFE_RUNTIME_ID.test(identity.release_id)
      && SHA256.test(identity.build_digest)
      && SAFE_RUNTIME_ID.test(identity.artifact_id)
      && SHA256.test(identity.artifact_sha256)
      && SHA256.test(identity.payload_digest)
    ) ? identity : null;
  } catch {
    return null;
  }
}

function sameRuntimeIdentity(left, right) {
  return Boolean(left && right && Object.keys(left).every((key) => left[key] === right[key]));
}

function packagedRuntimeSpec(resourcesPath, dataDir, port, targetPlatform = process.platform) {
  try {
    const runtimeRoot = path.join(resourcesPath, "runtime");
    const payload = path.join(runtimeRoot, "payload");
    const command = path.join(payload, "bin", targetPlatform === "win32" ? "ecorex.exe" : "ecorex");
    const identity = runtimeIdentity(runtimeRoot);
    if (!identity) return null;
    for (const directory of [runtimeRoot, payload]) {
      const metadata = fs.lstatSync(directory);
      if (!metadata.isDirectory() || metadata.isSymbolicLink()) return null;
    }
    for (const file of [
      command,
      path.join(runtimeRoot, ".slot.json"),
      path.join(runtimeRoot, "release-manifest.json"),
    ]) {
      const metadata = fs.lstatSync(file);
      if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size < 1) return null;
    }
    return {
      command,
      args: ["serve", "--host", "127.0.0.1", "--port", String(port)],
      cwd: payload,
      environment: {
        ...process.env,
        ECOREX_BOOTSTRAPPED: "1",
        COW_DATA_DIR: dataDir,
        COW_DESKTOP: "1",
        PLAYWRIGHT_BROWSERS_PATH: path.join(payload, "ms-playwright"),
        EMATE_DESKTOP: "1",
        EMATE_PACKAGED_RUNTIME: "1",
        EMATE_DATA_DIR: dataDir,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONNOUSERSITE: "1",
        PYTHONTZPATH: "",
        PYTHONUNBUFFERED: "1",
      },
      windowsHide: true,
      detached: targetPlatform !== "win32",
      runtimeIdentity: identity,
    };
  } catch {
    return null;
  }
}

function developmentPython() {
  if (process.env.EMATE_PYTHON) return process.env.EMATE_PYTHON;
  return process.platform === "win32" ? "python" : "python3";
}

function issueRuntimeOwnerReceipt(dataDir, pid, identity, nonce = crypto.randomBytes(32).toString("base64url")) {
  if (!Number.isSafeInteger(pid) || pid < 1 || !sameRuntimeIdentity(identity, runtimeIdentityValue(identity))) {
    throw new Error("Runtime owner receipt is invalid.");
  }
  if (!/^[A-Za-z0-9_-]{43}$/.test(nonce)) throw new Error("Runtime owner nonce is invalid.");
  const directory = path.join(dataDir, "bootstrap");
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const receiptPath = path.join(directory, "runtime-owner.json");
  const temporary = `${receiptPath}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify({
    schema_version: 2,
    nonce,
    pid,
    runtime_identity: identity,
  }), { mode: 0o600 });
  fs.renameSync(temporary, receiptPath);
  return nonce;
}

function runtimeIdentityValue(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const identity = {
    release_id: value.release_id,
    build_digest: value.build_digest,
    artifact_id: value.artifact_id,
    artifact_sha256: value.artifact_sha256,
    payload_digest: value.payload_digest,
  };
  return (
    Object.keys(value).length === 5
    && SAFE_RUNTIME_ID.test(identity.release_id)
    && SHA256.test(identity.build_digest)
    && SAFE_RUNTIME_ID.test(identity.artifact_id)
    && SHA256.test(identity.artifact_sha256)
    && SHA256.test(identity.payload_digest)
  ) ? identity : null;
}

function runtimeOwnerReceipt(dataDir) {
  try {
    const receiptPath = path.join(dataDir, "bootstrap", "runtime-owner.json");
    const metadata = fs.lstatSync(receiptPath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 2048) return null;
    const value = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
    const identity = runtimeIdentityValue(value.runtime_identity);
    return (
      value.schema_version === 2
      && /^[A-Za-z0-9_-]{43}$/.test(value.nonce)
      && Number.isSafeInteger(value.pid)
      && value.pid > 0
      && identity
    ) ? { nonce: value.nonce, pid: value.pid, runtime_identity: identity } : null;
  } catch {
    return null;
  }
}

function runtimeOwnerNonce(dataDir) {
  return runtimeOwnerReceipt(dataDir)?.nonce ?? null;
}

function probeRuntimeOwner(port, dataDir) {
  return new Promise((resolve) => {
    const receipt = runtimeOwnerReceipt(dataDir);
    if (!receipt) {
      resolve(null);
      return;
    }
    const { nonce } = receipt;
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
      resolve(accepted ? receipt : null);
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

async function runtimeResponds(port, dataDir, identity = null) {
  const receipt = await probeRuntimeOwner(port, dataDir);
  return Boolean(receipt && (!identity || sameRuntimeIdentity(receipt.runtime_identity, identity)));
}

function loopbackPortOccupied(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: "127.0.0.1", port });
    let settled = false;
    const finish = (occupied) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(occupied);
    };
    socket.setTimeout(500, () => finish(false));
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
  });
}

function runtimeTerminationSpec(pid, force, targetPlatform = process.platform, systemRoot = process.env.SystemRoot) {
  if (!Number.isSafeInteger(pid) || pid < 1) return null;
  if (targetPlatform === "win32") {
    return {
      command: path.join(systemRoot || "C:\\Windows", "System32", "taskkill.exe"),
      args: ["/PID", String(pid), "/T", ...(force ? ["/F"] : [])],
    };
  }
  return { pid: -pid, signal: force ? "SIGKILL" : "SIGTERM" };
}

function terminateRuntimeProcess(pid, force = false) {
  const termination = runtimeTerminationSpec(pid, force);
  if (!termination) return;
  if (termination.command) {
    spawn(termination.command, termination.args, { stdio: "ignore", windowsHide: true }).once("error", () => {});
    return;
  }
  try { process.kill(termination.pid, termination.signal); } catch { /* The exact process group already exited. */ }
}

async function stopRuntimePid(pid, port) {
  const waitForRelease = async (attempts) => {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (!await loopbackPortOccupied(port)) return true;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return false;
  };
  terminateRuntimeProcess(pid);
  if (await waitForRelease(50)) return;
  terminateRuntimeProcess(pid, true);
  if (!await waitForRelease(10)) throw new Error(`e-Mate Runtime did not release loopback port ${port}.`);
}

class BackendManager extends EventEmitter {
  constructor({ packaged, resourcesPath, dataDir, port, allowPortFallback = port === undefined }) {
    super();
    this.packaged = packaged;
    this.resourcesPath = resourcesPath;
    this.dataDir = dataDir;
    this.port = port ?? DEFAULT_RUNTIME_PORT;
    this.allowPortFallback = allowPortFallback;
    this.child = null;
    this.runtimePid = null;
    this.starting = null;
  }

  get origin() {
    return `http://127.0.0.1:${this.port}`;
  }

  async start() {
    if (this.child) return this.origin;
    if (this.starting) return this.starting;
    clearRuntimeFailureDiagnostic(this.dataDir);
    this.starting = this.#start();
    try {
      return await this.starting;
    } catch (error) {
      const normalized = error instanceof RuntimeStartupError
        ? error
        : new RuntimeStartupError("runtime_startup_failed");
      writeRuntimeFailureDiagnostic(this.dataDir, normalized);
      throw normalized;
    } finally {
      this.starting = null;
    }
  }

  async #start() {
    fs.mkdirSync(this.dataDir, { recursive: true, mode: 0o700 });

    let spec;
    if (this.packaged) {
      spec = packagedRuntimeSpec(this.resourcesPath, this.dataDir, this.port);
      if (!spec) throw new RuntimeStartupError("runtime_package_missing", { phase: "package" });
    } else {
      const payload = process.cwd();
      const isVerifiedPayload = process.env.ECOREX_BOOTSTRAPPED === "1"
        && path.basename(payload) === "payload"
        && path.basename(path.dirname(path.dirname(payload))) === "slots";
      if (!isVerifiedPayload) {
        throw new RuntimeStartupError("runtime_development_authority_invalid", { phase: "package" });
      }
      const runtimeRoot = path.dirname(payload);
      const identity = runtimeIdentity(runtimeRoot);
      if (!identity) throw new RuntimeStartupError("runtime_development_identity_invalid", { phase: "package" });
      spec = {
        command: developmentPython(),
        args: ["-m", "ecorex.server.cli", "serve", "--host", "127.0.0.1", "--port", String(this.port)],
        cwd: payload,
        environment: { ...process.env, PYTHONUNBUFFERED: "1" },
        windowsHide: true,
        detached: process.platform !== "win32",
        runtimeIdentity: identity,
      };
    }

    const existing = await probeRuntimeOwner(this.port, this.dataDir);
    if (existing) {
      if (sameRuntimeIdentity(existing.runtime_identity, spec.runtimeIdentity)) {
        this.runtimePid = existing.pid;
        this.emit("ready", this.origin);
        return this.origin;
      }
      await stopRuntimePid(existing.pid, this.port);
    }
    if (await loopbackPortOccupied(this.port)) {
      const expected = runtimeOwnerReceipt(this.dataDir);
      if (sameRuntimeIdentity(expected?.runtime_identity, spec.runtimeIdentity)) {
        const deadline = Date.now() + 15_000;
        while (Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 500));
          const starting = await probeRuntimeOwner(this.port, this.dataDir);
          if (sameRuntimeIdentity(starting?.runtime_identity, spec.runtimeIdentity)) {
            this.runtimePid = starting.pid;
            this.emit("ready", this.origin);
            return this.origin;
          }
          if (!await loopbackPortOccupied(this.port)) break;
        }
      }
    }
    if (await loopbackPortOccupied(this.port)) {
      if (!this.allowPortFallback) throw new RuntimeStartupError("runtime_port_occupied", { phase: "port" });
      this.port = await availableLoopbackPort();
      spec = this.packaged
        ? packagedRuntimeSpec(this.resourcesPath, this.dataDir, this.port)
        : { ...spec, args: [...spec.args.slice(0, -1), String(this.port)] };
      if (!spec) throw new RuntimeStartupError("runtime_package_missing", { phase: "package" });
    }
    for (let attempt = 0; attempt < MAX_RUNTIME_PORT_ATTEMPTS; attempt += 1) {
      try {
        return await this.#launch(spec);
      } catch (error) {
        if (
          !this.allowPortFallback
          || !(error instanceof RuntimeStartupError)
          || error.stage !== "http_server_bind"
          || attempt + 1 >= MAX_RUNTIME_PORT_ATTEMPTS
        ) throw error;
        this.port = await availableLoopbackPort();
        spec = this.packaged
          ? packagedRuntimeSpec(this.resourcesPath, this.dataDir, this.port)
          : { ...spec, args: [...spec.args.slice(0, -1), String(this.port)] };
        if (!spec) throw new RuntimeStartupError("runtime_package_missing", { phase: "package" });
      }
    }
    throw new RuntimeStartupError("runtime_port_retry_exhausted");
  }

  async #launch(spec) {
    const ownerNonce = crypto.randomBytes(32).toString("base64url");
    const diagnosticToken = crypto.randomBytes(32).toString("base64url");
    if (prepareRuntimeDiagnosticDirectory(this.dataDir)) {
      spec.environment[STARTUP_DIAGNOSTIC_TOKEN_ENV] = diagnosticToken;
    }
    spec.environment.ECOREX_RUNTIME_OWNER_NONCE = ownerNonce;
    const child = spawn(spec.command, spec.args, {
      cwd: spec.cwd,
      env: spec.environment,
      stdio: "ignore",
      windowsHide: spec.windowsHide,
      detached: spec.detached,
    });
    let startupFailure = null;
    child.once("error", (error) => {
      const code = typeof error?.code === "string" && /^[A-Z0-9_]{1,32}$/.test(error.code)
        ? `runtime_spawn_${error.code.toLowerCase()}`
        : "runtime_spawn_failed";
      startupFailure = new RuntimeStartupError(code, { phase: "spawn" });
    });
    child.once("exit", (code) => {
      if (this.child === child) this.child = null;
      const stage = consumeRuntimeStartupStage(this.dataDir, diagnosticToken);
      startupFailure = new RuntimeStartupError(
        stage ? `runtime_stage_${stage}` : runtimeExitDiagnosticCode(code),
        { exitCode: code, stage },
      );
      this.emit("exit", code);
    });
    if (!Number.isSafeInteger(child.pid) || child.pid < 1) {
      await new Promise((resolve) => child.once("error", resolve));
      throw startupFailure ?? new RuntimeStartupError("runtime_spawn_failed", { phase: "spawn" });
    }
    this.child = child;
    this.runtimePid = child.pid;
    issueRuntimeOwnerReceipt(this.dataDir, child.pid, spec.runtimeIdentity, ownerNonce);
    while (!startupFailure) {
      if (await runtimeResponds(this.port, this.dataDir, spec.runtimeIdentity)) {
        consumeRuntimeStartupStage(this.dataDir, diagnosticToken);
        this.emit("ready", this.origin);
        return this.origin;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw startupFailure;
  }

  async restart() {
    await this.stop();
    return this.start();
  }

  async stop() {
    const child = this.child;
    this.child = null;
    const pid = this.runtimePid;
    this.runtimePid = null;
    if (!pid) return;
    if (!child) {
      const receipt = await probeRuntimeOwner(this.port, this.dataDir);
      if (!receipt || receipt.pid !== pid) return;
    }
    await stopRuntimePid(pid, this.port);
  }
}

module.exports = {
  BackendManager,
  DEFAULT_RUNTIME_PORT,
  RuntimeStartupError,
  availableLoopbackPort,
  consumeRuntimeStartupStage,
  issueRuntimeOwnerReceipt,
  packagedRuntimeSpec,
  runtimeIdentity,
  runtimeDiagnosticCode,
  runtimeOwnerNonce,
  runtimeOwnerReceipt,
  runtimeResponds,
  runtimeTerminationSpec,
  writeRuntimeFailureDiagnostic,
};
