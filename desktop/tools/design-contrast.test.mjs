import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const css = readFileSync(new URL("../src/styles/tokens.css", import.meta.url), "utf8");

function block(start, end) {
  const startIndex = css.indexOf(start);
  assert.notEqual(startIndex, -1, `missing token block: ${start}`);
  const endIndex = css.indexOf(end, startIndex + start.length);
  assert.notEqual(endIndex, -1, `missing token boundary: ${end}`);
  return css.slice(startIndex, endIndex);
}

function token(source, name) {
  const match = source.match(new RegExp(`--${name}:\\s*oklch\\(([^)]+)\\)`));
  assert.ok(match, `missing OKLCH token --${name}`);
  const [lightness, chroma, hue] = match[1].trim().split(/\s+/).slice(0, 3).map(Number);
  return [lightness, chroma, hue];
}

function luminance([lightness, chroma, hue]) {
  const radians = hue * Math.PI / 180;
  const a = chroma * Math.cos(radians);
  const b = chroma * Math.sin(radians);
  const lRoot = lightness + 0.3963377774 * a + 0.2158037573 * b;
  const mRoot = lightness - 0.1055613458 * a - 0.0638541728 * b;
  const sRoot = lightness - 0.0894841775 * a - 1.291485548 * b;
  const l = lRoot ** 3;
  const m = mRoot ** 3;
  const s = sRoot ** 3;
  const red = Math.max(0, Math.min(1, 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s));
  const green = Math.max(0, Math.min(1, -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s));
  const blue = Math.max(0, Math.min(1, -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s));
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrast(left, right) {
  const values = [luminance(left), luminance(right)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

const modes = {
  light: block(':root,\n:root[data-theme="light"] {', ':root[data-theme="dark"] {'),
  dark: block(':root[data-theme="dark"] {', '@media (forced-colors: active)'),
};

for (const [mode, source] of Object.entries(modes)) {
  test(`${mode} design tokens preserve body, caption, accent, and two-ring focus contrast`, () => {
    const surface = token(source, "color-surface");
    const workspace = token(source, "color-workspace-surface");
    const session = token(source, "color-session-emphasis");
    const raised = token(source, "color-raised");
    const ink = token(source, "color-ink");
    const muted = token(source, "color-ink-muted");
    const subtle = token(source, "color-ink-subtle");
    const accent = token(source, "color-accent");
    const accentInk = token(source, "color-accent-ink");
    const focus = token(source, "color-focus");
    const info = token(source, "color-info");
    const success = token(source, "color-success");
    const warning = token(source, "color-warning");
    const danger = token(source, "color-danger");

    assert.ok(contrast(ink, surface) >= 4.5, `${mode}: ink/surface`);
    assert.ok(contrast(ink, workspace) >= 4.5, `${mode}: ink/workspace`);
    assert.ok(contrast(subtle, workspace) >= 4.5, `${mode}: subtle/workspace`);
    assert.ok(contrast(ink, session) >= 4.5, `${mode}: ink/session emphasis`);
    assert.ok(contrast(muted, surface) >= 4.5, `${mode}: muted/surface`);
    assert.ok(contrast(subtle, raised) >= 4.5, `${mode}: subtle/raised`);
    assert.ok(contrast(accentInk, accent) >= 4.5, `${mode}: accent ink/accent`);
    assert.ok(contrast(focus, surface) >= 3, `${mode}: outer focus/surface`);
    assert.ok(contrast(accentInk, accent) >= 3, `${mode}: inner focus/accent`);
    assert.ok(contrast(info, raised) >= 4.5, `${mode}: info/raised`);
    assert.ok(contrast(success, raised) >= 4.5, `${mode}: success/raised`);
    assert.ok(contrast(warning, raised) >= 4.5, `${mode}: warning/raised`);
    assert.ok(contrast(danger, raised) >= 4.5, `${mode}: danger/raised`);
  });
}
