const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
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
      spec.environment.ECOREX_RUNTIME_OWNER_NONCE = issueRuntimeOwnerReceipt(this.dataDir);
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
      };
    }

    const child = spawn(spec.command, spec.args, {
      cwd: spec.cwd,
      env: spec.environment,
      stdio: "ignore",
      windowsHide: spec.windowsHide,
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
  issueRuntimeOwnerReceipt,
  packagedRuntimeSpec,
  runtimeOwnerNonce,
  runtimeResponds,
};
