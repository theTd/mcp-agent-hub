// A small, safe Markdown subset for agent messages in the transcript view.
//
// Agents answer in Markdown (headings, lists, tables, fenced code, `code`, **bold**, links). The
// renderer escapes every character first and only then adds the few tags it knows, so no input can
// inject markup. Links become anchors only for http(s) URLs; a relative path (a file the agent
// wrote) stays plain text with the path in its tooltip.
import { escapeHtml } from './ui-kit.js';

const FENCE_RE = /^\s*(```|~~~)\s*([\w+#.-]*)\s*$/;
const HEADING_RE = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
const BULLET_RE = /^(\s*)[-*+]\s+(.*)$/;
const ORDERED_RE = /^(\s*)(\d{1,4})[.)]\s+(.*)$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const RULE_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;

/** Inline Markdown on one already-unescaped line: escapes, then code, links, bold, italics. */
export function renderInline(text) {
  const codes = [];
  // Code spans first: their content is literal and must not see any other rule.
  // \u0000 / \u0001 mark the placeholders below, so the input may not contain them.
  let out = String(text ?? '').replace(/[\u0000\u0001]/g, '').replace(/`([^`\n]+)`/g, (_, code) => {
    codes.push(`<code>${escapeHtml(code)}</code>`);
    return `\u0000${codes.length - 1}\u0000`;
  });
  const links = [];
  out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
    links.push(linkHtml(label, href));
    return `\u0001${links.length - 1}\u0001`;
  });
  out = escapeHtml(out)
    .replace(/(^|[\s(（])(https?:\/\/[^\s<>"')）]+[^\s<>"')）.,;:!?。，；：！？])/g,
      (_, lead, url) => `${lead}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`)
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
    .replace(/(^|[\s(（])\*([^*\s][^*\n]*?)\*(?=[\s).,;:!?）。，；：！？]|$)/g, '$1<em>$2</em>');
  return out
    .replace(/\u0001(\d+)\u0001/g, (_, i) => links[Number(i)])
    .replace(/\u0000(\d+)\u0000/g, (_, i) => codes[Number(i)]);
}

function linkHtml(label, href) {
  const text = renderInlineText(label);
  if (/^https?:\/\//i.test(href)) {
    return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  }
  return `<span class="md-ref" title="${escapeHtml(href)}">${text}</span>`;
}

function renderInlineText(text) {
  return escapeHtml(text).replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}

function splitRow(line) {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1);
  return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'));
}

function tableHtml(header, aligns, rows) {
  const align = (i) => (aligns[i] ? ` style="text-align:${aligns[i]}"` : '');
  const head = header.map((cell, i) => `<th${align(i)}>${renderInline(cell)}</th>`).join('');
  const body = rows.map((row) => `<tr>${header.map((_, i) => `<td${align(i)}>${renderInline(row[i] ?? '')}</td>`).join('')}</tr>`).join('');
  return `<div class="md-table"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/** Markdown text -> HTML string (block level). */
export function renderMarkdown(source) {
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const html = [];
  let paragraph = [];
  let list = null; // { tag, items: [] }

  const flushParagraph = () => {
    if (paragraph.length) html.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (list) html.push(`<${list.tag}>${list.items.map((item) => `<li>${item}</li>`).join('')}</${list.tag}>`);
    list = null;
  };
  const flush = () => { flushParagraph(); flushList(); };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const fence = line.match(FENCE_RE);
    if (fence) {
      flush();
      const body = [];
      let j = i + 1;
      while (j < lines.length && !lines[j].trim().startsWith(fence[1])) body.push(lines[j++]);
      const lang = fence[2] ? ` data-lang="${escapeHtml(fence[2])}"` : '';
      html.push(`<pre class="md-code"${lang}><code>${escapeHtml(body.join('\n'))}</code></pre>`);
      i = j;
      continue;
    }
    if (!line.trim()) { flush(); continue; }
    const heading = line.match(HEADING_RE);
    if (heading) {
      flush();
      const level = Math.min(6, heading[1].length + 2);
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }
    if (RULE_RE.test(line) && !paragraph.length) { flush(); html.push('<hr>'); continue; }
    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      flush();
      const header = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map((cell) => (
        cell.startsWith(':') && cell.endsWith(':') ? 'center' : cell.endsWith(':') ? 'right' : ''));
      const rows = [];
      let j = i + 2;
      while (j < lines.length && lines[j].includes('|') && lines[j].trim()) rows.push(splitRow(lines[j++]));
      html.push(tableHtml(header, aligns, rows));
      i = j - 1;
      continue;
    }
    const quote = line.match(QUOTE_RE);
    if (quote) {
      flush();
      const body = [quote[1]];
      while (i + 1 < lines.length && QUOTE_RE.test(lines[i + 1])) body.push(lines[++i].match(QUOTE_RE)[1]);
      html.push(`<blockquote>${body.map(renderInline).join('<br>')}</blockquote>`);
      continue;
    }
    const bullet = line.match(BULLET_RE);
    const ordered = bullet ? null : line.match(ORDERED_RE);
    if (bullet || ordered) {
      flushParagraph();
      const tag = bullet ? 'ul' : 'ol';
      const indent = (bullet ? bullet[1] : ordered[1]).length;
      const text = bullet ? bullet[2] : ordered[3];
      if (!list || list.tag !== tag) {
        flushList();
        list = { tag, items: [] };
        if (ordered && ordered[2] !== '1') list.tag = 'ol';
      }
      const item = renderInline(text);
      list.items.push(indent >= 2 ? `<span class="md-sub">${item}</span>` : item);
      continue;
    }
    if (list && /^\s{2,}\S/.test(line)) {
      // A wrapped list item continues on an indented line.
      list.items[list.items.length - 1] += `<br>${renderInline(line.trim())}`;
      continue;
    }
    flushList();
    paragraph.push(line);
  }
  flush();
  return html.join('');
}
