import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import type { Actions } from "../../App";
import type { AgentChanges, Review, Snapshot } from "../../types";
import { agentSummaries } from "../summaries";
import { FileTree } from "./FileTree";
import { DiffPane, type Mode } from "./DiffPane";

// The code view: the whole change summarised on top, then every changed file
// on the left and its diff on the right, like VS Code's diff editor.

const MODE_KEY = "agentos-diff-mode";

function storedMode(): Mode {
  try {
    return localStorage.getItem(MODE_KEY) === "inline" ? "inline" : "split";
  } catch {
    return "split";
  }
}

export function CodeReview({
  snapshot,
  source,
  onSource,
  actions,
}: {
  snapshot: Snapshot;
  source: string | null;
  onSource: (agent: string) => void;
  actions: Actions;
}) {
  const sources = snapshot.changes;
  const active = sources.find((c) => c.agent === source) ?? sources[0] ?? null;
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>(storedMode);

  // Refetch when the source changes, or when its totals move (an agent edited
  // something while you were looking).
  const signature = active ? `${active.agent}:${active.files.length}:${active.added}:${active.removed}` : "";
  useEffect(() => {
    if (!active) return;
    let live = true;
    setError(null);
    api
      .get<Review>(`/api/review/${encodeURIComponent(active.agent)}`)
      .then((r) => {
        if (!live) return;
        setReview(r);
        setSelected((current) =>
          current && r.files.some((f) => f.path === current) ? current : r.files[0]?.path ?? null,
        );
      })
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [signature]);

  const files = review?.agent === active?.agent ? review.files : [];
  const index = files.findIndex((f) => f.path === selected);
  const file = index >= 0 ? files[index] : null;

  // A new file starts at its top, however it was chosen.
  useEffect(() => {
    document.querySelector(".diff-scroll")?.scrollTo({ top: 0 });
  }, [selected]);

  const step = useCallback(
    (by: number) => {
      if (!files.length) return;
      const next = (Math.max(index, 0) + by + files.length) % files.length;
      setSelected(files[next].path);
    },
    [files, index],
  );

  // n / p move between files, as long as you're not typing somewhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest("input, select, textarea, dialog") || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "n") step(1);
      if (e.key === "p") step(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step]);

  const setAndStoreMode = (next: Mode) => {
    setMode(next);
    try {
      localStorage.setItem(MODE_KEY, next);
    } catch {
      /* not stored; fine */
    }
  };

  if (!active) {
    return (
      <section className="review empty-review">
        <h2>No code has changed</h2>
        <p className="hint">
          When agents edit files, every change shows up here, file by file. There's nothing uncommitted or
          unmerged right now.
        </p>
      </section>
    );
  }

  return (
    <section className="review" aria-label="Code changes">
      <Summary
        entry={active}
        review={review?.agent === active.agent ? review : null}
        snapshot={snapshot}
        sources={sources}
        onSource={onSource}
        actions={actions}
      />

      {error && <p className="hint">Couldn't load the changes: {error}</p>}

      <div className="review-body">
        <FileTree files={files} selected={selected} onSelect={setSelected} />
        <div className="diff-view">
          {file ? (
            <>
              <div className="diff-head">
                <div className="diff-title">
                  <span className="mono diff-path">
                    {file.status === "renamed" && file.old_path ? `${file.old_path} → ` : ""}
                    {file.path}
                  </span>
                  <span className="diff-counts">
                    <span className="plus">+{file.added}</span> <span className="minus">−{file.removed}</span>
                  </span>
                </div>
                <div className="diff-tools">
                  <div className="segmented" role="group" aria-label="Layout">
                    <button aria-pressed={mode === "split"} onClick={() => setAndStoreMode("split")}>
                      Side by side
                    </button>
                    <button aria-pressed={mode === "inline"} onClick={() => setAndStoreMode("inline")}>
                      Inline
                    </button>
                  </div>
                  <button className="button quiet small" onClick={() => step(-1)} title="Previous file (p)">
                    Previous file
                  </button>
                  <button className="button quiet small" onClick={() => step(1)} title="Next file (n)">
                    Next file
                  </button>
                  <span className="file-position">
                    {index + 1} of {files.length}
                  </span>
                </div>
              </div>
              <div className="diff-scroll">
                <DiffPane file={file} mode={mode} />
              </div>
            </>
          ) : (
            <p className="pane-note">{review ? "Choose a file on the left." : "Loading the changes…"}</p>
          )}
        </div>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ summary

function Summary({
  entry,
  review,
  snapshot,
  sources,
  onSource,
  actions,
}: {
  entry: AgentChanges;
  review: Review | null;
  snapshot: Snapshot;
  sources: AgentChanges[];
  onSource: (agent: string) => void;
  actions: Actions;
}) {
  const said = agentSummaries(entry, snapshot);
  const byStatus = useMemo(() => {
    const counts = { added: 0, modified: 0, deleted: 0, renamed: 0 };
    for (const f of review?.files ?? []) counts[f.status] += 1;
    return counts;
  }, [review]);
  const total = Math.max(1, entry.added + entry.removed);
  const nameOf = (c: AgentChanges) => (c.shared ? "Project folder" : c.agent);

  return (
    <header className="review-summary">
      <div className="summary-top">
        <div>
          <h2>
            {entry.shared
              ? "Uncommitted changes in the project folder"
              : `${entry.agent}'s work on ${entry.branch ?? "its branch"}`}
          </h2>
          <p className="hint">
            {entry.shared
              ? `Compared with the last commit. Agents without their own worktree share this folder (${entry.agents.join(", ") || "none configured"}), so git can't say which of them changed what.`
              : `Compared with ${entry.base}, where the branch started. ${entry.commits_ahead} commit(s) not merged yet.`}
          </p>
        </div>
        {sources.length > 1 && (
          <label className="field">
            <span>Showing</span>
            <select value={entry.agent} onChange={(e) => onSource(e.target.value)}>
              {sources.map((c) => (
                <option key={c.agent} value={c.agent}>
                  {nameOf(c)} (+{c.added} −{c.removed})
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <dl className="summary-stats">
        <div>
          <dt>Files</dt>
          <dd>{entry.files.length}</dd>
        </div>
        <div>
          <dt>Lines added</dt>
          <dd className="plus">+{entry.added}</dd>
        </div>
        <div>
          <dt>Lines removed</dt>
          <dd className="minus">−{entry.removed}</dd>
        </div>
        {review && (
          <div className="summary-kinds">
            <dt>By kind</dt>
            <dd>
              {byStatus.added > 0 && <span>{byStatus.added} new</span>}
              {byStatus.modified > 0 && <span>{byStatus.modified} modified</span>}
              {byStatus.renamed > 0 && <span>{byStatus.renamed} renamed</span>}
              {byStatus.deleted > 0 && <span>{byStatus.deleted} deleted</span>}
            </dd>
          </div>
        )}
      </dl>

      <div className="summary-areas" aria-label="Where the change landed">
        <div className="area-strip" aria-hidden="true">
          {entry.areas.slice(0, 6).map((a, i) => (
            <span
              key={a.path}
              className={`area-seg seg-${i}`}
              style={{ flexGrow: a.added + a.removed || 1 }}
              title={`${a.path}: +${a.added} −${a.removed}`}
            />
          ))}
        </div>
        <ul>
          {entry.areas.slice(0, 6).map((a, i) => (
            <li key={a.path}>
              <span className={`swatch seg-${i}`} aria-hidden="true" />
              <span className="mono">{a.path}</span>
              <span className="hint">
                {Math.round(((a.added + a.removed) / total) * 100)}% ({a.files} {a.files === 1 ? "file" : "files"})
              </span>
            </li>
          ))}
        </ul>
      </div>

      {(said.length > 0 || (review?.commits.length ?? 0) > 0) && (
        <div className="summary-narrative">
          {said.length > 0 && (
            <div>
              <h3>What the agents say they did</h3>
              {said.map(({ name, task, summary }) => (
                <div key={name} className="said">
                  <p className="run-head">
                    <strong>{name}</strong>
                    <button className="link key" onClick={() => actions.select({ kind: "task", key: task.key })}>
                      {task.key}
                    </button>
                    <span>{task.title}</span>
                  </p>
                  <p className="prose clipped">{summary || "No summary until the task reports."}</p>
                </div>
              ))}
            </div>
          )}
          {review && review.commits.length > 0 && (
            <div>
              <h3>Commits</h3>
              <ol className="commits">
                {review.commits.map((c) => (
                  <li key={c.sha}>
                    <span className="mono">{c.sha}</span> {c.subject}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      )}
    </header>
  );
}
