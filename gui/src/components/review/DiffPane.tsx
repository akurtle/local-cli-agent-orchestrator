import { useMemo } from "react";
import type { DiffHunk, DiffLine, FileDiff } from "../../types";
import { changedRange, highlightLine, languageFor, markRange } from "./highlight";

// One file's diff, drawn side by side or inline, with line numbers, syntax
// colours, and the changed characters inside a modified line marked.

export type Mode = "split" | "inline";

interface Cell {
  line: DiffLine;
  html: string;
}

interface SplitRow {
  left: Cell | null;
  right: Cell | null;
}

/**
 * Pair each run of removed lines with the added lines that follow it, row by
 * row, the way a side-by-side diff lines them up. Paired lines get their
 * changed characters marked.
 */
function cellsFor(hunk: DiffHunk, language: string | null): { split: SplitRow[]; inline: Cell[] } {
  const split: SplitRow[] = [];
  const inline: Cell[] = [];
  let dels: DiffLine[] = [];
  let adds: DiffLine[] = [];

  const cell = (line: DiffLine, range?: [number, number]): Cell => {
    let html = highlightLine(line.text, language);
    if (range) html = markRange(html, range[0], range[1]);
    return { line, html };
  };

  const flush = () => {
    const rows = Math.max(dels.length, adds.length);
    const left: Cell[] = [];
    const right: Cell[] = [];
    for (let i = 0; i < rows; i++) {
      const d = dels[i];
      const a = adds[i];
      const range = d && a ? changedRange(d.text, a.text) : null;
      if (d) left.push(cell(d, range?.old));
      if (a) right.push(cell(a, range?.new));
      split.push({ left: d ? left[left.length - 1] : null, right: a ? right[right.length - 1] : null });
    }
    inline.push(...left, ...right);
    dels = [];
    adds = [];
  };

  for (const line of hunk.lines) {
    if (line.kind === "del") {
      if (adds.length) flush();
      dels.push(line);
    } else if (line.kind === "add") {
      adds.push(line);
    } else {
      flush();
      const c = cell(line);
      split.push({ left: c, right: c });
      inline.push(c);
    }
  }
  flush();
  return { split, inline };
}

function Code({ cell }: { cell: Cell | null }) {
  if (!cell) return <span className="code empty" aria-hidden="true" />;
  return (
    <span
      className={`code k-${cell.line.kind}`}
      // hljs output (or our escape) only: never raw file text.
      dangerouslySetInnerHTML={{ __html: cell.html || "&#8203;" }}
    />
  );
}

function HunkView({ hunk, language, mode }: { hunk: DiffHunk; language: string | null; mode: Mode }) {
  const { split, inline } = useMemo(() => cellsFor(hunk, language), [hunk, language]);
  const heading = (
    <div className="hunk-head">
      <span className="mono">
        @@ −{hunk.old_start} +{hunk.new_start} @@
      </span>
      {hunk.header && <span className="hunk-context mono">{hunk.header}</span>}
    </div>
  );

  if (mode === "inline") {
    return (
      <div className="hunk">
        {heading}
        <div className="rows inline-rows">
          {inline.map((c, i) => (
            <div key={i} className={`row k-${c.line.kind}`}>
              <span className="ln">{c.line.old ?? ""}</span>
              <span className="ln">{c.line.new ?? ""}</span>
              <span className="sign" aria-hidden="true">
                {c.line.kind === "add" ? "+" : c.line.kind === "del" ? "−" : ""}
              </span>
              <Code cell={c} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="hunk">
      {heading}
      <div className="rows split-rows">
        {split.map((row, i) => (
          <div key={i} className="row">
            <span className={`ln ${row.left ? `k-${row.left.line.kind}` : "filler"}`}>
              {row.left?.line.old ?? ""}
            </span>
            <Code cell={row.left?.line.kind === "context" || row.left?.line.kind === "del" ? row.left : null} />
            <span className={`ln ${row.right ? `k-${row.right.line.kind}` : "filler"}`}>
              {row.right?.line.new ?? ""}
            </span>
            <Code cell={row.right?.line.kind === "context" || row.right?.line.kind === "add" ? row.right : null} />
          </div>
        ))}
      </div>
    </div>
  );
}

export function DiffPane({ file, mode }: { file: FileDiff; mode: Mode }) {
  const language = languageFor(file.path);

  if (file.binary) return <p className="pane-note">Binary file. Its contents can't be shown as text.</p>;
  if (file.too_large) {
    return (
      <p className="pane-note">
        This file changed by {file.added + file.removed} lines, too many to draw here. Open it in your
        editor to review it.
      </p>
    );
  }
  if (!file.hunks.length) {
    return (
      <p className="pane-note">
        {file.status === "renamed" ? "Renamed without changes to its contents." : "No line changes."}
      </p>
    );
  }
  return (
    <div className={`diff-body mode-${mode}`}>
      {file.hunks.map((hunk, i) => (
        <HunkView key={`${file.path}-${i}`} hunk={hunk} language={language} mode={mode} />
      ))}
    </div>
  );
}
