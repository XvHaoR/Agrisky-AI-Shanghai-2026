import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import test from "node:test";

const require = createRequire(import.meta.url);
const tailwindConfig = require("../tailwind.config.js");
const postcssConfig = require("../postcss.config.js");

test("Tailwind is incremental and does not reset existing pages", () => {
  assert.equal(tailwindConfig.corePlugins?.preflight, false);
  assert.ok(tailwindConfig.content?.includes("./app/**/*.{js,ts,jsx,tsx,mdx}"));
});

test("PostCSS compiles Tailwind utilities", () => {
  assert.deepEqual(Object.keys(postcssConfig.plugins), ["tailwindcss", "autoprefixer"]);
});

test("global stylesheet exposes only the incremental Tailwind layers", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(css, /@tailwind components;\s*@tailwind utilities;\s*$/);
  assert.doesNotMatch(css, /@tailwind base;/);
});

test("claims stage sizing and toast positioning remain scoped", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  const page = await readFile(new URL("../app/claims/page.tsx", import.meta.url), "utf8");

  assert.match(page, /claims-stage-panel/);
  assert.match(css, /\.claims-workspace:not\(\.claims-workspace--state-init\) \.claims-stage-panel/);
  assert.match(css, /\.claims-shell > \.claims-toast\s*{[^}]*position:\s*fixed;/s);
});
