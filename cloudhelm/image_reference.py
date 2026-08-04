import re
from dataclasses import dataclass

TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
PATH_COMPONENT_PATTERN = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$"
)
REGISTRY_PATTERN = re.compile(
    r"^(?:localhost|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::[0-9]{1,5})?$"
)


@dataclass(frozen=True)
class TaggedImage:
    repository: str
    tag: str

    @property
    def canonical_repository(self) -> str:
        parts = self.repository.lower().split("/")
        first = parts[0]
        if "." in first or ":" in first or first == "localhost":
            registry = first
            path = parts[1:]
        else:
            registry = "docker.io"
            path = parts
        if registry in {"index.docker.io", "registry-1.docker.io"}:
            registry = "docker.io"
        if registry == "docker.io" and len(path) == 1:
            path.insert(0, "library")
        return "/".join([registry, *path])

    @property
    def canonical(self) -> str:
        return f"{self.canonical_repository}:{self.tag}"


def parse_tagged_image(value: str) -> TaggedImage:
    value = value.strip()
    if not value or len(value) > 512:
        raise ValueError("镜像名称不能为空且不能超过 512 个字符")
    if "@" in value:
        raise ValueError("只支持镜像 Tag，不支持 digest")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("镜像名称不能包含空白或控制字符")

    slash = value.rfind("/")
    colon = value.rfind(":")
    if colon <= slash:
        raise ValueError("镜像必须包含明确的 Tag")
    repository, tag = value[:colon], value[colon + 1 :]
    if not repository or repository.startswith("/") or repository.endswith("/"):
        raise ValueError("镜像仓库名称无效")
    if repository != repository.lower():
        raise ValueError("镜像仓库名称必须使用小写字母")
    parts = repository.split("/")
    first = parts[0]
    has_registry = "." in first or ":" in first or first == "localhost"
    path = parts[1:] if has_registry else parts
    if (
        (has_registry and not REGISTRY_PATTERN.fullmatch(first))
        or not path
        or any(not PATH_COMPONENT_PATTERN.fullmatch(part) for part in path)
    ):
        raise ValueError("镜像仓库名称无效")
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("镜像 Tag 格式无效")
    return TaggedImage(repository=repository, tag=tag)


def validate_tag_change(current: str, target: str) -> tuple[TaggedImage, TaggedImage]:
    current_ref = parse_tagged_image(current)
    target_ref = parse_tagged_image(target)
    if current_ref.canonical_repository != target_ref.canonical_repository:
        raise ValueError("只能替换同一镜像仓库的不同 Tag")
    if current_ref.tag == target_ref.tag:
        raise ValueError("新旧镜像 Tag 不能相同")
    return current_ref, target_ref
