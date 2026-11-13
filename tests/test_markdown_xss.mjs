// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// 红队：FoilPage 自研 Markdown 渲染器 XSS 收敛测试。
// 直接加载 app/webview_ui/js/markdown.js（window.Markdown）并断言输出不包含
// 可执行 HTML / javascript: / file: 链接。
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.resolve(__dirname, "../app/webview_ui/js/markdown.js"),
  "utf-8",
);

const sandbox = { window: {} };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const Markdown = sandbox.window.Markdown;

const VECTORS = [
  "<img src=x onerror=alert(1)>",
  "<script>alert(1)</script>",
  "[x](javascript:alert(1))",
  "[x](file:///etc/passwd)",
  "[x](data:text/html,<script>alert(1)</script>)",
  "**<img src=x onerror=alert(1)>**",
  "__<script>alert(1)</script>__",
  "*<svg onload=alert(1)>*",
  "_<iframe src=file:///etc/passwd>_",
  "`<img src=x onerror=alert(1)>`",
  "`` ` <img src=x onerror=alert(1)> ` ``",
  "# <img src=x onerror=alert(1)>",
  "### <script>alert(1)</script>",
  "- <img src=x onerror=alert(1)>",
  "+ <script>alert(1)</script>",
  "1. <img src=x onerror=alert(1)>",
  "> <img src=x onerror=alert(1)>",
  "<img src=x onerror=alert(1)>\n---\n[x](javascript:alert(2))",
  "```\n<script>alert(1)</script>\n```",
  "```\n<img src=x onerror=alert(1)>\n```",
  "&lt;img src=x onerror=alert(1)&gt;",
  "[x](JaVaScRiPt:alert(1))",
  "[x](java\nscript:alert(1))",
  "[x](\u0000javascript:alert(1))",
];

function assertSafe(html) {
  assert.ok(!/<(img|script|iframe|svg|object|embed|style|link|meta|form)[\s>]/i.test(html),
    `注入标签未转义: ${html}`);
  // 事件属性只审查真实元素标签内（转义后的 &lt;img ...&gt; 是纯文本，允许出现）
  assert.ok(!/<[a-z][^>]*\s(?:onerror|onload|onclick|onmouseover|onfocus|onmouseenter)\s*=/i.test(html),
    `事件属性残留: ${html}`);
  assert.ok(!/<a\s[^>]*href\s*=/i.test(html), `链接 href 被渲染: ${html}`);
  assert.ok(!/<[a-z][^>]*\s(?:href|src)\s*=\s*["']?(?:javascript|file|data):/i.test(html),
    `危险协议被渲染为属性: ${html}`);
}

test("markdown inline 注入向量全部转义", () => {
  for (const vector of VECTORS) {
    const html = Markdown.render(vector);
    assertSafe(html);
    // 原文内容必须以纯文本形式保留（可被 &amp; 等实体化，但不可消失）
    const plain = html.replace(/<[^>]+>/g, "");
    assert.ok(plain.length > 0, `向量被整体丢弃: ${vector}`);
  }
});

test("markdown 正常语法仍可用", () => {
  const html = Markdown.render("# 标题\n\n**粗体** 与 *斜体* 和 `代码`\n\n- 列表项\n\n> 引用");
  assert.ok(html.includes("<h1>"));
  assert.ok(html.includes("<strong>"));
  assert.ok(html.includes("<em>"));
  assert.ok(html.includes("<code>"));
  assert.ok(html.includes("<ul>"));
  assert.ok(html.includes("<blockquote>"));
});

test("markdown 链接只显示文本不渲染 href", () => {
  const html = Markdown.render("[FoilPage](https://example.com/a?x=1)");
  assert.ok(html.includes("FoilPage"));
  assert.ok(!html.includes("<a"));
  assert.ok(!html.includes("example.com"));
});

test("markdown 嵌套 strong/em 中注入被递归转义", () => {
  const html = Markdown.render("**外层 *<img src=x onerror=alert(1)>* 内层**");
  assertSafe(html);
  assert.ok(html.includes("<strong>"));
});
