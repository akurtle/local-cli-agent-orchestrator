import type { Actions } from "../App";
import type { Task } from "../types";

// The same four moves the terminal dashboard offers (u, R, b, s), with the
// same rules: nothing here touches a task an agent is running right now.

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function TaskActionButtons({ task, actions }: { task: Task; actions: Actions }) {
  const key = task.key;
  const path = (verb: string) => `/api/tasks/${encodeURIComponent(key)}/${verb}`;
  const buttons = [];

  if (task.status === "blocked" && task.needs_intervention) {
    buttons.push(
      <button
        key="unblock"
        className="button primary"
        onClick={() =>
          actions.confirm({
            title: `Unblock ${key}?`,
            body: "It goes back in the queue. Fix what blocked it first, or the agent will hit the same wall.",
            confirmLabel: "Unblock",
            path: path("unblock"),
            done: `Unblocked ${key}. Start work to run it.`,
          })
        }
      >
        Unblock
      </button>,
    );
  }

  if (task.status === "failed" || task.status === "failed_verification") {
    buttons.push(
      <button
        key="retry"
        className="button primary"
        onClick={() =>
          actions.confirm({
            title: `Retry ${key}?`,
            body:
              task.status === "failed_verification"
                ? "It goes back in the queue and has to pass its checks again. Fix what the checks found first."
                : "It goes back in the queue with a fresh attempt.",
            confirmLabel: "Retry",
            path: path("retry"),
            done: `Queued ${key} to retry. Start work to run it.`,
          })
        }
      >
        Retry
      </button>,
    );
  }

  const waiting =
    task.status === "pending" ||
    task.status === "ready" ||
    (task.status === "blocked" && !task.needs_intervention);
  if (waiting) {
    buttons.push(
      <button
        key="hold"
        className="button"
        onClick={() =>
          actions.confirm({
            title: `Block ${key}?`,
            body: "It won't be scheduled until you unblock it. Tasks that depend on it wait too.",
            confirmLabel: "Block",
            path: path("hold"),
            done: `Blocked ${key}.`,
          })
        }
      >
        Block
      </button>,
    );
  }

  if (!TERMINAL.has(task.status) && task.status !== "running") {
    buttons.push(
      <button
        key="cancel"
        className="button danger-quiet"
        onClick={() =>
          actions.confirm({
            title: `Cancel ${key}?`,
            body: "This can't be undone. Tasks that depend on it will be blocked. To pause it instead, block it.",
            confirmLabel: "Cancel task",
            danger: true,
            path: path("cancel"),
            done: `Cancelled ${key}.`,
          })
        }
      >
        Cancel task
      </button>,
    );
  }

  if (task.status === "running") {
    return <p className="hint">An agent is working on this. Stop work to change it.</p>;
  }
  return buttons.length ? <div className="button-row">{buttons}</div> : null;
}
