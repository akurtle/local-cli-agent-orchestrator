import { useState } from "react";
import type { Actions } from "../App";
import type { Snapshot, View } from "../types";

export function TopBar({
  snapshot,
  actions,
  view,
  onView,
}: {
  snapshot: Snapshot;
  actions: Actions;
  view: View;
  onView: (v: View) => void;
}) {
  const [choosing, setChoosing] = useState(false);
  const { counts, provider, work } = snapshot;
  const objective = [...snapshot.objectives].reverse().find((o) => !["completed", "failed", "cancelled"].includes(o.status))
    ?? snapshot.objectives[snapshot.objectives.length - 1];

  const workButton =
    work === "running" ? (
      <button
        className="button"
        onClick={() =>
          actions.confirm({
            title: "Stop work?",
            body: "Nothing new starts. Agents already running finish their current task, then work stops.",
            confirmLabel: "Stop work",
            path: "/api/work/stop",
            done: "Stopping. Running agents are finishing their tasks.",
          })
        }
      >
        Stop work
      </button>
    ) : work === "stopping" ? (
      <button className="button" disabled>
        Stopping…
      </button>
    ) : (
      <button
        className="button primary"
        onClick={() =>
          actions.confirm({
            title: `Start work${provider ? ` on ${provider.label}` : ""}?`,
            body: "Every ready task runs, the same as agentctl work in a terminal. This uses paid model usage. It keeps running if you close this page.",
            confirmLabel: "Start work",
            path: "/api/work/start",
            done: "Work started. Follow it in the work log.",
          })
        }
      >
        Start work
      </button>
    );

  return (
    <header className="topbar">
      <div className="identity">
        <h1>{snapshot.project}</h1>
        {objective && (
          <p className="objective" title={objective.description}>
            Objective {objective.id}: {objective.description}
            <span className={`pill status-${objective.status}`}>{objective.status.replace("_", " ")}</span>
          </p>
        )}
      </div>

      <nav className="views" aria-label="Views">
        <button aria-current={view === "board" ? "page" : undefined} onClick={() => onView("board")}>
          Board
        </button>
        <button aria-current={view === "code" ? "page" : undefined} onClick={() => onView("code")}>
          Code changes
          {snapshot.changes.length > 0 && (
            <span className="view-count">
              {snapshot.changes.reduce((n, c) => n + c.files.length, 0)}
            </span>
          )}
        </button>
      </nav>

      <dl className="tally">
        <div>
          <dt>Done</dt>
          <dd>
            {counts.completed}
            <span className="of">/{counts.tasks}</span>
          </dd>
        </div>
        <div className={counts.need_you ? "alert" : ""}>
          <dt>Need you</dt>
          <dd>{counts.need_you}</dd>
        </div>
      </dl>

      <div className="controls">
        <div className="provider">
          <button
            className="button quiet"
            aria-expanded={choosing}
            onClick={() => setChoosing((v) => !v)}
            title="Choose which provider the next run uses"
          >
            {provider ? provider.label : "Provider"}
            {provider && (
              <span className="models">
                {provider.planning ?? "default"} plans, {provider.execution ?? "default"} works
              </span>
            )}
          </button>
          {choosing && (
            <ul className="provider-menu" role="menu">
              {snapshot.providers.map((p) => (
                <li key={p.name} role="none">
                  <button
                    role="menuitemradio"
                    aria-checked={p.active}
                    disabled={!p.installed || p.active}
                    onClick={() => {
                      setChoosing(false);
                      actions.run("/api/provider", `Switched to ${p.label}. Applies to the next run.`, { name: p.name });
                    }}
                  >
                    <strong>{p.label}</strong>
                    <span>
                      {p.installed
                        ? `${p.planning ?? "default"} for planning, ${p.execution ?? "default"} for the work`
                        : "Not installed"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <span className={`work-state ${work ?? "idle"}`} role="status">
          {work === "running" ? "Working" : work === "stopping" ? "Stopping" : "Idle"}
        </span>
        {workButton}
      </div>
    </header>
  );
}
