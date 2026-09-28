"""The scheduling core. Pure functions, so tested exhaustively and cheaply.

No database, no asyncio, no Claude. If dependency logic is ever wrong, it should
be wrong here first and caught here first.
"""

from __future__ import annotations

import pytest

from agentos.schemas.enums import TaskStatus
from agentos.services.dag import (
    DependencyCycle,
    Readiness,
    TaskNode,
    compute_readiness,
    dangling_edges,
    find_cycles,
    has_cycle,
    is_settled,
    ready_nodes,
    selectable,
    would_create_cycle,
)


def graph(*specs: tuple) -> dict[int, TaskNode]:
    """Build a node map from (id, status, deps...) tuples."""
    nodes: dict[int, TaskNode] = {}
    for spec in specs:
        task_id, status, *rest = spec
        deps = frozenset(rest[0]) if rest else frozenset()
        priority = rest[1] if len(rest) > 1 else 100
        agent = rest[2] if len(rest) > 2 else f"agent{task_id}"
        nodes[task_id] = TaskNode(
            id=task_id,
            key=f"T-{task_id}",
            status=status,
            depends_on=deps,
            priority=priority,
            agent_name=agent,
            sequence=task_id,
        )
    return nodes


def decisions_by_id(nodes) -> dict[int, TaskStatus]:
    return {d.task_id: d.status for d in compute_readiness(nodes)}


# ------------------------------------------------------------ no dependencies


def test_task_without_dependencies_becomes_ready() -> None:
    assert decisions_by_id(graph((1, TaskStatus.PENDING))) == {1: TaskStatus.READY}


def test_already_ready_task_produces_no_change() -> None:
    assert compute_readiness(graph((1, TaskStatus.READY))) == []


def test_running_task_is_never_recomputed() -> None:
    assert compute_readiness(graph((1, TaskStatus.RUNNING))) == []


@pytest.mark.parametrize(
    "status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
)
def test_terminal_tasks_are_never_recomputed(status: TaskStatus) -> None:
    assert compute_readiness(graph((1, status))) == []


# ----------------------------------------------------------- one dependency


def test_pending_dependency_keeps_task_pending() -> None:
    nodes = graph((1, TaskStatus.RUNNING), (2, TaskStatus.PENDING, [1]))
    assert 2 not in decisions_by_id(nodes)


def test_completed_dependency_makes_task_ready() -> None:
    nodes = graph((1, TaskStatus.COMPLETED), (2, TaskStatus.PENDING, [1]))
    assert decisions_by_id(nodes)[2] is TaskStatus.READY


def test_failed_dependency_blocks_task() -> None:
    nodes = graph((1, TaskStatus.FAILED), (2, TaskStatus.PENDING, [1]))
    assert decisions_by_id(nodes)[2] is TaskStatus.BLOCKED


def test_cancelled_dependency_blocks_task() -> None:
    nodes = graph((1, TaskStatus.CANCELLED), (2, TaskStatus.PENDING, [1]))
    assert decisions_by_id(nodes)[2] is TaskStatus.BLOCKED


def test_block_reason_names_the_offending_dependency() -> None:
    nodes = graph((1, TaskStatus.FAILED), (2, TaskStatus.PENDING, [1]))
    reason = next(d.reason for d in compute_readiness(nodes) if d.task_id == 2)
    assert "T-1" in reason and "failed" in reason


def test_ready_task_regresses_to_blocked_if_dependency_fails() -> None:
    """A task already marked ready must not stay ready once a dep dies."""
    nodes = graph((1, TaskStatus.FAILED), (2, TaskStatus.READY, [1]))
    assert decisions_by_id(nodes)[2] is TaskStatus.BLOCKED


def test_blocked_task_recovers_when_dependency_completes() -> None:
    """Re-running a failed dependency must unblock its dependents."""
    nodes = graph((1, TaskStatus.COMPLETED), (2, TaskStatus.BLOCKED, [1]))
    assert decisions_by_id(nodes)[2] is TaskStatus.READY


# ------------------------------------------------------- multiple dependencies


def test_task_waits_for_all_dependencies() -> None:
    nodes = graph(
        (1, TaskStatus.COMPLETED),
        (2, TaskStatus.RUNNING),
        (3, TaskStatus.PENDING, [1, 2]),
    )
    assert 3 not in decisions_by_id(nodes)


def test_task_ready_when_all_dependencies_complete() -> None:
    """The BACKEND-1 + FRONTEND-1 -> QA-1 fan-in from the spec."""
    nodes = graph(
        (1, TaskStatus.COMPLETED),
        (2, TaskStatus.COMPLETED),
        (3, TaskStatus.PENDING, [1, 2]),
    )
    assert decisions_by_id(nodes)[3] is TaskStatus.READY


def test_one_dead_dependency_blocks_despite_others_completing() -> None:
    nodes = graph(
        (1, TaskStatus.COMPLETED),
        (2, TaskStatus.FAILED),
        (3, TaskStatus.PENDING, [1, 2]),
    )
    assert decisions_by_id(nodes)[3] is TaskStatus.BLOCKED


