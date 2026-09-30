import { useEffect, useState } from "react";
import { api } from "../api";
import type { Diff } from "../types";

// A plain unified diff, coloured by line. Fetched on demand: diffs can be large
// and most of the time the summary is enough.

export function DiffView({ agent }: { agent: string }) {
  const [diff, setDiff] = useState<Diff | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .get<Diff>(`/api/diff/${encodeURIComponent(agent)}`)
      .then((d) => live && setDiff(d))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [agent]);

  if (error) return <p className="hint">Couldn't load the diff: {error}</p>;
  if (!diff) return <p className="hint">Loading the diff…</p>;

  const lines = diff.text.split("\n");
  return (
    <div className="diff">
      {diff.new_files.length > 0 && (
        <p className="hint">
          New files not shown below (git has not tracked them yet): {diff.new_files.join(", ")}
        </p>
      )}
      {diff.text.trim() ? (
        <pre>
          {lines.map((line, i) => (
            <span key={i} className={lineClass(line)}>
              {line}
              {"\n"}
            </span>
          ))}
        </pre>
      ) : (
        <p className="hint">No changes to tracked files.</p>
      )}
      {diff.truncated && <p className="hint">The diff is long and was cut short here.</p>}
    </div>
  );
}

function lineClass(line: string): string {
  if (line.startsWith("diff --git")) return "d-file";
  if (line.startsWith("@@")) return "d-hunk";
  if (line.startsWith("+++") || line.startsWith("---")) return "d-meta";
  if (line.startsWith("+")) return "d-add";
  if (line.startsWith("-")) return "d-del";
  return "";
}
