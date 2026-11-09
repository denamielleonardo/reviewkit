// Small, dependency-free Markdown renderer (headings, lists, tables, code, quotes, inline styles).
(function () {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = esc(s);
    s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
  }

  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

  function render(src) {
    const lines = src.replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      const fence = line.match(/^\s*(```|~~~)\s*([\w#+.-]*)/);
      if (fence) {
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i++]);
        i++;
        out.push(`<pre><code class="lang-${esc(fence[2] || "")}">${esc(buf.join("\n"))}</code></pre>`);
        continue;
      }
      if (!line.trim()) { i++; continue; }

      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { const n = h[1].length; out.push(`<h${n}>${inline(h[2].replace(/\s#+\s*$/, ""))}</h${n}>`); i++; continue; }

      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

      if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        out.push("<table><thead><tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
          rows.map((r) => "<tr>" + head.map((_, k) => `<td>${inline(r[k] || "")}</td>`).join("") + "</tr>").join("") +
          "</tbody></table>");
        continue;
      }

      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push(`<blockquote>${render(buf.join("\n"))}</blockquote>`);
        continue;
      }

      const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (li) {
        const ordered = /\d/.test(li[2]);
        const base = li[1].length;
        const items = [];
        while (i < lines.length) {
          const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
          if (m && m[1].length <= base + 1) { items.push([m[3]]); i++; continue; }
          if (lines[i].trim() && /^\s+/.test(lines[i]) && items.length) { items[items.length - 1].push(lines[i].replace(/^\s{0,4}/, "")); i++; continue; }
          if (!lines[i].trim() && i + 1 < lines.length && /^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i + 1])) { i++; continue; }
          break;
        }
        const tag = ordered ? "ol" : "ul";
        out.push(`<${tag}>` + items.map((it) => {
          const [first, ...rest] = it;
          const box = first.match(/^\[([ xX])\]\s+(.*)$/);
          const head = box ? `${box[1] === " " ? "☐" : "☑"} ${inline(box[2])}` : inline(first);
          return `<li>${head}${rest.length ? render(rest.join("\n")) : ""}</li>`;
        }).join("") + `</${tag}>`);
        continue;
      }

      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*```|\s*~~~|\s*>|\s*([-*+]|\d+[.)])\s)/.test(lines[i]) &&
        !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))) buf.push(lines[i++]);
      if (!buf.length) { buf.push(lines[i++]); }
      out.push(`<p>${buf.map(inline).join("<br>")}</p>`);
    }
    return out.join("\n");
  }

  window.renderMarkdown = render;
})();
