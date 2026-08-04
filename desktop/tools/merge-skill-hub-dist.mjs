import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const root = path.resolve(process.argv[2] || "dist");
const hub = path.resolve(process.argv[3] || "skill-hub/dist");
const html = await readFile(path.join(hub, "index.html"), "utf8");
if ((html.match(/<!--__ECOREX_RUNTIME_CONFIG__-->/g) || []).length !== 1) {
  throw new Error("Astro Skill Hub must contain one Runtime config marker");
}
await mkdir(path.join(root, "assets"), { recursive: true });
await cp(path.join(hub, "assets"), path.join(root, "assets"), { recursive: true, errorOnExist: true });
await writeFile(path.join(root, "assets", "skill-hub-page.unhashed-upstream0c214c3.json"), html, { flag: "wx" });
