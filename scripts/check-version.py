"""Fail when release-facing files disagree about the project version."""

import re
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def require_match(relative_path: str, pattern: str, expected: str) -> None:
    values = re.findall(pattern, read(relative_path), flags=re.MULTILINE)
    if not values:
        raise SystemExit(f"{relative_path}: version marker not found")
    unexpected = sorted(set(values) - {expected})
    if unexpected:
        raise SystemExit(
            f"{relative_path}: expected version {expected}, found {unexpected}"
        )


def main() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        project_version = tomllib.load(file)["project"]["version"]
    expected = sys.argv[1] if len(sys.argv) > 1 else project_version
    if project_version != expected:
        raise SystemExit(
            f"pyproject.toml: expected version {expected}, found {project_version}"
        )

    require_match("cloudhelm/__init__.py", r'__version__ = "([^"]+)"', expected)
    require_match(
        "cloudhelm_agent/__init__.py", r'__version__ = "([^"]+)"', expected
    )
    image_version_pattern = r"cloud-helm-(?:server|agent):([0-9]+\.[0-9]+\.[0-9]+)"
    require_match("docker-compose.yml", image_version_pattern, expected)
    require_match(
        "deploy/postgres.compose.yml",
        image_version_pattern,
        expected,
    )
    require_match(
        "deploy/agent.compose.yml",
        image_version_pattern,
        expected,
    )
    require_match(
        "cloudhelm/static/index.html",
        r"Cloud Helm ([0-9]+\.[0-9]+\.[0-9]+)",
        expected,
    )
    require_match(
        "README.md", r"当前版本为 `([0-9]+\.[0-9]+\.[0-9]+)`", expected
    )
    require_match(
        "RELEASE_NOTES.md",
        r"^# Cloud Helm ([0-9]+\.[0-9]+\.[0-9]+)$",
        expected,
    )
    print(f"Version consistency check passed: {expected}")


if __name__ == "__main__":
    main()
