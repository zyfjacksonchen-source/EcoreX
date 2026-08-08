import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const roots = ["electron", "src", "dist", "runtime-bundle", "package.json"];
const forbidden = [/cowagent/i, /com\.cowagent/i, /cow-agent/i];
const findings = [];

async function scan(relative) {
  const absolute = path.join(desktop, relative);
  const entries = await readdir(absolute, { withFileTypes: true }).catch(() => null);
  if (entries) {
    await Promise.all(entries.map((entry) => scan(path.join(relative, entry.name))));
    return;
  }
  const content = await readFile(absolute).catch(() => null);
  if (!content) return;
  const text = content.toString("utf8");
  if (forbidden.some((pattern) => pattern.test(text))) findings.push(relative);
}

await Promise.all(roots.map(scan));
if (findings.length) {
  throw new Error(`Legacy desktop brand found in: ${findings.sort().join(", ")}`);
}
console.log("e-Mate desktop brand gate passed.");
