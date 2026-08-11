import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const tokens = readFileSync(new URL("../src/styles/tokens.css", import.meta.url), "utf8");
const primitives = readFileSync(new URL("../src/v1/styles/primitives.css", import.meta.url), "utf8");
const layout = readFileSync(new URL("../src/v1/styles/layout.css", import.meta.url), "utf8");
const features = readFileSync(new URL("../src/v1/styles/features.css", import.meta.url), "utf8");
const plainLanguage = readFileSync(new URL("../src/v1/styles/plain-language.css", import.meta.url), "utf8");
const componentCss = [primitives, layout, features, plainLanguage];
const v1Root = fileURLToPath(new URL("../src/v1/", import.meta.url));

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = `${directory}/${entry.name}`;
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.(?:ts|tsx)$/.test(entry.name) ? [path] : [];
  });
}

function rule(source, marker) {
  const start = source.indexOf(marker);
  assert.notEqual(start, -1, `missing CSS rule: ${marker}`);
  const open = source.indexOf("{", start);
  const close = source.indexOf("}", open + 1);
  assert.ok(open > start && close > open, `invalid CSS rule: ${marker}`);
  return source.slice(open + 1, close);
}

function leafRules(source) {
  const found = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  for (const match of source.matchAll(pattern)) {
    found.push({ selector: match[1].trim(), declarations: match[2] });
  }
  return found;
}

const CONTROL_BASE_CLASSES = [
  "ex-button",
  "ex-icon-button",
  "ex-new-task",
  "ex-sidebar-action",
  "ex-task-row",
  "ex-composer-tool",
  "ex-composer-model-trigger",
  "ex-permission-inline",
  "ex-usage-summary",
  "ex-search-result",
  "ex-search-continue",
  "ex-skill-card-main",
  "ex-skills-back",
  "ex-new-project-trigger",
  "ex-disposition",
  "ex-send-button",
  "ex-artifact-primary",
  "ex-artifact-sheet-action",
];
const CONTEXT_CONTROL_OWNERS = [
  "ex-composer-attachment",
  "ex-new-conversation-options",
  "ex-retouch-review-tabs",
  "ex-retouch-region-list",
  "ex-skills-tabs",
  "ex-skill-category-grid",
  "ex-settings-page-nav",
];
const CONTEXT_CONTROL_SELECTORS = [
  ".ex-composer-attachment button",
  ".ex-new-conversation-options button",
  ".ex-retouch-review-tabs button",
  ".ex-retouch-region-list > button",
  ".ex-skills-tabs button",
  ".ex-skill-category-grid button",
  ".ex-settings-page-nav button",
];
const STRUCTURAL_BUTTON_EXCEPTIONS = [
  "ex-sidebar-scrim",
  "ex-retouch-result-media",
  "ex-image-gallery-media",
  "ex-input-attachment-preview-trigger",
  "ex-timeline-jump-button",
  "ex-process-toggle",
  // A semantic switch is a persistent state indicator, not an ordinary
  // command button. Shape Lock explicitly permits toggle tracks to be pills.
  "ex-skill-switch",
];

function classAttributeSource(attributes, sourceFile) {
  const attribute = attributes.properties.find((property) => (
    ts.isJsxAttribute(property) && property.name.getText(sourceFile) === "className"
  ));
  return attribute?.getText(sourceFile) ?? "";
}

function sourceHasClass(source, className) {
  return new RegExp(`\\b${className}\\b`, "u").test(source);
}

