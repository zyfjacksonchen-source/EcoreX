import { defineConfig } from "astro/config";

export default defineConfig({
  base: "/ecorex-agent/skills",
  output: "static",
  outDir: "./dist",
  publicDir: "./public",
});