def test_diamond_graph_resolves_in_order() -> None:
    #   1 -> 2,3 -> 4
    nodes = graph(
        (1, TaskStatus.COMPLETED),
        (2, TaskStatus.PENDING, [1]),
        (3, TaskStatus.PENDING, [1]),
        (4, TaskStatus.PENDING, [2, 3]),
    )
    result = decisions_by_id(nodes)
    assert result[2] is TaskStatus.READY
    assert result[3] is TaskStatus.READY
    assert 4 not in result  # still waiting on 2 and 3


def test_deep_chain_does_not_recurse() -> None:
    """A long dependency chain must not hit the recursion limit."""
    specs = [(1, TaskStatus.COMPLETED)]
    specs += [(i, TaskStatus.PENDING, [i - 1]) for i in range(2, 3000)]
    nodes = graph(*specs)
    assert not has_cycle(nodes)
    assert decisions_by_id(nodes)[2] is TaskStatus.READY


# ---------------------------------------------------------------- missing deps


def test_missing_dependency_blocks_rather_than_running() -> None:
    """A dangling edge must never let a task run as if unblocked."""
    nodes = graph((2, TaskStatus.PENDING, [99]))
    assert decisions_by_id(nodes)[2] is TaskStatus.BLOCKED


def test_dangling_edges_are_reported() -> None:
    nodes = graph((1, TaskStatus.PENDING), (2, TaskStatus.PENDING, [99]))
    assert dangling_edges(nodes) == [(2, 99)]


def test_no_dangling_edges_in_a_sound_graph() -> None:
    nodes = graph((1, TaskStatus.COMPLETED), (2, TaskStatus.PENDING, [1]))
    assert dangling_edges(nodes) == []


# ------------------------------------------------------------ cycle detection


def test_no_cycle_in_a_dag() -> None:
    nodes = graph(
        (1, TaskStatus.PENDING),
        (2, TaskStatus.PENDING, [1]),
        (3, TaskStatus.PENDING, [1, 2]),
    )
    assert find_cycles(nodes) == []
    assert not has_cycle(nodes)


def test_direct_two_node_cycle() -> None:
    nodes = graph((1, TaskStatus.PENDING, [2]), (2, TaskStatus.PENDING, [1]))
    assert has_cycle(nodes)


def test_three_node_cycle() -> None:
    nodes = graph(
        (1, TaskStatus.PENDING, [3]),
        (2, TaskStatus.PENDING, [1]),
        (3, TaskStatus.PENDING, [2]),
    )
    cycles = find_cycles(nodes)
    assert len(cycles) == 1
    assert set(cycles[0]) == {1, 2, 3}


def test_self_dependency_is_a_cycle() -> None:
    assert has_cycle(graph((1, TaskStatus.PENDING, [1])))


def test_cycle_found_even_with_acyclic_tasks_present() -> None:
    nodes = graph(
        (1, TaskStatus.COMPLETED),
        (2, TaskStatus.PENDING, [1]),
        (3, TaskStatus.PENDING, [4]),
        (4, TaskStatus.PENDING, [3]),
    )
    cycles = find_cycles(nodes)
    assert len(cycles) == 1
    assert set(cycles[0]) == {3, 4}


def test_two_independent_cycles_are_both_found() -> None:
    nodes = graph(
        (1, TaskStatus.PENDING, [2]),
        (2, TaskStatus.PENDING, [1]),
        (3, TaskStatus.PENDING, [4]),
        (4, TaskStatus.PENDING, [3]),
    )
    assert len(find_cycles(nodes)) == 2


def test_cycle_exception_message_lists_the_path() -> None:
    exc = DependencyCycle([[1, 2, 1]])
    assert "1 -> 2 -> 1" in str(exc)


# ------------------------------------------------------- incremental cycle check


def test_would_create_cycle_detects_self_edge() -> None:
    nodes = graph((1, TaskStatus.PENDING))
    assert would_create_cycle(nodes, 1, 1)


def test_would_create_cycle_detects_reverse_edge() -> None:
    nodes = graph((1, TaskStatus.PENDING), (2, TaskStatus.PENDING, [1]))
    # 2 already depends on 1, so making 1 depend on 2 closes the loop.
    assert would_create_cycle(nodes, 1, 2)


def test_would_create_cycle_detects_transitive_loop() -> None:
    nodes = graph(
        (1, TaskStatus.PENDING),
        (2, TaskStatus.PENDING, [1]),
        (3, TaskStatus.PENDING, [2]),
    )
    assert would_create_cycle(nodes, 1, 3)


def test_safe_edge_is_allowed() -> None:
    nodes = graph((1, TaskStatus.PENDING), (2, TaskStatus.PENDING), (3, TaskStatus.PENDING))
    assert not would_create_cycle(nodes, 3, 1)
    assert not would_create_cycle(nodes, 3, 2)


