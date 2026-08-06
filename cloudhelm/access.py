from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudhelm.models import AccessRule, Container, Node, User, UserRole

Permission = Literal["view", "logs", "operate", "manage"]


def load_access_rules(db: Session, user: User) -> list[AccessRule]:
    if user.role == UserRole.admin or not user.resource_restricted:
        return []
    return list(
        db.scalars(select(AccessRule).where(AccessRule.user_id == user.id)).all()
    )


def _permission_granted(rule: AccessRule, permission: Permission) -> bool:
    if rule.can_manage:
        return True
    if permission == "view":
        return rule.can_view
    if permission == "logs":
        return rule.can_logs
    if permission == "operate":
        return rule.can_operate
    return False


def _rule_matches(
    rule: AccessRule, node: Node, container: Container | None = None
) -> bool:
    if rule.scope_type == "all":
        return True
    if rule.scope_type == "environment":
        return rule.environment == node.environment
    if rule.scope_type == "node":
        return rule.node_id == node.id
    if rule.scope_type == "project":
        return (
            container is not None
            and rule.node_id == node.id
            and rule.project == container.compose_project
        )
    if rule.scope_type == "container":
        return container is not None and rule.container_id == container.id
    return False


def can_access(
    user: User,
    rules: list[AccessRule],
    node: Node,
    container: Container | None = None,
    permission: Permission = "view",
) -> bool:
    if user.role == UserRole.admin:
        return True
    if permission == "manage" and user.role != UserRole.operator:
        return False
    if permission == "operate" and user.role != UserRole.operator:
        return False
    if permission == "manage" and not user.resource_restricted:
        return False
    if not user.resource_restricted:
        return True

    if container is None:
        return any(
            _permission_granted(rule, permission)
            and (
                _rule_matches(rule, node)
                or (
                    rule.scope_type in ("project", "container")
                    and rule.node_id == node.id
                )
            )
            for rule in rules
        )
    return any(
        _permission_granted(rule, permission) and _rule_matches(rule, node, container)
        for rule in rules
    )


def can_manage_resources(db: Session, user: User) -> bool:
    if user.role == UserRole.admin:
        return True
    if user.role != UserRole.operator or not user.resource_restricted:
        return False
    return (
        db.scalar(
            select(AccessRule.id)
            .where(
                AccessRule.user_id == user.id,
                AccessRule.can_manage.is_(True),
            )
            .limit(1)
        )
        is not None
    )


def can_view_node_metrics(user: User, rules: list[AccessRule], node: Node) -> bool:
    """Keep host-wide metrics out of container-only permission scopes."""
    if user.role == UserRole.admin or not user.resource_restricted:
        return True
    return any(
        rule.can_view
        and rule.scope_type in {"all", "environment", "node"}
        and _rule_matches(rule, node)
        for rule in rules
    )


def visible_inventory(
    db: Session, user: User
) -> tuple[list[Node], list[Container], list[AccessRule]]:
    nodes = list(db.scalars(select(Node).order_by(Node.environment, Node.name)).all())
    containers = list(
        db.scalars(
            select(Container)
            .where(Container.present.is_(True))
            .order_by(Container.compose_project, Container.name)
        ).all()
    )
    rules = load_access_rules(db, user)
    nodes_by_id = {node.id: node for node in nodes}
    visible_containers = [
        item
        for item in containers
        if (node := nodes_by_id.get(item.node_id))
        and can_access(user, rules, node, item, "view")
    ]
    container_node_ids = {item.node_id for item in visible_containers}
    visible_nodes = [
        node
        for node in nodes
        if node.id in container_node_ids
        or can_access(user, rules, node, permission="view")
    ]
    return visible_nodes, visible_containers, rules
