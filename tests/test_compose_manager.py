from pathlib import Path

import pytest
from ruamel.yaml import YAML

from cloudhelm_agent.compose_manager import ComposeManager


def _labels(path: Path) -> dict[str, str]:
    return {
        "com.docker.compose.project": "inference",
        "com.docker.compose.service": "vlm",
        "com.docker.compose.project.config_files": str(path),
    }


def test_compose_manager_atomically_updates_allowlisted_service(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  vlm:\n    # keep this operator note\n    image: example/vlm:1.0\n",
        encoding="utf-8",
    )
    manager = ComposeManager((tmp_path,))

    update = manager.update_image(
        _labels(compose),
        "docker.io/example/vlm:1.0",
        "docker.io/example/vlm:2.0",
    )

    assert update is not None
    assert update.path == compose
    assert update.backup_path.is_file()
    assert "keep this operator note" in compose.read_text(encoding="utf-8")
    with compose.open("r", encoding="utf-8") as source:
        document = YAML(typ="safe").load(source)
    assert document["services"]["vlm"]["image"] == "docker.io/example/vlm:2.0"

    update.rollback()
    assert "image: example/vlm:1.0" in compose.read_text(encoding="utf-8")


def test_compose_manager_rejects_paths_outside_allowlist(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    compose = outside / "compose.yml"
    compose.write_text(
        "services:\n  vlm:\n    image: example/vlm:1.0\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="outside the configured allowlist"):
        ComposeManager((allowed,)).update_image(
            _labels(compose), "example/vlm:1.0", "example/vlm:2.0"
        )


def test_compose_manager_rejects_ambiguous_override_definitions(tmp_path):
    first = tmp_path / "compose.yml"
    second = tmp_path / "override.yml"
    content = "services:\n  vlm:\n    image: example/vlm:1.0\n"
    first.write_text(content, encoding="utf-8")
    second.write_text(content, encoding="utf-8")
    labels = _labels(first)
    labels["com.docker.compose.project.config_files"] = f"{first},{second}"

    with pytest.raises(RuntimeError, match="exactly one file"):
        ComposeManager((tmp_path,)).update_image(
            labels, "example/vlm:1.0", "example/vlm:2.0"
        )
