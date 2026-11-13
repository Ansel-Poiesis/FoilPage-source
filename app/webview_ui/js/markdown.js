// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* FoilPage Markdown 渲染（子集：标题/粗斜体/行内代码/链接/列表/引用/分隔线/代码块） */
'use strict';

window.Markdown = (() => {
  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function inline(text) {
    let out = "";
    const re = /(`+)(.+?)\1|(\*\*|__)(.+?)\3|(\*|_)(.+?)\5|\[([^\]]+)\]\([^)]*\)/g;
    let pos = 0, m;
    while ((m = re.exec(text)) !== null) {
      out += esc(text.slice(pos, m.index));
      if (m[2] !== undefined) out += "<code>" + esc(m[2]) + "</code>";
      else if (m[4] !== undefined) out += "<strong>" + inline(m[4]) + "</strong>";
      else if (m[6] !== undefined) out += "<em>" + inline(m[6]) + "</em>";
      else if (m[7] !== undefined) out += esc(m[7]);
      pos = m.index + m[0].length;
    }
    out += esc(text.slice(pos));
    return out;
  }

  function render(md) {
    const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let i = 0, listType = null, inCode = false, codeBuf = [];

    function closeList() {
      if (listType) { html.push("</" + listType + ">"); listType = null; }
    }
    function flushCode() {
      if (inCode) {
        html.push("<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>");
        codeBuf = []; inCode = false;
      }
    }

    for (; i < lines.length; i++) {
      const line = lines[i];
      const stripped = line.trim();

      if (stripped.startsWith("```")) {
        flushCode(); closeList(); inCode = true; continue;
      }
      if (inCode) { codeBuf.push(line); continue; }

      const h = /^(#{1,6})\s+(.*)$/.exec(stripped);
      if (h) {
        flushCode(); closeList();
        const level = h[1].length;
        html.push(`<h${level}>${inline(h[2])}</h${level}>`);
        continue;
      }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(stripped)) {
        flushCode(); closeList(); html.push("<hr>"); continue;
      }
      if (/^>\s?/.test(stripped)) {
        flushCode(); closeList();
        html.push("<blockquote>" + inline(stripped.replace(/^>\s?/, "")) + "</blockquote>");
        continue;
      }
      const ul = /^[-*+]\s+(.*)$/.exec(stripped);
      if (ul) {
        flushCode();
        if (listType !== "ul") { closeList(); html.push("<ul>"); listType = "ul"; }
        html.push("<li>" + inline(ul[1]) + "</li>");
        continue;
      }
      const ol = /^\d+[.)]\s+(.*)$/.exec(stripped);
      if (ol) {
        flushCode();
        if (listType !== "ol") { closeList(); html.push("<ol>"); listType = "ol"; }
        html.push("<li>" + inline(ol[1]) + "</li>");
        continue;
      }
      flushCode(); closeList();
      if (stripped === "") {
        continue;
      }
      html.push("<p>" + inline(line) + "</p>");
    }
    flushCode(); closeList();
    return html.join("\n");
  }

  return { render, esc };
})();
