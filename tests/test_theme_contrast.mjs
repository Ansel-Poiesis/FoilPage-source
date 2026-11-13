// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// 主题对比度守护（00-待办 #9）：--faint / --muted 在每套主题的底色上必须
// 达到 WCAG AA 正文标准（≥4.5:1）。0.13.0 前深色系 faint 普遍只有 3.x、
// 浅色系只有 1.9-2.0；本套件直接从 CSS 实算比值，防止后续调色回退。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

function channel(value) {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}
function luminance(hex) {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}
function contrast(fg, bg) {
  const a = luminance(fg);
  const b = luminance(bg);
  const hi = Math.max(a, b);
  const lo = Math.min(a, b);
  return (hi + 0.05) / (lo + 0.05);
}
function token(body, name) {
  const match = body.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`));
  return match ? match[1] : null;
}
function themeBlocks(css) {
  return [...css.matchAll(/body\[data-theme="([^"]+)"\]\s*\{([^}]*)\}/g)]
    .map((m) => ({ name: m[1], body: m[2] }));
}

const SURFACE_TOKENS = ["bg", "surface", "hover", "navpanel", "badge", "row-alt", "header-bg", "sidebar"];

async function assertThemeContrast(cssPath, label) {
  const css = await readFile(new URL(`../${cssPath}`, import.meta.url), "utf8");
  const seen = new Set();
  for (const { name, body } of themeBlocks(css)) {
    const bg = token(body, "bg");
    if (!bg) continue;
    const surfaces = SURFACE_TOKENS.map((t) => token(body, t)).filter(Boolean);
    if (!surfaces.length) continue;
    const key = `${label}:${name}:${token(body, "faint")}`;
    if (seen.has(key)) continue;
    seen.add(key);
    for (const text of ["faint", "muted"]) {
      const fg = token(body, text);
      if (!fg) continue;
      const worst = Math.min(...surfaces.map((s) => contrast(fg, s)));
      assert.ok(
        worst >= 4.5,
        `${label} [${name}] --${text} ${fg} 最差底色对比度 ${worst.toFixed(2)} < 4.5`,
      );
    }
  }
  return seen.size;
}

test("app.css 每套主题的 faint/muted 对比度 ≥ 4.5:1", async () => {
  const count = await assertThemeContrast("app/webview_ui/css/app.css", "app.css");
  assert.ok(count >= 9, `应覆盖深/白岩主题与 8 套社区主题，实际 ${count} 组`);
});

test("product.css 两套主题的 faint/muted 对比度 ≥ 4.5:1", async () => {
  const count = await assertThemeContrast("app/webview_ui/css/product.css", "product.css");
  assert.ok(count >= 2, `实际 ${count} 组`);
});

test("应用内 toast 具备 aria 播报语义且错误态升级为 alert", async () => {
  const html = await readFile(new URL("../app/webview_ui/index.html", import.meta.url), "utf8");
  assert.match(html, /id="toast"[^>]*role="status"[^>]*aria-live="polite"/);
  const app = await readFile(new URL("../app/webview_ui/js/app.js", import.meta.url), "utf8");
  assert.match(app, /setAttribute\("role", kind === "error" \? "alert" : "status"\)/);
  assert.match(app, /setAttribute\("aria-live", kind === "error" \? "assertive" : "polite"\)/);
});
