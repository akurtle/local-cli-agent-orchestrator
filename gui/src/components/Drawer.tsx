import { useEffect, useRef, useState } from "react";
import type { Actions } from "../App";
import type { AgentChanges, AttentionItem, Selection, Snapshot, Task } from "../types";
import { TaskActionButtons } from "./taskActions";
import { DiffView } from "./DiffView";

export function Drawer({
  snapshot,
  selection,
  actions,
  onClose,
}: {
  snapshot: Snapshot;
  selection: NonNullable<Selection>;
  actions: Actions;
  onClose: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const identity = JSON.stringify(selection);
  useEffect(() => {
    heading.current?.focus();
  }, [identity]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector("dialog[open]")) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  let body: React.ReactNode;
  let title = "";
  if (selection.kind === "task") {
    const task = snapshot.tasks.find((t) => t.key === selection.key);
    title = task ? task.key : "Task not found";
    body = task ? <TaskDetail task={task} snapshot={snapshot} actions={actions} /> : <Gone />;
  } else if (selection.kind === "agent") {
    const agent = snapshot.agents.find((a) => a.name === selection.name);
    title = selection.name;
    body = agent ? <AgentDetail name={agent.name} snapshot={snapshot} actions={actions} /> : <Gone />;
  } else if (selection.kind === "attention") {
    const item = snapshot.attention.find((i) => i.key === selection.key);
    title = item ? item.label.charAt(0).toUpperCase() + item.label.slice(1) : "Resolved";
    body = item ? (
      <AttentionDetail item={item} snapshot={snapshot} actions={actions} />
    ) : (
      <p className="hint">This no longer needs you.</p>
    );
  } else {
    const entry = snapshot.changes.find((c) => c.agent === selection.agent);
    title = selection.agent === "(shared)" ? "Project folder" : selection.agent;
    body = entry ? <ChangesDetail entry={entry} snapshot={snapshot} actions={actions} /> : <Gone />;
  }

  return (
    <aside className="drawer" aria-labelledby="drawer-title">
      <div className="drawer-head">
        <h2
          id="drawer-title"
          ref={heading}
          tabIndex={-1}
          className={selection.kind === "task" ? "key" : undefined}
        >
          {title}
        </h2>
        <button className="button quiet small" onClick={onClose} aria-label="Close details">
          Close
        </button>
      </div>
      <div className="drawer-body">{body}</div>
    </aside>
  );
}

function Gone() {
  return <p className="hint">This is no longer in the project.</p>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="field-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function TaskLink({ taskKey, actions }: { taskKey: string; actions: Actions }) {
  return (
    <button className="link key" onClick={() => actions.select({ kind: "task", key: taskKey })}>
      {taskKey}
    </button>
  );
}

// ------------------------------------------------------------------ task

function TaskDetail({ task, snapshot, actions }: { task: Task; snapshot: Snapshot; actions: Actions }) {
  const dependents = snapshot.tasks.filter((t) => t.depends_on.includes(task.key));
  return (
    <>
      <p className="detail-title">{task.title}</p>
      <dl className="fields">
        <Field label="Status">
          <span className={`pill status-${task.status}`}>{task.status.replace("_", " ")}</span>
          {task.needs_intervention && <span className="pill needs">needs you</span>}
        </Field>
        <Field label="Agent">
          {task.assigned_agent ? (
            <button className="link" onClick={() => actions.select({ kind: "agent", name: task.assigned_agent! })}>
              {task.assigned_agent}
            </button>
          ) : (
            "unassigned"
          )}
        </Field>
        {task.depends_on.length > 0 && (
          <Field label="After">
            {task.depends_on.map((k) => (
              <TaskLink key={k} taskKey={k} actions={actions} />
            ))}
          </Field>
        )}
        {dependents.length > 0 && (
          <Field label="Before">
            {dependents.map((t) => (
              <TaskLink key={t.key} taskKey={t.key} actions={actions} />
            ))}
          </Field>
        )}
        {task.attempts > 0 && <Field label="Attempts">{task.attempts}</Field>}
      </dl>

      <TaskActionButtons task={task} actions={actions} />

      {task.error && (
        <section className="block error-block">
          <h3>Why it stopped</h3>
          <p className="prose">{task.error}</p>
        </section>
      )}
      {task.acceptance_criteria.length > 0 && (
        <section className="block">
          <h3>Done when</h3>
          <ul className="criteria">
            {task.acceptance_criteria.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </section>
      )}
      {task.description.trim() && (
        <section className="block">
          <h3>Brief</h3>
          <p className="prose">{task.description.trim()}</p>
        </section>
      )}
      {task.result && (
        <section className="block">
          <h3>What the agent reported</h3>
          <p className="prose">{task.result.trim()}</p>
        </section>
      )}
    </>
  );
}

// ----------------------------------------------------------------- agent

function AgentDetail({ name, snapshot, actions }: { name: string; snapshot: Snapshot; actions: Actions }) {
  const agent = snapshot.agents.find((a) => a.name === name)!;
  const runs = snapshot.runs.filter((r) => r.agent === name).slice(0, 5);
  const current = snapshot.tasks.find((t) => t.id === agent.current_task_id);
  const path = (verb: string) => `/api/agents/${encodeURIComponent(name)}/${verb}`;
  return (
    <>
      <p className="detail-title">
        <span className={`lamp lamp-${agent.status}`} aria-hidden="true" /> {agent.role}, {agent.status}
      </p>
      {agent.description && <p className="hint">{agent.description}</p>}
      <dl className="fields">
        {current && (
          <Field label="Working on">
            <TaskLink taskKey={current.key} actions={actions} />
          </Field>
        )}
        <Field label="Model">{agent.model ?? "CLI default"}</Field>
        <Field label="Session">
          <span className="mono">{agent.session_id ? agent.session_id.slice(0, 8) : "none yet"}</span>
        </Field>
        {agent.branch_name && (
          <Field label="Branch">
            <span className="mono">{agent.branch_name}</span>
          </Field>
        )}
      </dl>
      <div className="button-row">
        {agent.status === "paused" ? (
          <button className="button primary" onClick={() => actions.run(path("resume"), `Resumed ${name}.`)}>
            Resume
          </button>
        ) : (
          <button
            className="button"
            disabled={agent.status === "working"}
            title={agent.status === "working" ? "Wait until it finishes its task" : undefined}
            onClick={() =>
              actions.confirm({
                title: `Pause ${name}?`,
                body: "It takes no new tasks until you resume it. Tasks assigned to it wait.",
                confirmLabel: "Pause",
                path: path("pause"),
                done: `Paused ${name}.`,
              })
            }
          >
            Pause
          </button>
        )}
      </div>
      <section className="block">
        <h3>Recent runs</h3>
        {runs.length === 0 ? (
          <p className="hint">No runs yet.</p>
        ) : (
          <ol className="runs">
            {runs.map((run) => (
              <li key={run.id}>
                <p className="run-head">
                  {run.task_key ? <TaskLink taskKey={run.task_key} actions={actions} /> : "no task"}
                  <span>
                    {run.status}, {run.duration}
                  </span>
                </p>
                {run.text && <p className="prose clipped">{run.text}</p>}
              </li>
            ))}
          </ol>
        )}
      </section>
    </>
  );
}

// ----------------------------------------------------------- needs you

function AttentionDetail({
  item,
  snapshot,
  actions,
}: {
  item: AttentionItem;
  snapshot: Snapshot;
  actions: Actions;
}) {
  const taskKey = item.key.startsWith("task:") ? item.key.slice(5) : null;
  const task = taskKey ? snapshot.tasks.find((t) => t.key === taskKey) : undefined;
  const [copied, setCopied] = useState<string | null>(null);

  return (
    <>
      <p className="detail-title">{item.title}</p>
      {item.agent && <p className="hint">Assigned to {item.agent}</p>}
      {task && <TaskActionButtons task={task} actions={actions} />}

      <section className="block">
        <h3>Why</h3>
        <p className="prose">{item.reason}</p>
      </section>

      {item.denials.length > 0 && (
        <section className="block">
          <h3>Refused in the agent's session</h3>
          <ul className="denials">
            {item.denials.map((d, i) => (
              <li key={i}>
                <span className="tool">{d.tool}</span> <code>{d.detail}</code>
              </li>
            ))}
          </ul>
        </section>
      )}

      {item.holding_up.length > 0 && (
        <section className="block">
          <h3>Holding up</h3>
          <p className="chips">
            {item.holding_up.map((k) => (
              <TaskLink key={k} taskKey={k} actions={actions} />
            ))}
          </p>
          <p className="hint">These continue once this is resolved.</p>
        </section>
      )}

      {item.notes.length > 0 && (
        <section className="block">
          {item.notes.map((n, i) => (
            <p key={i} className="hint">
              {n}
            </p>
          ))}
        </section>
      )}

      <section className="block">
        <h3>From a terminal</h3>
        <ul className="commands">
          {item.actions.map((command) => (
            <li key={command}>
              <code>{command}</code>
              <button
                className="button quiet small"
                onClick={() => {
                  navigator.clipboard?.writeText(command);
                  setCopied(command);
                }}
              >
                {copied === command ? "Copied" : "Copy"}
              </button>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}

// -------------------------------------------------------------- changes

function ChangesDetail({ entry, snapshot, actions }: { entry: AgentChanges; snapshot: Snapshot; actions: Actions }) {
  const [showDiff, setShowDiff] = useState(false);
  const widest = Math.max(1, ...entry.areas.map((a) => a.added + a.removed));
  const biggest = [...entry.files].sort((a, b) => b.churn - a.churn).slice(0, 10);
  const who = entry.agents.length ? entry.agents : [entry.agent];
  const summaries = who
    .map((name) => {
      const current = snapshot.agents.find((a) => a.name === name);
      const mine = snapshot.tasks
        .filter((t) => t.assigned_agent === name && t.started_at)
        .sort((a, b) => (a.started_at! < b.started_at! ? 1 : -1));
      const task = snapshot.tasks.find((t) => t.id === current?.current_task_id) ?? mine[0];
      return task ? { name, task } : null;
    })
    .filter(Boolean) as { name: string; task: Task }[];

  return (
    <>
      <p className="detail-title">
        <span className="plus">+{entry.added}</span> <span className="minus">−{entry.removed}</span> in{" "}
        {entry.files.length} files{entry.new_files ? `, ${entry.new_files} new` : ""}
      </p>
      <p className="hint">
        {entry.shared
          ? "Uncommitted work in the project folder. Agents without their own worktree all edit here, so this can't be split by agent."
          : `${entry.branch ?? entry.agent} against ${entry.base}, ${entry.commits_ahead} commit(s) ahead.`}
      </p>

      <section className="block">
        <h3>Where</h3>
        <ul className="areas">
          {entry.areas.slice(0, 8).map((area) => (
            <li key={area.path}>
              <span className="mono">{area.path}</span>
              <span className="bar" aria-hidden="true">
                <span className="bar-add" style={{ width: `${(area.added / widest) * 100}%` }} />
                <span className="bar-remove" style={{ width: `${(area.removed / widest) * 100}%` }} />
              </span>
              <span className="area-count">
                {area.files} files, +{area.added} −{area.removed}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section className="block">
        <h3>Largest changes</h3>
        <ul className="files">
          {biggest.map((f) => (
            <li key={f.path}>
              <span className="mono">{f.path}</span>
              <span>
                {f.is_new && <span className="pill new">new</span>}{" "}
                {f.binary ? "binary" : `+${f.added} −${f.removed}`}
              </span>
            </li>
          ))}
        </ul>
      </section>

      {summaries.length > 0 && (
        <section className="block">
          <h3>What the agents say they did</h3>
          {summaries.map(({ name, task }) => (
            <div key={name} className="summary">
              <p className="run-head">
                <strong>{name}</strong> <TaskLink taskKey={task.key} actions={actions} />
              </p>
              <p className="prose clipped">
                {(task.result ?? "").split(/\n\n(?:Branch:|No file changes detected\.)/)[0].trim() ||
                  "No summary until the task reports."}
              </p>
            </div>
          ))}
        </section>
      )}

      <section className="block">
        {showDiff ? (
          <DiffView agent={entry.agent} />
        ) : (
          <button className="button" onClick={() => setShowDiff(true)}>
            Show the full diff
          </button>
        )}
      </section>
    </>
  );
}