def test_diamond_edge_is_not_a_cycle() -> None:
    nodes = graph(
        (1, TaskStatus.PENDING),
        (2, TaskStatus.PENDING, [1]),
        (3, TaskStatus.PENDING, [1]),
        (4, TaskStatus.PENDING),
    )
    assert not would_create_cycle(nodes, 4, 2)
    assert not would_create_cycle(nodes, 4, 3)


def test_would_create_cycle_with_unknown_task_is_false() -> None:
    nodes = graph((1, TaskStatus.PENDING))
    assert not would_create_cycle(nodes, 1, 99)


# ----------------------------------------------------------- dispatch ordering


def test_ready_nodes_ordered_by_priority() -> None:
    nodes = graph(
        (1, TaskStatus.READY, [], 50),
        (2, TaskStatus.READY, [], 10),
        (3, TaskStatus.READY, [], 100),
    )
    assert [n.id for n in ready_nodes(nodes)] == [2, 1, 3]


def test_equal_priority_falls_back_to_sequence() -> None:
    nodes = graph(
        (3, TaskStatus.READY, [], 100),
        (1, TaskStatus.READY, [], 100),
        (2, TaskStatus.READY, [], 100),
    )
    assert [n.id for n in ready_nodes(nodes)] == [1, 2, 3]


def test_only_ready_tasks_are_dispatchable() -> None:
    nodes = graph(
        (1, TaskStatus.PENDING),
        (2, TaskStatus.READY),
        (3, TaskStatus.RUNNING),
        (4, TaskStatus.BLOCKED),
    )
    assert [n.id for n in ready_nodes(nodes)] == [2]


# --------------------------------------------------------------- selection


def test_selection_respects_capacity() -> None:
    nodes = graph(
        (1, TaskStatus.READY, [], 100, "a"),
        (2, TaskStatus.READY, [], 100, "b"),
        (3, TaskStatus.READY, [], 100, "c"),
    )
    chosen = selectable(nodes, {"a", "b", "c"}, capacity=2)
    assert [n.id for n in chosen] == [1, 2]


def test_zero_capacity_selects_nothing() -> None:
    nodes = graph((1, TaskStatus.READY, [], 100, "a"))
    assert selectable(nodes, {"a"}, capacity=0) == []


def test_one_task_per_agent_per_pass() -> None:
    """Two ready tasks for the same agent must not both start."""
    nodes = graph(
        (1, TaskStatus.READY, [], 100, "backend"),
        (2, TaskStatus.READY, [], 100, "backend"),
    )
    chosen = selectable(nodes, {"backend"}, capacity=5)
    assert [n.id for n in chosen] == [1]


def test_busy_agent_is_skipped() -> None:
    nodes = graph(
        (1, TaskStatus.READY, [], 100, "backend"),
        (2, TaskStatus.READY, [], 100, "frontend"),
    )
    chosen = selectable(nodes, {"frontend"}, capacity=5)
    assert [n.id for n in chosen] == [2]


def test_unassigned_task_is_not_dispatched() -> None:
    nodes = {
        1: TaskNode(id=1, key="T-1", status=TaskStatus.READY, agent_name=None),
    }
    assert selectable(nodes, {"backend"}, capacity=5) == []


def test_concurrent_selection_across_distinct_agents() -> None:
    """backend + frontend + docs should all start together."""
    nodes = graph(
        (1, TaskStatus.READY, [], 100, "backend"),
        (2, TaskStatus.READY, [], 100, "frontend"),
        (3, TaskStatus.READY, [], 100, "docs"),
    )
    chosen = selectable(nodes, {"backend", "frontend", "docs"}, capacity=3)
    assert {n.agent_name for n in chosen} == {"backend", "frontend", "docs"}


# --------------------------------------------------------------- settled state


def test_not_settled_while_work_remains() -> None:
    assert not is_settled(graph((1, TaskStatus.READY)))
    assert not is_settled(graph((1, TaskStatus.RUNNING)))
    assert not is_settled(graph((1, TaskStatus.PENDING)))


def test_settled_when_everything_is_terminal_or_blocked() -> None:
    assert is_settled(
        graph(
            (1, TaskStatus.COMPLETED),
            (2, TaskStatus.FAILED),
            (3, TaskStatus.BLOCKED),
            (4, TaskStatus.CANCELLED),
        )
    )


def test_empty_graph_is_settled() -> None:
    assert is_settled({})
    assert compute_readiness({}) == []
    assert find_cycles({}) == []


def test_readiness_is_deterministic() -> None:
    """Same input, same output order -- scheduling must be reproducible."""
    nodes = graph(
        (3, TaskStatus.PENDING),
        (1, TaskStatus.PENDING),
        (2, TaskStatus.PENDING),
    )
    first = compute_readiness(nodes)
    assert first == compute_readiness(nodes)
    assert [d.task_id for d in first] == [1, 2, 3]


def test_readiness_dataclass_equality() -> None:
    assert Readiness(1, TaskStatus.READY, "x") == Readiness(1, TaskStatus.READY, "x")
