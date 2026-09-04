import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from cloudhelm.image_reference import parse_tagged_image


@dataclass
class ComposeUpdate:
    path: Path
    backup_path: Path

    def rollback(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.cloudhelm-restore")
        shutil.copy2(self.backup_path, temporary)
        os.replace(temporary, self.path)


class ComposeManager:
    """Updates only Compose files located below explicitly allowed host paths."""

    def __init__(self, roots: tuple[Path, ...]):
        self.roots = tuple(root.resolve() for root in roots)

    def _allowed_path(self, raw_path: str) -> Path:
        path = Path(raw_path).resolve(strict=True)
        if not path.is_file() or not any(path.is_relative_to(root) for root in self.roots):
            raise RuntimeError(f"Compose file is outside the configured allowlist: {path}")
        return path

    @staticmethod
    def _config_paths(labels: dict[str, Any]) -> list[str]:
        raw = str(labels.get("com.docker.compose.project.config_files") or "")
        return [value.strip() for value in raw.split(",") if value.strip()]

    def _find_image_definition(
        self, labels: dict[str, Any], expected_image: str, target_image: str
    ) -> tuple[Path, Any, dict[str, Any]] | None:
        project = str(labels.get("com.docker.compose.project") or "")
        service_name = str(labels.get("com.docker.compose.service") or "")
        if not project and not service_name:
            return None
        if not self.roots:
            raise RuntimeError(
                "Compose update is disabled; configure CLOUDHELM_AGENT_COMPOSE_ROOTS"
            )
        if not project or not service_name:
            raise RuntimeError("Compose project or service label is missing")
        raw_paths = self._config_paths(labels)
        if not raw_paths:
            raise RuntimeError("Compose config file label is missing; recreate with Compose v2")

        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        matches: list[tuple[Path, Any, dict[str, Any]]] = []
        for raw_path in raw_paths:
            path = self._allowed_path(raw_path)
            try:
                with path.open("r", encoding="utf-8") as source:
                    document = yaml.load(source)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Cannot read Compose file {path}: {exc}") from exc
            services = document.get("services") if isinstance(document, dict) else None
            service = services.get(service_name) if isinstance(services, dict) else None
            if isinstance(service, dict) and "image" in service:
                matches.append((path, document, service))
        if len(matches) != 1:
            raise RuntimeError(
                f"Compose image definition must resolve to exactly one file; found {len(matches)}"
            )

        path, document, service = matches[0]
        configured = str(service.get("image") or "")
        try:
            configured_ref = parse_tagged_image(configured)
            expected_ref = parse_tagged_image(expected_image)
        except ValueError as exc:
            raise RuntimeError(f"Invalid Compose image: {exc}") from exc
        if configured_ref.canonical != expected_ref.canonical:
            raise RuntimeError(
                "Compose image changed or does not match the running container"
            )
        if configured_ref.canonical_repository != parse_tagged_image(
            target_image
        ).canonical_repository:
            raise RuntimeError("Compose target image must use the same repository")
        return path, document, service

    def validate_image(
        self, labels: dict[str, Any], expected_image: str, target_image: str
    ) -> Path | None:
        match = self._find_image_definition(labels, expected_image, target_image)
        return match[0] if match else None

    def update_image(
        self, labels: dict[str, Any], expected_image: str, target_image: str
    ) -> ComposeUpdate | None:
        match = self._find_image_definition(labels, expected_image, target_image)
        if not match:
            return None
        path, document, service = match
        service["image"] = target_image
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.cloudhelm-{timestamp}.bak")
        shutil.copy2(path, backup)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.cloudhelm-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                yaml.dump(document, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, path.stat().st_mode)
            os.replace(temporary_name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            shutil.copy2(backup, path)
            raise
        return ComposeUpdate(path=path, backup_path=backup)
