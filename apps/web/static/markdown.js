// 小型 Markdown 渲染器（无依赖、离线可用）。安全原则：所有文本先转义，只生成白名单标签；
// 链接只允许 http/https/mailto。输出的是 HTML 字符串，可直接放进 innerHTML。
// 支持：标题、段落、换行、无序/有序列表、引用、围栏代码块（未闭合时按代码块显示，适合流式）、
// 表格、分隔线、行内代码、粗体、斜体、删除线、链接。
(function (root) {
  "use strict";

  function esc(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeUrl(raw) {
    const url = String(raw || "").trim();
    if (/^(https?:\/\/|mailto:)/i.test(url) && !/[\s<>"'`]/.test(url)) return url;
    return null;
  }

  // 行内：先拆出行内代码，其余部分转义后再套粗体/斜体/链接。
  function inline(text) {
    const parts = String(text).split(/(`+[^`]*?`+)/);
    let out = "";
    for (const part of parts) {
      const code = /^(`+)([^`]*?)\1$/.exec(part);
      if (code && part.length > 1) {
        out += "<code>" + esc(code[2]) + "</code>";
      } else {
        out += emphasis(part);
      }
    }
    return out;
  }

  function emphasis(text) {
    const links = [];
    // 链接先抽出来，避免 URL 里的 _ * 被当成强调。
    let raw = String(text).replace(/\[([^\]\n]{1,500})\]\(([^()\s]{1,2000})\)/g, (all, label, href) => {
      const url = safeUrl(href);
      links.push(url
        ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${emphasisOnly(esc(label))}</a>`
        : emphasisOnly(esc(label)));
      return `\u0000${links.length - 1}\u0000`;
    });
    raw = emphasisOnly(esc(raw));
    return raw.replace(/\u0000(\d+)\u0000/g, (all, index) => links[Number(index)]);
  }

  function emphasisOnly(html) {
    return html
      .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>")
      .replace(/__(?=\S)([\s\S]*?\S)__/g, "<strong>$1</strong>")
      .replace(/~~(?=\S)([\s\S]*?\S)~~/g, "<del>$1</del>")
      .replace(/(^|[^*\w])\*(?=\S)([^*\n]*?\S)\*(?!\*)/g, "$1<em>$2</em>")
      .replace(/(^|[^_\w])_(?=\S)([^_\n]*?\S)_(?![_\w])/g, "$1<em>$2</em>");
  }

  function splitRow(line) {
    let body = line.trim();
    if (body.startsWith("|")) body = body.slice(1);
    if (body.endsWith("|") && !body.endsWith("\\|")) body = body.slice(0, -1);
    return body.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
  }

  const FENCE = /^\s{0,3}(```+|~~~+)\s*([\w+-]*)\s*$/;
  const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const HR = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  const UL = /^(\s*)[-*+]\s+(.*)$/;
  const OL = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
  const QUOTE = /^\s{0,3}>\s?(.*)$/;
  const TABLE_SEP = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

  function blocks(lines, depth) {
    let html = "";
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      const fence = FENCE.exec(line);
      if (fence) {
        const close = new RegExp("^\\s{0,3}" + fence[1][0] + "{" + fence[1].length + ",}\\s*$");
        const body = [];
        i += 1;
        while (i < lines.length && !close.test(lines[i])) { body.push(lines[i]); i += 1; }
        i += 1; // 跳过闭合行；流式时没有闭合也照样显示成代码块
        const lang = fence[2] ? ` class="language-${esc(fence[2])}"` : "";
        html += `<pre><code${lang}>${esc(body.join("\n"))}</code></pre>`;
        continue;
      }
      const heading = HEADING.exec(line);
      if (heading) {
        const level = heading[1].length;
        html += `<h${level}>${inline(heading[2])}</h${level}>`;
        i += 1;
        continue;
      }
      if (HR.test(line)) { html += "<hr>"; i += 1; continue; }
      if (QUOTE.test(line) && depth < 8) {
        const body = [];
        while (i < lines.length && lines[i].trim() && QUOTE.test(lines[i])) { body.push(QUOTE.exec(lines[i])[1]); i += 1; }
        html += `<blockquote>${blocks(body, depth + 1)}</blockquote>`;
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        const head = splitRow(line);
        const aligns = splitRow(lines[i + 1]).map((cell) =>
          cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : cell.startsWith(":") ? "left" : "");
        const attr = (index) => (aligns[index] ? ` style="text-align:${aligns[index]}"` : "");
        html += "<table><thead><tr>" + head.map((cell, index) => `<th${attr(index)}>${inline(cell)}</th>`).join("") + "</tr></thead><tbody>";
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
          const cells = splitRow(lines[i]);
          html += "<tr>" + head.map((_, index) => `<td${attr(index)}>${inline(cells[index] || "")}</td>`).join("") + "</tr>";
          i += 1;
        }
        html += "</tbody></table>";
        continue;
      }
      if (UL.test(line) || OL.test(line)) {
        html += list(lines, i, depth, (next) => { i = next; });
        continue;
      }
      const para = [];
      while (
        i < lines.length && lines[i].trim() && !FENCE.test(lines[i]) && !HEADING.test(lines[i])
        && !QUOTE.test(lines[i]) && !UL.test(lines[i]) && !OL.test(lines[i]) && !HR.test(lines[i])
      ) { para.push(lines[i].trim()); i += 1; }
      if (!para.length) { para.push(lines[i].trim()); i += 1; }
      html += "<p>" + para.map(inline).join("<br>") + "</p>";
    }
    return html;
  }

  function list(lines, start, depth, done) {
    const first = UL.exec(lines[start]) || OL.exec(lines[start]);
    const ordered = !UL.test(lines[start]);
    const indent = first[1].length;
    const startNo = ordered ? Number(first[2]) : 1;
    let html = ordered ? (startNo !== 1 ? `<ol start="${startNo}">` : "<ol>") : "<ul>";
    let i = start;
    while (i < lines.length) {
      const line = lines[i];
      const match = ordered ? OL.exec(line) : UL.exec(line);
      if (!match || match[1].length !== indent) break;
      const body = [ordered ? match[3] : match[2]];
      i += 1;
      const children = [];
      while (i < lines.length && lines[i].trim()) {
        const child = UL.exec(lines[i]) || OL.exec(lines[i]);
        if (child && child[1].length <= indent) break;
        if (child || /^\s{2,}/.test(lines[i])) {
          children.push(lines[i].slice(Math.min(indent + 2, lines[i].search(/\S/))));
        } else {
          body.push(lines[i].trim());
        }
        i += 1;
      }
      let item = body.map(inline).join("<br>");
      if (children.length && depth < 8) item += blocks(children, depth + 1);
      html += `<li>${item}</li>`;
      if (i < lines.length && !lines[i].trim()) {
        const next = lines[i + 1] || "";
        const again = ordered ? OL.exec(next) : UL.exec(next);
        if (again && again[1].length === indent) { i += 1; continue; }
        break;
      }
    }
    done(i);
    return html + (ordered ? "</ol>" : "</ul>");
  }

  function renderMarkdown(text) {
    const source = String(text == null ? "" : text).replace(/\r\n?/g, "\n");
    return blocks(source.split("\n"), 0);
  }

  root.renderMarkdown = renderMarkdown;
  if (typeof module !== "undefined" && module.exports) module.exports = { renderMarkdown };
})(typeof window !== "undefined" ? window : globalThis);
