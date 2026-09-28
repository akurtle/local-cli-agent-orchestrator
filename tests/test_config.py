from __future__ import annotations

from pathlib import Path

import pytest

from agentos.config import ConfigError, default_config_yaml, load_config
from agentos.paths import ProjectPaths, find_project_root


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agentos.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_default_config_round_trips(tmp_path: Path) -> None:
    path = write(tmp_path, default_config_yaml("Demo"))
    config = load_config(path)
    assert config.project.name == "Demo"
    assert config.orchestrator.max_concurrent_agents == 3
    assert config.runtime.name == "claude"
    assert set(config.agents) == {"manager", "backend", "frontend", "qa", "reviewer"}
    assert config.role_names() == set(config.agents)


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("nope") / "agentos.yaml")


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = write(tmp_path, "project: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    path = write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """Typos must fail at load time, not halfway through a run."""
    path = write(tmp_path, "orchestrator:\n  max_concurent_agents: 4\n")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(path)


def test_concurrency_bounds_enforced(tmp_path: Path) -> None:
    path = write(tmp_path, "orchestrator:\n  max_concurrent_agents: 0\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_empty_config_uses_defaults(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    config = load_config(path)
    assert config.project.name == "Untitled Project"
    assert config.agents == {}


def test_find_project_root_walks_upward(tmp_path: Path) -> None:
    write(tmp_path, default_config_yaml("Demo"))
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == tmp_path.resolve()


def test_find_project_root_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_project_root(tmp_path) is None


def test_project_paths_layout(tmp_path: Path) -> None:
    paths = ProjectPaths(root=tmp_path)
    paths.ensure()
    assert paths.state_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.db_file.parent == paths.state_dir
    paths.ensure()  # idempotent
