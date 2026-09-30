import { useMemo, useState } from "react";
import type { Actions } from "../App";
import type { Selection, Snapshot, Task, TaskStatus } from "../types";

// The dispatch board: every task is a strip, and strips move left to right
// through the lanes as the orchestrator advances them. "Stuck" sits between
// the work and the finish line, where it can't be missed.

interface Lane {
  id: string;
  title: string;
  statuses: TaskStatus[];
  empty: string;
}

const LANES: Lane[] = [
  { id: "waiting", title: "Waiting", statuses: ["pending", "ready"], empty: "Nothing queued." },
  { id: "running", title: "Running", statuses: ["running"], empty: "No agent is working." },
  {
    id: "checking",
    title: "Checking",
    statuses: ["agent_done", "verifying", "review"],
    empty: "Nothing awaiting checks.",
  },
  {
    id: "stuck",
    title: "Stuck",
    statuses: ["blocked", "failed", "failed_verification"],
    empty: "Nothing stuck.",
  },
  { id: "done", title: "Done", statuses: ["completed"], empty: "Nothing finished yet." },
];

const STATUS_WORDS: Record<TaskStatus, string> = {
  pending: "waiting on others",
  ready: "ready",
  running: "running",
  agent_done: "claims done",
  verifying: "being checked",
  failed_verification: "checks failed",
  blocked: "blocked",
  review: "in review",
  completed: "done",
  failed: "failed",
  cancelled: "cancelled",
};

export function Board({
  snapshot,
  selection,
  actions,
}: {
  snapshot: Snapshot;
  selection: Selection;
  actions: Actions;
}) {
  const [objectiveId, setObjectiveId] = useState<number | "all">("all");
  const [showCancelled, setShowCancelled] = useState(false);

  const tasks = useMemo(
    () =>
      objectiveId === "all"
        ? snapshot.tasks
        : snapshot.tasks.filter((t) => t.objective_id === objectiveId),
    [snapshot.tasks, objectiveId],
  );
  const cancelled = tasks.filter((t) => t.status === "cancelled");
  const lanes = showCancelled
    ? [...LANES, { id: "cancelled", title: "Cancelled", statuses: ["cancelled"] as TaskStatus[], empty: "" }]
    : LANES;

  if (!snapshot.tasks.length) {
    return (
      <section className="board empty-board">
        <h2>No tasks yet</h2>
        <p>
          Give the manager an objective with <code>agentctl run "what you want done"</code>. Its
          plan appears here once you approve it.
        </p>
      </section>
    );
  }

  return (
    <section className="board" aria-labelledby="board-title">
      <div className="board-head">
        <h2 id="board-title">Tasks</h2>
        {snapshot.objectives.length > 1 && (
          <label className="field">
            <span>Objective</span>
            <select
              value={objectiveId}
              onChange={(e) => setObjectiveId(e.target.value === "all" ? "all" : Number(e.target.value))}
            >
              <option value="all">All objectives</option>
              {snapshot.objectives.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.id}: {o.description.slice(0, 60)}
                </option>
              ))}
            </select>
          </label>
        )}
        {cancelled.length > 0 && (
          <button className="button quiet small" onClick={() => setShowCancelled((v) => !v)}>
            {showCancelled ? "Hide cancelled" : `Show ${cancelled.length} cancelled`}
          </button>
        )}
      </div>

      <div className="lanes" style={{ ["--lanes" as string]: lanes.length }}>
        {lanes.map((lane) => {
          const strips = tasks.filter((t) => lane.statuses.includes(t.status));
          return (
            <div key={lane.id} className={`lane lane-${lane.id}`}>
              <h3>
                {lane.title}
                <span className="lane-count">{strips.length}</span>
              </h3>
              {strips.length === 0 ? (
                <p className="lane-empty">{lane.empty}</p>
              ) : (
                <ol>
                  {strips.map((task) => (
                    <Strip
                      key={task.key}
                      task={task}
                      selected={selection?.kind === "task" && selection.key === task.key}
                      onSelect={() => actions.select({ kind: "task", key: task.key })}
                      tasks={snapshot.tasks}
                    />
                  ))}
                </ol>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function Strip({
  task,
  selected,
  onSelect,
  tasks,
}: {
  task: Task;
  selected: boolean;
  onSelect: () => void;
  tasks: Task[];
}) {
  const openDeps = task.depends_on.filter(
    (key) => tasks.find((t) => t.key === key)?.status !== "completed",
  );
  return (
    <li>
      <button
        className={`strip status-${task.status} ${task.needs_intervention ? "needs" : ""} ${selected ? "selected" : ""}`}
        aria-pressed={selected}
        onClick={onSelect}
      >
        <span className="strip-top">
          <span className="key">{task.key}</span>
          <span className="strip-agent">{task.assigned_agent ?? "unassigned"}</span>
        </span>
        <span className="strip-title">{task.title}</span>
        <span className="strip-meta">
          <span>{task.needs_intervention ? "needs you" : STATUS_WORDS[task.status]}</span>
          {openDeps.length > 0 && <span>after {openDeps.join(", ")}</span>}
          {task.attempts > 1 && <span>attempt {task.attempts}</span>}
        </span>
      </button>
    </li>
  );
}
