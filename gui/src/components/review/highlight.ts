// Syntax highlighting for diff lines, plus marking the characters that
// actually changed inside a modified line (as VS Code does).
//
// Only a handful of languages are bundled, to keep the page light. Lines are
// highlighted one at a time, so a construct spanning lines (a block comment)
// may colour imperfectly; the diff itself is never affected.

import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

hljs.registerLanguage("bash", bash);
hljs.registerLanguage("css", css);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("markdown", markdown);
hljs.registerLanguage("python", python);
hljs.registerLanguage("sql", sql);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("xml", xml);
hljs.registerLanguage("yaml", yaml);

const BY_EXTENSION: Record<string, string> = {
  ts: "typescript",
  tsx: "typescript",
  mts: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  py: "python",
  sql: "sql",
  css: "css",
  scss: "css",
  json: "json",
  md: "markdown",
  yml: "yaml",
  yaml: "yaml",
  toml: "yaml",
  sh: "bash",
  bash: "bash",
  html: "xml",
  xml: "xml",
  svg: "xml",
};

export function languageFor(path: string): string | null {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return BY_EXTENSION[ext] ?? null;
}

function escape(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** HTML for one line of code. Safe to inject: hljs escapes its input. */
export function highlightLine(text: string, language: string | null): string {
  if (!language || !text) return escape(text);
  try {
    return hljs.highlight(text, { language, ignoreIllegals: true }).value;
  } catch {
    return escape(text);
  }
}

/**
 * The span that differs between an old and a new version of a line: the part
 * left after trimming their common start and end. Enough to show "this word
 * changed" without a full character diff.
 */
export function changedRange(before: string, after: string): {
  old: [number, number];
  new: [number, number];
} | null {
  if (before === after) return null;
  let start = 0;
  const max = Math.min(before.length, after.length);
  while (start < max && before[start] === after[start]) start++;
  let endBefore = before.length;
  let endAfter = after.length;
  while (endBefore > start && endAfter > start && before[endBefore - 1] === after[endAfter - 1]) {
    endBefore--;
    endAfter--;
  }
  // A line rewritten almost entirely gains nothing from inner marks.
  const changed = Math.max(endBefore - start, endAfter - start);
  if (changed > 0.8 * Math.max(before.length, after.length)) return null;
  return { old: [start, endBefore], new: [start, endAfter] };
}

/**
 * Wrap characters [start, end) of highlighted HTML in <mark>, walking text
 * nodes so the syntax colouring around them survives.
 */
export function markRange(html: string, start: number, end: number): string {
  if (end <= start) return html;
  const holder = document.createElement("span");
  holder.innerHTML = html;
  const walker = document.createTreeWalker(holder, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  while (walker.nextNode()) texts.push(walker.currentNode as Text);

  let offset = 0;
  for (const node of texts) {
    const length = node.data.length;
    const from = Math.max(start, offset);
    const to = Math.min(end, offset + length);
    if (from < to) {
      const local = node.splitText(from - offset);
      local.splitText(to - from);
      const mark = document.createElement("mark");
      local.parentNode!.replaceChild(mark, local);
      mark.appendChild(local);
    }
    offset += length;
  }
  return holder.innerHTML;
}
