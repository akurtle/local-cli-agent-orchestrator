import type { Actions } from "../App";
import type { Selection, Snapshot } from "../types";

// The team, left to right, each with a status lamp. A lamp that is lit means
// the agent is doing something; amber or red means it wants attention.

export function AgentRow({
  snapshot,
  selection,
  actions,
}: {
  snapshot: Snapshot;
  selection: Selection;
  actions: Actions;
}) {
  const taskKey = (id: number | null) =>
    id == null ? null : snapshot.tasks.find((t) => t.id === id)?.key ?? `#${id}`;

  return (
    <section className="agents" aria-label="Agents">
      <ul>
        {snapshot.agents.map((agent) => {
          const selected = selection?.kind === "agent" && selection.name === agent.name;
          const current = taskKey(agent.current_task_id);
          return (
            <li key={agent.name}>
              <button
                className={`agent ${selected ? "selected" : ""}`}
                aria-pressed={selected}
                onClick={() => actions.select({ kind: "agent", name: agent.name })}
              >
                <span className={`lamp lamp-${agent.status}`} aria-hidden="true" />
                <span className="agent-name">{agent.name}</span>
                <span className="agent-state">
                  {agent.status === "working" && current ? `On ${current}` : agent.status}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
