// Shapes of what /api/snapshot returns. Mirrors the Python views; only the
// fields the UI reads are listed.

export type TaskStatus =
  | "pending"
  | "ready"
  | "running"
  | "agent_done"
  | "verifying"
  | "failed_verification"
  | "blocked"
  | "review"
  | "completed"
  | "failed"
  | "cancelled";

export type AgentStatus =
  | "idle"
  | "working"
  | "waiting"
  | "blocked"
  | "failed"
  | "paused"
  | "offline";

export interface Task {
  id: number;
  key: string;
  title: string;
  description: string;
  status: TaskStatus;
  assigned_agent: string | null;
  acceptance_criteria: string[];
  depends_on: string[];
  result: string | null;
  error: string | null;
  attempts: number;
  needs_intervention: boolean;
  objective_id: number | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface Agent {
  id: number;
  name: string;
  role: string;
  description: string;
  status: AgentStatus;
  model: string | null;
  session_id: string | null;
  current_task_id: number | null;
  branch_name: string | null;
}

export interface Objective {
  id: number;
  description: string;
  status: string;
  task_keys: string[];
}

export interface Message {
  id: number;
  sender: string;
  recipient: string | null;
  body: string;
  status: string;
  message_type: string;
  created_at: string | null;
  task_key?: string | null;
}

export interface Run {
  id: number;
  agent: string;
  task_key: string | null;
  status: string;
  started_at: string;
  duration: string;
  text: string;
}

export interface Denial {
  tool: string;
  detail: string;
  spelled: string;
}

export type AttentionKind = "plan" | "blocked" | "command" | "verify" | "failed" | "merge";

export interface AttentionItem {
  kind: AttentionKind;
  key: string;
  title: string;
  reason: string;
  agent: string | null;
  actions: string[];
  notes: string[];
  denials: Denial[];
  holding_up: string[];
  label: string;
}

export interface FileDelta {
  path: string;
  added: number;
  removed: number;
  is_new: boolean;
  binary: boolean;
  churn: number;
}

export interface Area {
  path: string;
  files: number;
  added: number;
  removed: number;
}

export interface AgentChanges {
  agent: string;
  base: string;
  branch: string | null;
  files: FileDelta[];
  commits_ahead: number;
  agents: string[];
  added: number;
  removed: number;
  new_files: number;
  shared: boolean;
  areas: Area[];
}

export interface Provider {
  name: string;
  label: string;
  installed: boolean;
  active: boolean;
  planning: string | null;
  execution: string | null;
}

export interface Snapshot {
  project: string;
  provider: Provider | null;
  providers: Provider[];
  work: "running" | "stopping" | null;
  agents: Agent[];
  tasks: Task[];
  objectives: Objective[];
  messages: Message[];
  runs: Run[];
  attention: AttentionItem[];
  changes: AgentChanges[];
  changes_error: string | null;
  counts: {
    tasks: number;
    completed: number;
    failed: number;
    blocked: number;
    need_you: number;
  };
}

export interface Diff {
  agent: string;
  text: string;
  truncated: boolean;
  new_files: string[];
}

export interface WorkLog {
  path: string | null;
  text: string;
  truncated?: boolean;
}

// What the side drawer is showing.
export type Selection =
  | { kind: "task"; key: string }
  | { kind: "agent"; name: string }
  | { kind: "attention"; key: string }
  | { kind: "changes"; agent: string }
  | null;

// ------------------------------------------------------------ code review

export interface DiffLine {
  kind: "context" | "add" | "del";
  text: string;
  old: number | null;
  new: number | null;
}

export interface DiffHunk {
  old_start: number;
  new_start: number;
  header: string;
  lines: DiffLine[];
}

export interface FileDiff {
  path: string;
  status: "added" | "deleted" | "modified" | "renamed";
  old_path: string | null;
  binary: boolean;
  too_large: boolean;
  added: number;
  removed: number;
  hunks: DiffHunk[];
}

export interface Review {
  agent: string;
  base: string;
  branch: string | null;
  commits: { sha: string; subject: string }[];
  files: FileDiff[];
}

export type View = "board" | "code";