function jsxControlInventory() {
  const controls = [];
  for (const path of sourceFiles(v1Root).filter((candidate) => candidate.endsWith(".tsx"))) {
    const source = readFileSync(path, "utf8");
    const sourceFile = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const visit = (node) => {
      if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        const tag = node.tagName.getText(sourceFile);
        if (tag === "button" || tag === "a") {
          const classSource = classAttributeSource(node.attributes, sourceFile);
          const roleSource = node.attributes.properties
            .filter((property) => ts.isJsxAttribute(property) && property.name.getText(sourceFile) === "role")
            .map((property) => property.getText(sourceFile))
            .join(" ");
          const directClasses = [...classSource.matchAll(/\b(?:ex|is)-[a-z0-9-]+\b/gu)].map((match) => match[0]);
          const ancestorClassSources = [];
          let parent = ts.isJsxOpeningElement(node) ? node.parent.parent : node.parent;
          while (parent) {
            if (ts.isJsxElement(parent)) {
              ancestorClassSources.push(classAttributeSource(parent.openingElement.attributes, sourceFile));
            }
            parent = parent.parent;
          }
          const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
          controls.push({ path, line, tag, classSource, roleSource, directClasses, ancestorClassSources });
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }
  return controls;
}

test("Codex-density typography is owned by the system UI and code tokens", () => {
  assert.match(
    tokens,
    /--font-ui:\s*-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif;/,
  );
  assert.match(tokens, /--font-code:\s*ui-monospace,/);
  assert.match(tokens, /--font-display:\s*var\(--font-ui\);/);
  assert.match(tokens, /--font-body:\s*var\(--font-ui\);/);
  assert.match(tokens, /--font-mono:\s*var\(--font-code\);/);
  assert.match(tokens, /--text-body-size:\s*14px;\s*--text-body-line:\s*22px;/);
  assert.match(tokens, /--text-ui-size:\s*13px;\s*--text-ui-line:\s*20px;/);
  assert.match(tokens, /--text-caption-size:\s*12px;\s*--text-caption-line:\s*16px;/);
  assert.match(tokens, /--font-size-ui:\s*var\(--text-ui-size\);/);

  for (const css of componentCss) {
    for (const line of css.split(/\r?\n/)) {
      if (!line.includes("font-family:")) continue;
      assert.match(
        line,
        /font-family:\s*(?:var\(--font-(?:ui|code|display|body|mono|sans)\)|inherit);/,
        `raw component font is forbidden: ${line.trim()}`,
      );
    }
  }
  for (const path of sourceFiles(v1Root)) {
    assert.doesNotMatch(
      readFileSync(path, "utf8"),
      /\bfontFamily\s*:/,
      `inline raw fonts bypass the typography tokens: ${path}`,
    );
  }
  assert.doesNotMatch(
    primitives,
    /\.ex-app-shell\s+button[\s\S]{0,180}?font:\s*inherit;/,
    "a broad font shorthand must not override the 13/20 control typography",
  );
});

test("session summaries stay left aligned", () => {
  const taskRows = rule(layout, ".ex-task-row,\n.ex-sidebar-action");
  assert.match(taskRows, /text-align:\s*left;/);
});

test("ordinary controls are frameless until hover, focus, or active state", () => {
  const idle = rule(primitives, ".ex-button,\n.ex-icon-button,");
  assert.match(idle, /border:\s*1px solid var\(--control-idle-border\);/);
  assert.match(idle, /background:\s*var\(--control-idle-surface\);/);
  assert.match(idle, /box-shadow:\s*none;/);
  assert.match(idle, /font-size:\s*var\(--text-ui-size\);/);
  assert.match(idle, /line-height:\s*var\(--text-ui-line\);/);
  assert.doesNotMatch(idle, /background:\s*var\(--color-(?:surface|raised|hover)\);/);
  assert.doesNotMatch(idle, /border:\s*1px solid var\(--color-rule/);

  for (const marker of [
    ".ex-button:not(.is-primary):not(.is-danger):focus-visible,",
    ".ex-button:not(.is-primary):not(.is-danger):not(:disabled):active,",
    ".ex-button:not(.is-primary):not(.is-danger):not(:disabled):hover,",
  ]) {
    const state = rule(primitives, marker);
    assert.match(state, /border-color:\s*var\(--control-hover-border\);/);
    assert.match(state, /background:\s*var\(--control-hover-surface\);/);
  }

  const primary = rule(primitives, ".ex-button.is-primary,\n.ex-send-button {");
  assert.match(primary, /border-color:\s*transparent;/);
  assert.match(primary, /background:\s*var\(--color-brand\);/);
  const newTaskHover = rule(primitives, ".ex-new-task:not(:disabled):hover,");
  assert.match(newTaskHover, /border-color:\s*var\(--control-hover-border\);/);
  assert.match(newTaskHover, /background:\s*var\(--control-hover-surface\);/);
  const danger = rule(primitives, ".ex-button.is-danger {");
  assert.match(danger, /border-color:\s*transparent;/);
  assert.match(danger, /background:\s*transparent;/);
  const selected = rule(primitives, ".ex-icon-button.is-selected,");
  assert.match(selected, /border-color:\s*transparent;/);
  assert.match(selected, /background:\s*var\(--color-selected\);/);

  const primaryHover = rule(primitives, ".ex-button.is-primary:hover,");
  assert.match(primaryHover, /border-color:\s*var\(--color-brand-strong\);/);
  const dangerHover = rule(primitives, ".ex-button.is-danger:not(:disabled):hover {");
  assert.match(dangerHover, /border-color:\s*var\(--color-danger\);/);
});

test("every native button and anchor-as-button is owned by the density contract", () => {
  const controls = jsxControlInventory();
  for (const control of controls) {
    const buttonLikeAnchor = control.tag === "a" && (
      /role\s*=\s*["'{]?button\b/u.test(control.roleSource)
      || CONTROL_BASE_CLASSES.some((className) => sourceHasClass(control.classSource, className))
    );
    if (control.tag !== "button" && !buttonLikeAnchor) continue;

    const hasBase = CONTROL_BASE_CLASSES.some((className) => sourceHasClass(control.classSource, className));
    const hasContextOwner = CONTEXT_CONTROL_OWNERS.some((className) => (
      control.ancestorClassSources.some((source) => sourceHasClass(source, className))
    ));
    const structuralException = STRUCTURAL_BUTTON_EXCEPTIONS.some((className) => (
      sourceHasClass(control.classSource, className)
    ));
    assert.ok(
      hasBase || hasContextOwner || structuralException,
      `unowned ${control.tag} can bypass frameless density: ${control.path}:${control.line} (${control.classSource || "no class"})`,
    );
  }

  const idleRule = leafRules(primitives).find(({ declarations }) => (
    /border:\s*1px solid var\(--control-idle-border\);/u.test(declarations)
    && /font-size:\s*var\(--text-ui-size\);/u.test(declarations)
  ));
  assert.ok(idleRule, "missing shared idle control rule");
  for (const selector of CONTEXT_CONTROL_SELECTORS) {
    assert.ok(
      idleRule.selector.includes(selector),
      `context control bypasses shared 13/20 frameless baseline: ${selector}`,
    );
  }
});

test("feature styles cannot quietly re-box ordinary buttons", () => {
  const directButtonClasses = new Set(
    jsxControlInventory()
      .filter((control) => control.tag === "button" || CONTROL_BASE_CLASSES.some((name) => sourceHasClass(control.classSource, name)))
      .flatMap((control) => control.directClasses)
      .filter((className) => className.startsWith("ex-") && !STRUCTURAL_BUTTON_EXCEPTIONS.includes(className)),
  );
  const directClassPattern = [...directButtonClasses]
    .sort((left, right) => right.length - left.length)
    .map((className) => `\\.${className}\\b`)
    .join("|");
  const ordinary = new RegExp(
    `(?:${directClassPattern}|\\.ex-retouch-region-list\\s*>\\s*button\\b|\\.ex-retouch-review-tabs\\s+button\\b)`,
    "u",
  );
  const semanticOrState = /(?::(?:hover|focus-visible|active|disabled)|\.is-(?:primary|danger|current|active|selected)|\[(?:aria-pressed|data-state)|\.ex-(?:new-task|send-button)\b)/;
  const harmlessBorder = /^(?:0(?:\s+\w+)*|none|transparent|1px solid transparent|1px solid var\(--control-idle-border\)|var\(--control-idle-border\))$/;
  const harmlessSurface = /^(?:none|transparent|var\(--control-idle-surface\))$/;
  const harmlessOutline = /^(?:0|none|transparent)$/;

  for (const css of componentCss) {
    for (const { selector, declarations } of leafRules(css)) {
      for (const individualSelector of selector.split(",").map((item) => item.trim())) {
        if (!ordinary.test(individualSelector) || semanticOrState.test(individualSelector)) continue;
        for (const match of declarations.matchAll(/(?:^|;)\s*(border(?:-(?:top|right|bottom|left|block|inline|color))?|background(?:-color)?|box-shadow|outline(?:-color)?)\s*:\s*([^;]+)/g)) {
          const [, property, rawValue] = match;
          const value = rawValue.trim();
          const harmless = property.startsWith("background")
            ? harmlessSurface.test(value)
            : property === "box-shadow"
              ? value === "none"
              : property.startsWith("outline")
                ? harmlessOutline.test(value)
              : harmlessBorder.test(value);
          assert.ok(harmless, `ordinary control has a permanent ${property}: ${individualSelector} -> ${value}`);
        }
      }
    }
  }
});

test("chat, connector, and office artifact rows use sparse framing", () => {
  assert.doesNotMatch(features, /\.ex-message\s*\+\s*\.ex-message\s*\{[^}]*border/s);
  assert.doesNotMatch(
    rule(features, ".ex-connector-row {"),
    /\bborder(?:-(?:top|right|bottom|left|color|style|width))?\s*:/,
  );
  const officeRow = rule(features, ".ex-artifact.is-row {");
  assert.match(officeRow, /border-color:\s*transparent;/);
  assert.match(officeRow, /background:\s*transparent;/);
  const officeHover = rule(features, ".ex-artifact.is-row:hover {");
  assert.match(officeHover, /border-color:\s*var\(--control-hover-border\);/);
});

test("connector workspace and contextual menus retain compact desktop density", () => {
  assert.match(rule(features, ".ex-connector-catalog-panel {"), /width:\s*100%;/u);
  assert.match(rule(features, ".ex-connector-catalog-panel {"), /padding:\s*var\(--space-4\);/u);
  assert.match(rule(features, ".ex-connector-row {"), /padding:\s*var\(--space-3\);/u);
  assert.match(rule(primitives, ".ex-menu-item {"), /min-height:\s*32px;/u);
  assert.match(rule(primitives, ".ex-menu-item {"), /padding:\s*var\(--space-1\) var\(--space-2\);/u);
  assert.match(rule(layout, ".ex-account-menu {"), /width:\s*192px;/u);
});
