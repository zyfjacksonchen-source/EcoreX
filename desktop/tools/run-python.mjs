import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const repositoryRoot = resolve(import.meta.dirname, "..", "..");
const candidates = process.platform === "win32"
  ? [resolve(repositoryRoot, ".venv", "Scripts", "python.exe"), "python"]
  : [resolve(repositoryRoot, ".venv", "bin", "python"), "python3", "python"];
const python = candidates.find((candidate) => !candidate.includes(".venv") || existsSync(candidate));
const result = spawnSync(python, process.argv.slice(2), { stdio: "inherit" });

if (result.error) {
  throw result.error;
}
process.exit(result.status ?? 1);
