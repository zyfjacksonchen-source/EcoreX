const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

const DEFAULT_RUNTIME_PORT = 8765;

function packagedRuntimeSpec(resourcesPath, dataDir, port, targetPlatform = process.platform) {
  try {
    const runtimeRoot = path.join(resourcesPath, "runtime");
    const payload = path.join(runtimeRoot, "payload");
    const command = path.join(payload, "bin", targetPlatform === "win32" ? "ecorex.exe" : "ecorex");
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
        PLAYWRIGHT_BROWSERS_PATH: path.join(dataDir, "ms-playwright"),
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
    };
  } catch {
    return null;
  }
}

function developmentPython() {
  if (process.env.EMATE_PYTHON) return process.env.EMATE_PYTHON;
  return process.platform === "win32" ? "python" : "python3";
}

function issueRuntimeOwnerReceipt(dataDir) {
  const directory = path.join(dataDir, "bootstrap");
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const receiptPath = path.join(directory, "runtime-owner.json");
  const temporary = `${receiptPath}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`;
  const nonce = crypto.randomBytes(32).toString("base64url");
  fs.writeFileSync(temporary, JSON.stringify({ schema_version: 1, nonce }), { mode: 0o600 });
  fs.renameSync(temporary, receiptPath);
  return nonce;
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

function terminateRuntimeProcess(child, force = false) {
  const termination = runtimeTerminationSpec(child.pid, force);
  if (!termination) return;
  if (termination.command) {
    const killer = spawn(termination.command, termination.args, { stdio: "ignore", windowsHide: true });
    killer.once("error", () => {
      try { child.kill(force ? "SIGKILL" : "SIGTERM"); } catch { /* The exact child already exited. */ }
    });
    return;
  }
  try {
    process.kill(termination.pid, termination.signal);
  } catch {
    try { child.kill(termination.signal); } catch { /* The exact child already exited. */ }
  }
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

    let spec;
    if (this.packaged) {
      spec = packagedRuntimeSpec(this.resourcesPath, this.dataDir, this.port);
      if (!spec) throw new Error("The packaged e-Mate Runtime is missing.");
    } else {
      const payload = process.cwd();
      const isVerifiedPayload = process.env.ECOREX_BOOTSTRAPPED === "1"
        && path.basename(payload) === "payload"
        && path.basename(path.dirname(path.dirname(payload))) === "slots";
      if (!isVerifiedPayload) {
        throw new Error("Development Runtime must be launched through the signed e-Mate Bootstrap.");
      }
      spec = {
        command: developmentPython(),
        args: ["-m", "ecorex.server.cli", "serve", "--host", "127.0.0.1", "--port", String(this.port)],
        cwd: payload,
        environment: { ...process.env, PYTHONUNBUFFERED: "1" },
        windowsHide: true,
        detached: process.platform !== "win32",
      };
    }

    if (await runtimeResponds(this.port, this.dataDir)) {
      this.emit("ready", this.origin);
      return this.origin;
    }
    if (await loopbackPortOccupied(this.port)) {
      throw new Error(`Loopback port ${this.port} is occupied by a process not owned by e-Mate.`);
    }
    if (this.packaged) {
      spec.environment.ECOREX_RUNTIME_OWNER_NONCE = issueRuntimeOwnerReceipt(this.dataDir);
    }

    const child = spawn(spec.command, spec.args, {
      cwd: spec.cwd,
      env: spec.environment,
      stdio: "ignore",
      windowsHide: spec.windowsHide,
      detached: spec.detached,
    });
    this.child = child;
    let startupFailure = null;
    child.once("error", () => {
      startupFailure = new Error("e-Mate Runtime could not be launched.");
    });
    child.once("exit", (code) => {
      if (this.child === child) this.child = null;
      startupFailure = new Error(`e-Mate Runtime stopped during startup (${code ?? "terminated"}).`);
      this.emit("exit", code);
    });

    while (!startupFailure) {
      if (await runtimeResponds(this.port, this.dataDir)) {
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
      terminateRuntimeProcess(child);
      timer = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) terminateRuntimeProcess(child, true);
        setTimeout(finish, 1_000).unref();
      }, 5_000);
      timer.unref();
    });
  }
}

module.exports = {
  BackendManager,
  DEFAULT_RUNTIME_PORT,
  issueRuntimeOwnerReceipt,
  packagedRuntimeSpec,
  runtimeOwnerNonce,
  runtimeResponds,
  runtimeTerminationSpec,
};
