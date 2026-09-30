import type { AgentChanges, Snapshot, Task } from "../types";

// What the agents say they did for a change source: each candidate agent's
// current task, or else the last one it started, with its reported summary.

export interface AgentSummary {
  name: string;
  task: Task;
  summary: string;
}

// The scheduler appends git and verification reports after the agent's words.
const APPENDED = /\n\n(?:Branch:|No file changes detected\.|Verification)/;

export function summaryText(result: string | null): string {
  return (result ?? "").split(APPENDED)[0].trim();
}

export function agentSummaries(entry: AgentChanges, snapshot: Snapshot): AgentSummary[] {
  const who = entry.agents.length ? entry.agents : [entry.agent];
  const found: AgentSummary[] = [];
  for (const name of who) {
    const agent = snapshot.agents.find((a) => a.name === name);
    const started = snapshot.tasks
      .filter((t) => t.assigned_agent === name && t.started_at)
      .sort((a, b) => (a.started_at! < b.started_at! ? 1 : -1));
    const task = snapshot.tasks.find((t) => t.id === agent?.current_task_id) ?? started[0];
    if (task) found.push({ name, task, summary: summaryText(task.result) });
  }
  return found;
}
