"""Phase 11: approval gates.

The rule worth testing hardest: a required gate with nobody to ask must refuse,
not assume. A gate that passes because no terminal was attached is not a gate.
"""

from __future__ import annotations

import pytest

from agentos.config import ApprovalsSection, Config, load_config
from agentos.services.approvals import (
    ApprovalRequired,
    ApprovalService,
    Gate,
)


def service(**overrides) -> ApprovalService:
    return ApprovalService(ApprovalsSection.model_validate(overrides))


# ------------------------------------------------------------------- defaults


def test_repository_changing_gates_are_on_by_default() -> None:
    approvals = service()
    assert approvals.is_required(Gate.MANAGER_PLAN)
    assert approvals.is_required(Gate.MERGE)


def test_final_completion_is_off_by_default() -> None:
    """Completion is computed from task state; a prompt there is just noise."""
    assert not service().is_required(Gate.FINAL_COMPLETION)


def test_required_gates_listing() -> None:
    gates = service(merge=False).required_gates()
    assert Gate.MANAGER_PLAN in gates
    assert Gate.MERGE not in gates


def test_describe() -> None:
    assert service().describe(Gate.MERGE) == "required"
    assert service(merge=False).describe(Gate.MERGE) == "not required"


# ------------------------------------------------------------------ decisions


def test_disabled_gate_allows_without_asking() -> None:
    decision = service(merge=False).evaluate(Gate.MERGE, interactive=False)
    assert decision.allowed
    assert not decision.required
    assert decision.reason == "no gate configured"


def test_explicit_yes_allows() -> None:
    decision = service().evaluate(Gate.MERGE, approved=True)
    assert decision.allowed
    assert decision.required
    assert decision.reason == "approved"


def test_explicit_no_refuses() -> None:
    decision = service().evaluate(Gate.MERGE, approved=False)
    assert not decision.allowed
    assert decision.reason == "declined"


def test_unanswered_gate_is_not_allowed_yet() -> None:
    """Interactive callers must go on to prompt."""
    decision = service().evaluate(Gate.MERGE, approved=None, interactive=True)
    assert not decision.allowed
    assert decision.reason == "not yet answered"


def test_non_interactive_required_gate_refuses() -> None:
    """The central safety property."""
    with pytest.raises(ApprovalRequired) as excinfo:
        service().evaluate(Gate.MERGE, approved=None, interactive=False)
    assert excinfo.value.gate is Gate.MERGE


def test_refusal_explains_both_escape_hatches() -> None:
    with pytest.raises(ApprovalRequired) as excinfo:
        service().evaluate(Gate.MANAGER_PLAN, interactive=False)
    message = str(excinfo.value)
    assert "--auto-approve" in message
    assert "approvals.manager_plan" in message


def test_refusal_names_the_right_flag_per_gate() -> None:
    """Telling someone to pass a flag that command does not have is useless."""
    with pytest.raises(ApprovalRequired) as excinfo:
        service().evaluate(Gate.MERGE, interactive=False)
    assert "--yes" in str(excinfo.value)


@pytest.mark.parametrize("gate", list(Gate))
def test_every_gate_has_a_documented_flag(gate: Gate) -> None:
    assert gate in ApprovalRequired.FLAGS


def test_non_interactive_disabled_gate_is_fine() -> None:
    """Automation is possible: turn the gate off deliberately."""
    decision = service(manager_plan=False).evaluate(
        Gate.MANAGER_PLAN, interactive=False
    )
    assert decision.allowed


def test_auto_approve_works_non_interactively() -> None:
    decision = service().evaluate(Gate.MERGE, approved=True, interactive=False)
    assert decision.allowed


@pytest.mark.parametrize("gate", list(Gate))
def test_every_gate_is_configurable(gate: Gate) -> None:
    """A gate the config cannot express would be unusable."""
    assert hasattr(ApprovalsSection(), gate.value)


@pytest.mark.parametrize("gate", list(Gate))
def test_every_gate_can_be_disabled(gate: Gate) -> None:
    approvals = service(**{gate.value: False})
    assert not approvals.is_required(gate)
    assert approvals.evaluate(gate, interactive=False).allowed


# --------------------------------------------------------------------- config


def test_config_defaults() -> None:
    config = Config.model_validate({})
    assert config.approvals.manager_plan is True
    assert config.approvals.merge is True
    assert config.approvals.final_completion is False


def test_config_overrides(tmp_path) -> None:
    path = tmp_path / "agentos.yaml"
    path.write_text(
        "approvals:\n  manager_plan: false\n  merge: true\n", encoding="utf-8"
    )
    config = load_config(path)
    assert config.approvals.manager_plan is False
    assert config.approvals.merge is True


def test_unknown_approval_key_is_rejected(tmp_path) -> None:
    """A typo must not silently leave a gate on or off."""
    from agentos.config import ConfigError

    path = tmp_path / "agentos.yaml"
    path.write_text("approvals:\n  manger_plan: false\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_starter_config_documents_approvals(tmp_path) -> None:
    from agentos.config import default_config_yaml

    path = tmp_path / "agentos.yaml"
    path.write_text(default_config_yaml("Demo"), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert "approvals:" in text
    # And it still loads.
    assert load_config(path).approvals.merge is True
