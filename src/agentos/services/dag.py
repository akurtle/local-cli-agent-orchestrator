"""Dependency graph logic.

Deliberately pure: plain data in, plain data out. No database, no asyncio, no
Claude. Scheduling decisions are the part of this system that must be exactly
right, so they are expressed as functions that can be tested exhaustively
without any infrastructure.

The scheduler in `services/scheduler.py` loads rows, calls these functions, and
persists what they decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentos.schemas.enums import TaskStatus

# A dependency in one of these states can never be satisfied as things stand, so
# anything waiting on it is blocked rather than merely pending.
#
# BLOCKED is included so blockage propagates down a chain: if C waits on B and B
# is blocked by a failed A, then C can never run either, and calling it "pending"
# would make the work look like it is still in flight. Readiness is recomputed
# from scratch every pass, so this reverses automatically once the chain clears.
DEAD_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED}
)

# Statuses whose readiness the scheduler may recompute. Running and terminal
# tasks are left alone.
RECOMPUTABLE = frozenset({TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED})


@dataclass(frozen=True)
class TaskNode:
    """The minimum a scheduling decision needs to know about a task."""

    id: int
    key: str
    status: TaskStatus
    depends_on: frozenset[int] = field(default_factory=frozenset)
    priority: int = 100
    agent_name: str | None = None
    sequence: int = 0
    """Tie-breaker for deterministic ordering, normally the row id."""


@dataclass(frozen=True)
class Readiness:
    """What a task's status should become, and why."""

    task_id: int
    status: TaskStatus
    reason: str = ""


class DependencyCycle(ValueError):
    """The dependency graph contains a cycle, so some tasks can never run."""

    def __init__(self, cycles: list[list[int]]) -> None:
        self.cycles = cycles
        rendered = "; ".join(" -> ".join(str(i) for i in cycle) for cycle in cycles)
        super().__init__(f"dependency cycle detected: {rendered}")


def find_cycles(nodes: dict[int, TaskNode]) -> list[list[int]]:
    """Return every dependency cycle, each as a list of task ids.

    Iterative depth-first search with an explicit stack: a deep dependency chain
    must not blow the Python recursion limit.
    """
    cycles: list[list[int]] = []
    seen_cycles: set[frozenset[int]] = set()
    # 0 = unvisited, 1 = on the current path, 2 = fully explored
    state: dict[int, int] = {task_id: 0 for task_id in nodes}

    for root in sorted(nodes):
        if state[root] != 0:
            continue
        path: list[int] = []
        # Each stack frame is (node, iterator over its unvisited dependencies).
        stack: list[tuple[int, list[int]]] = [
            (root, sorted(nodes[root].depends_on))
        ]
        state[root] = 1
        path.append(root)

        while stack:
            node, pending = stack[-1]
            if not pending:
                state[node] = 2
                path.pop()
                stack.pop()
                continue
            nxt = pending.pop()
            if nxt not in nodes:
                continue  # dangling edge; validated separately
            if state[nxt] == 1:
                # Found a back edge: the cycle is the tail of the current path.
                start = path.index(nxt)
                cycle = path[start:] + [nxt]
                signature = frozenset(cycle)
                if signature not in seen_cycles:
                    seen_cycles.add(signature)
                    cycles.append(cycle)
                continue
            if state[nxt] == 2:
                continue
            state[nxt] = 1
            path.append(nxt)
            stack.append((nxt, sorted(nodes[nxt].depends_on)))

    return cycles


def has_cycle(nodes: dict[int, TaskNode]) -> bool:
    return bool(find_cycles(nodes))


def would_create_cycle(
    nodes: dict[int, TaskNode], task_id: int, depends_on_id: int
) -> bool:
    """True if adding `task_id -> depends_on_id` would create a cycle.

    Checked before persisting an edge, so a cycle never reaches the database.
    """
    if task_id == depends_on_id:
        return True
    if depends_on_id not in nodes or task_id not in nodes:
        return False
    # A cycle appears exactly when task_id is already reachable from depends_on_id.
    stack = [depends_on_id]
    visited: set[int] = set()
    while stack:
        current = stack.pop()
        if current == task_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        node = nodes.get(current)
        if node is not None:
            stack.extend(node.depends_on)
    return False


def dangling_edges(nodes: dict[int, TaskNode]) -> list[tuple[int, int]]:
    """Dependency edges pointing at tasks that do not exist."""
    missing: list[tuple[int, int]] = []
    for node in nodes.values():
        for dep in sorted(node.depends_on):
            if dep not in nodes:
                missing.append((node.id, dep))
    return missing


def compute_readiness(nodes: dict[int, TaskNode]) -> list[Readiness]:
    """Decide the correct status for every recomputable task.

    Rules, in order:
      * a dependency that failed or was cancelled blocks the task permanently
      * all dependencies completed -> ready
      * otherwise -> pending (dependencies still in flight)

    Only tasks whose status should actually change are returned.
    """
    decisions: list[Readiness] = []
    for task_id in sorted(nodes):
        node = nodes[task_id]
        if node.status not in RECOMPUTABLE:
            continue

        dead: list[str] = []
        waiting: list[str] = []
        for dep_id in sorted(node.depends_on):
            dep = nodes.get(dep_id)
            if dep is None:
                # A missing dependency cannot complete, so treat it as blocking
                # rather than silently letting the task run.
                dead.append(f"missing task {dep_id}")
                continue
            if dep.status in DEAD_STATUSES:
                dead.append(f"{dep.key} is {dep.status.value}")
            elif dep.status is not TaskStatus.COMPLETED:
                waiting.append(dep.key)

        if dead:
            target, reason = TaskStatus.BLOCKED, "blocked by " + ", ".join(dead)
        elif waiting:
            target, reason = TaskStatus.PENDING, "waiting on " + ", ".join(waiting)
        else:
            target, reason = TaskStatus.READY, "all dependencies complete"

        if target is not node.status:
            decisions.append(Readiness(task_id=task_id, status=target, reason=reason))

    return decisions


def ready_nodes(nodes: dict[int, TaskNode]) -> list[TaskNode]:
    """Tasks currently eligible to run, in dispatch order.

    Ordered by priority (lower first), then sequence, then key, so dispatch is
    deterministic and reproducible across runs.
    """
    ready = [n for n in nodes.values() if n.status is TaskStatus.READY]
    return sorted(ready, key=lambda n: (n.priority, n.sequence, n.key))


def selectable(
    nodes: dict[int, TaskNode],
    available_agents: set[str],
    capacity: int,
) -> list[TaskNode]:
    """Pick which ready tasks may start now.

    Enforces three independent limits:
      * only tasks whose assigned agent is available
      * one task per agent per pass
      * the global concurrency budget
    """
    if capacity <= 0:
        return []
    chosen: list[TaskNode] = []
    claimed: set[str] = set()
    for node in ready_nodes(nodes):
        if len(chosen) >= capacity:
            break
        agent = node.agent_name
        if agent is None or agent not in available_agents or agent in claimed:
            continue
        claimed.add(agent)
        chosen.append(node)
    return chosen


def is_settled(nodes: dict[int, TaskNode]) -> bool:
    """True when no task can make further progress without intervention.

    Used to decide when the scheduler should stop rather than spin.
    """
    return not any(
        node.status in {TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.PENDING}
        for node in nodes.values()
    )
