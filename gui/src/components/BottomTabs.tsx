import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Actions } from "../App";
import type { Selection, Snapshot, WorkLog } from "../types";

type Tab = "changes" | "messages" | "log";

export function BottomTabs({
  snapshot,
  selection,
  actions,
}: {
  snapshot: Snapshot;
  selection: Selection;
  actions: Actions;
}) {
  const [tab, setTab] = useState<Tab>("changes");
  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "changes", label: "Changes", count: snapshot.changes.length },
    { id: "messages", label: "Messages", count: snapshot.messages.length },
    { id: "log", label: "Work log" },
  ];

  return (
    <section className="tabs">
      <div role="tablist" aria-label="Activity">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            className="tab"
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.count ? <span className="tab-count">{t.count}</span> : null}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="tab-panel">
        {tab === "changes" && <ChangesList snapshot={snapshot} selection={selection} actions={actions} />}
        {tab === "messages" && <Messages snapshot={snapshot} />}
        {tab === "log" && <Log running={snapshot.work !== null} />}
      </div>
    </section>
  );
}

function ChangesList({ snapshot, selection, actions }: { snapshot: Snapshot; selection: Selection; actions: Actions }) {
  if (snapshot.changes_error) return <p className="hint">Couldn't read git: {snapshot.changes_error}</p>;
  if (!snapshot.changes.length) return <p className="hint">No uncommitted or unmerged changes.</p>;
  return (
    <table className="changes-table">
      <thead>
        <tr>
          <th scope="col">Where</th>
          <th scope="col">Lines</th>
          <th scope="col">Files</th>
          <th scope="col">Mostly in</th>
          <th scope="col">
            <span className="visually-hidden">Review</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {snapshot.changes.map((c) => {
          const selected = selection?.kind === "changes" && selection.agent === c.agent;
          return (
            <tr key={c.agent} className={selected ? "selected" : ""}>
              <td>
                <button className="link" onClick={() => actions.select({ kind: "changes", agent: c.agent })}>
                  {c.shared ? "Project folder" : c.agent}
                </button>
              </td>
              <td>
                <span className="plus">+{c.added}</span> <span className="minus">−{c.removed}</span>
              </td>
              <td>{c.files.length}</td>
              <td className="mono">{c.areas.slice(0, 2).map((a) => a.path).join(", ")}</td>
              <td>
                <button className="button quiet small" onClick={() => actions.openReview(c.agent)}>
                  Review the code
                </button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Messages({ snapshot }: { snapshot: Snapshot }) {
  if (!snapshot.messages.length) return <p className="hint">Agents haven't messaged each other yet.</p>;
  return (
    <ol className="messages">
      {[...snapshot.messages].reverse().map((m) => (
        <li key={m.id}>
          <p className="run-head">
            <strong>{m.sender}</strong> to <strong>{m.recipient ?? "everyone"}</strong>
            <span>{m.status}</span>
          </p>
          <p className="prose clipped">{m.body}</p>
        </li>
      ))}
    </ol>
  );
}

function Log({ running }: { running: boolean }) {
  const [log, setLog] = useState<WorkLog | null>(null);
  const bottom = useRef<HTMLPreElement>(null);

  useEffect(() => {
    let live = true;
    const load = () =>
      api
        .get<WorkLog>("/api/work/log")
        .then((l) => live && setLog(l))
        .catch(() => {});
    load();
    // Follow the log only while work runs; a finished log doesn't change.
    const timer = running ? window.setInterval(load, 2000) : undefined;
    return () => {
      live = false;
      if (timer) window.clearInterval(timer);
    };
  }, [running]);

  useEffect(() => {
    const el = bottom.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log?.text]);

  if (!log) return <p className="hint">Loading…</p>;
  if (!log.path) return <p className="hint">No work has been started from here yet. Start work to see its output.</p>;
  return (
    <>
      <p className="hint mono">{log.path}</p>
      <pre ref={bottom} className="log">
        {log.text}
      </pre>
    </>
  );
}
