import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const read = (path) => readFile(new URL(path, root), "utf8");

test("unauthenticated users see only the account login surface", async () => {
  const [app, login, client, session] = await Promise.all([
    read("src/v1/AppV1.tsx"),
    read("src/v1/components/LoginPage.tsx"),
    read("src/v1/api/runtimeClient.ts"),
    read("src/v1/state/useRuntimeSession.ts"),
  ]);

  const gate = app.indexOf("if (bootstrap && !authenticated)");
  const shell = app.indexOf('<div className="ex-app-shell">');
  assert.ok(gate >= 0 && shell > gate, "login gate must precede the complete app shell");
  assert.match(app, /<LoginPage[\s\S]*onLogin=\{runtime\.loginSession\}/u);
  assert.doesNotMatch(app, /DeviceLoginCard|session\/device|beginDeviceLogin/u);

  assert.match(login, /name="identifier"/u);
  assert.match(login, /autoComplete="username"/u);
  assert.match(login, /type="password"/u);
  assert.match(login, /autoComplete="current-password"/u);
  assert.doesNotMatch(login, /验证码|设备|注册链接|忘记密码/u);

  assert.match(client, /"\/api\/v1\/session\/login"/u);
  assert.match(client, /identifier,\s*password,\s*client_request_id/u);
  assert.match(client, /waitForCredentialRotation/u);
  assert.match(client, /if \(error\.status === 401\) return true/u);
  assert.match(client, /if \(error\.status !== 409\)/u);
  assert.match(session, /if \(!receipt\.restart_scheduled\)/u);
  assert.match(session, /await client\.waitForCredentialRotation\(\{ timeoutMs: 90_000 \}\)/u);
  assert.match(session, /登录已完成，正在自动重新连接 e-Mate/u);
  assert.match(session, /e-Mate 正在重新连接，请稍候/u);
  assert.match(session, /window\.setTimeout\(\(\) => window\.location\.reload\(\), 1_500\)/u);
  assert.match(session, /window\.location\.reload\(\)/u);
});

test("the fixed viewport gives summaries and the conversation independent scrolling", async () => {
  const [layout, features, sidebar] = await Promise.all([
    read("src/v1/styles/layout.css"),
    read("src/v1/styles/features.css"),
    read("src/v1/components/Sidebar.tsx"),
  ]);

  assert.match(layout, /html,\s*body,\s*#root\s*\{[\s\S]*overflow:\s*hidden;/u);
  assert.match(layout, /\.ex-app-shell\s*\{[\s\S]*height:\s*100dvh;[\s\S]*max-height:\s*100dvh;/u);
  assert.match(layout, /\.ex-sidebar\s*\{[\s\S]*overflow:\s*clip;/u);
  assert.match(
    layout,
    /\.ex-task-nav\s*\{[\s\S]*min-height:\s*0;[\s\S]*overflow-y:\s*auto;[\s\S]*overscroll-behavior:\s*contain;/u,
  );
  assert.match(layout, /\.ex-sidebar-footer\s*\{[\s\S]*padding:/u);
  const sidebarBrand = sidebar.indexOf('<div className="ex-sidebar-brand">');
  const sidebarNav = sidebar.indexOf('<nav className="ex-task-nav"');
  const sidebarFooter = sidebar.indexOf('<div className="ex-sidebar-footer">');
  assert.ok(
    sidebarBrand >= 0 && sidebarBrand < sidebarNav && sidebarNav < sidebarFooter,
    "brand/new-task controls and account footer must stay outside the independently scrolling summary navigation",
  );
  assert.match(layout, /\.ex-workspace\s*\{[\s\S]*grid-template-rows:\s*auto auto minmax\(0, 1fr\) auto;/u);
  assert.match(
    layout,
    /\.ex-workspace-bottom\s*\{[\s\S]*max-height:\s*min\(58dvh, 520px\);[\s\S]*grid-template-rows:\s*minmax\(0, 1fr\) auto;/u,
  );
  assert.match(
    features,
    /\.ex-interaction-stack\s*\{[\s\S]*grid-row:\s*1;[\s\S]*overflow-y:\s*auto;/u,
  );
  assert.match(features, /\.ex-composer-region\s*\{[\s\S]*grid-row:\s*2;/u);
  assert.match(
    layout,
    /\.ex-login-page\s*\{[\s\S]*overflow-y:\s*auto;[\s\S]*display:\s*flex;/u,
  );
  assert.match(layout, /\.ex-timeline\s*\{[\s\S]*overflow-y:\s*auto;/u);
  assert.match(layout, /\.ex-workspace-bottom\s*\{[\s\S]*grid-row:\s*4;/u);
  assert.match(
    layout,
    /@media \(max-width:\s*839px\)\s*\{[\s\S]*\.ex-sidebar\s*\{[\s\S]*inset:\s*var\(--space-2\) auto var\(--space-2\) var\(--space-2\);[\s\S]*height:\s*auto;[\s\S]*max-height:\s*none;/u,
  );
  assert.match(
    features,
    /\.ex-workspace\.is-skills\s*\{[^}]*overflow:\s*clip;[^}]*\}/u,
  );
});

test("desktop updater is the only visible update surface", async () => {
  const app = await read("src/v1/AppV1.tsx");
  assert.doesNotMatch(app, /DISMISSED_UPDATE_BANNERS_KEY|updateBannerVisible|runtime\.activateUpdate/u);
  assert.match(app, /label="关闭桌面更新提示"/u);
  assert.match(app, /window\.__ECOREX_RUNTIME__\?\.version/u);
  assert.doesNotMatch(app, /version=\{[^}]*"1\.0\.4"/u);
});
