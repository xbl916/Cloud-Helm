from cloudhelm.access import can_access, can_receive_alert, can_view_node_metrics
from cloudhelm.models import AccessRule, Container, Node, User, UserRole


def inventory():
    node = Node(
        id="node-1",
        agent_key="agent-key",
        name="生产节点",
        environment="production",
        agent_token_hash="hash",
    )
    container = Container(
        id="container-1",
        node_id=node.id,
        docker_id="docker-container-id",
        name="web-api",
        compose_project="web",
    )
    return node, container


def test_unrestricted_role_still_limits_operations():
    node, container = inventory()
    viewer = User(
        id="viewer-1",
        username="viewer",
        wecom_userid="viewer",
        display_name="Viewer",
        role=UserRole.viewer,
        resource_restricted=False,
    )
    assert can_access(viewer, [], node, container, "view")
    assert can_access(viewer, [], node, container, "logs")
    assert not can_access(viewer, [], node, container, "operate")


def test_project_rule_grants_only_selected_project():
    node, container = inventory()
    operator = User(
        id="operator-1",
        username="operator",
        wecom_userid="operator",
        display_name="Operator",
        role=UserRole.operator,
        resource_restricted=True,
    )
    rule = AccessRule(
        user_id=operator.id,
        scope_type="project",
        node_id=node.id,
        project="web",
        can_view=True,
        can_logs=True,
        can_operate=True,
    )
    assert can_access(operator, [rule], node, container, "operate")
    other = Container(
        id="container-2",
        node_id=node.id,
        docker_id="another-docker-id",
        name="database",
        compose_project="database",
    )
    assert not can_access(operator, [rule], node, other, "view")
    assert not can_view_node_metrics(operator, [rule], node)


def test_node_rule_grants_host_metrics():
    node, _ = inventory()
    viewer = User(
        id="viewer-2",
        username="node-viewer",
        wecom_userid="node-viewer",
        display_name="Node Viewer",
        role=UserRole.viewer,
        resource_restricted=True,
    )
    rule = AccessRule(
        user_id=viewer.id,
        scope_type="node",
        node_id=node.id,
        can_view=True,
    )
    assert can_view_node_metrics(viewer, [rule], node)


def test_resource_manager_is_limited_to_managed_scope():
    node, container = inventory()
    manager = User(
        id="manager-1",
        username="manager",
        wecom_userid="manager",
        display_name="Resource Manager",
        role=UserRole.operator,
        resource_restricted=True,
    )
    rule = AccessRule(
        user_id=manager.id,
        scope_type="container",
        node_id=node.id,
        container_id=container.id,
        can_manage=True,
    )
    assert can_access(manager, [rule], node, container, "manage")
    assert can_access(manager, [rule], node, container, "operate")
    other = Container(
        id="container-2",
        node_id=node.id,
        docker_id="other-docker-id",
        name="other",
    )
    assert not can_access(manager, [rule], node, other, "manage")


def test_unrestricted_operator_does_not_implicitly_manage_resources():
    node, container = inventory()
    operator = User(
        id="operator-global",
        username="operator-global",
        wecom_userid="operator-global",
        display_name="Global Operator",
        role=UserRole.operator,
        resource_restricted=False,
    )
    assert can_access(operator, [], node, container, "operate")
    assert not can_access(operator, [], node, container, "manage")


def test_alert_subscription_requires_current_matching_access():
    node, container = inventory()
    subscriber = User(
        id="alert-subscriber",
        username="alert-subscriber",
        wecom_userid="alert-subscriber",
        display_name="Alert Subscriber",
        role=UserRole.viewer,
        resource_restricted=True,
        is_active=True,
    )
    project_rule = AccessRule(
        user_id=subscriber.id,
        scope_type="project",
        node_id=node.id,
        project="web",
        can_view=True,
        alert_notify=True,
    )
    assert can_receive_alert(subscriber, [project_rule], node, container)
    assert not can_receive_alert(subscriber, [project_rule], node)
    project_rule.alert_notify = False
    assert not can_receive_alert(subscriber, [project_rule], node, container)
    project_rule.alert_notify = True
    subscriber.is_active = False
    assert not can_receive_alert(subscriber, [project_rule], node, container)


def test_unrestricted_alert_subscription_uses_global_switch():
    node, container = inventory()
    subscriber = User(
        id="global-alert-subscriber",
        username="global-alert-subscriber",
        wecom_userid="global-alert-subscriber",
        display_name="Global Alert Subscriber",
        role=UserRole.viewer,
        resource_restricted=False,
        alert_notifications=True,
        is_active=True,
    )
    assert can_receive_alert(subscriber, [], node, container)
    subscriber.alert_notifications = False
    assert not can_receive_alert(subscriber, [], node, container)
