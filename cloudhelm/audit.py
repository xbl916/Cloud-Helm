import json

from fastapi import Request
from sqlalchemy.orm import Session

from cloudhelm.config import Settings
from cloudhelm.dependencies import request_ip
from cloudhelm.models import AuditLog, User

AUDIT_ACTION_LABELS = {
    "agent.enroll": "Agent 注册",
    "wecom.login": "企业微信登录",
    "wecom.login.denied": "企业微信登录被拒绝",
    "wecom.mini.login": "企业微信小程序登录",
    "wecom.mini.login.denied": "企业微信小程序登录被拒绝",
    "logout": "退出登录",
    "node.gpu_baseline.reset": "重设 GPU 掉卡检测基线",
    "alert.rule.create": "创建告警规则",
    "alert.rule.update": "修改告警规则",
    "alert.rule.delete": "删除告警规则",
    "alert.acknowledge": "确认告警",
    "alert.notification.test": "发送模拟告警",
    "user.create": "创建用户",
    "user.update": "修改用户",
    "user.sessions.revoke": "撤销用户登录会话",
    "user.access.update": "修改用户权限与告警订阅",
    "user.access.management.revoke": "撤销用户资源管理权",
}

AUDIT_TARGET_LABELS = {
    "node": "节点",
    "container": "容器",
    "alert_rule": "告警规则",
    "user": "用户",
    "session": "登录会话",
}

CONTAINER_ACTION_LABELS = {
    "start": "启动容器",
    "stop": "停止容器",
    "restart": "重启容器",
    "logs": "查看容器日志",
    "update_image": "更换容器镜像 Tag",
}


def audit_action_label(action: str) -> str:
    if action in AUDIT_ACTION_LABELS:
        return AUDIT_ACTION_LABELS[action]
    if action.startswith("container."):
        return CONTAINER_ACTION_LABELS.get(action.removeprefix("container."), action)
    if action.startswith("task.") and action.endswith(".result"):
        task_action = action.removeprefix("task.").removesuffix(".result")
        label = CONTAINER_ACTION_LABELS.get(task_action, task_action)
        return f"{label}执行结果"
    return action


def audit_target_label(target_type: str) -> str:
    return AUDIT_TARGET_LABELS.get(target_type, target_type)


def _localized_json_detail(detail: str) -> str | None:
    try:
        value = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    field_labels = {
        "display_name": "显示名称",
        "wecom_userid": "企微 UserId",
        "role": "角色",
        "is_active": "启用状态",
    }
    role_labels = {"admin": "管理员", "operator": "运维", "viewer": "只读"}
    parts = []
    for key, raw in value.items():
        label = field_labels.get(key, key)
        if key == "role":
            raw = role_labels.get(str(raw), raw)
        elif isinstance(raw, bool):
            raw = "是" if raw else "否"
        parts.append(f"{label}={raw}")
    return "，".join(parts)


def audit_detail_label(action: str, detail: str) -> str:
    if not detail:
        return ""
    if action == "user.update":
        localized = _localized_json_detail(detail)
        if localized is not None:
            return localized
    exact = {
        "member is not bound or is disabled": "成员未绑定或已停用",
        "task queued": "任务已排队",
        "completed": "执行完成",
        "container started": "容器已启动",
        "container stopped": "容器已停止",
        "container restarted": "容器已重启",
    }
    if detail in exact:
        return exact[detail]
    if action == "container.update_image" and detail.startswith("task queued: "):
        return f"镜像更新任务已排队：{detail.removeprefix('task queued: ')}"
    if detail.startswith("container image updated to "):
        return f"容器镜像已更新为 {detail.removeprefix('container image updated to ')}"
    replacements = {
        "agent_key=": "Agent 标识=",
        "metric=": "指标=",
        "threshold=": "阈值=",
        "enabled=": "启用=",
        "role=": "角色=",
        "wecom_userid=": "企微 UserId=",
        "sessions=": "会话数=",
        "rule=": "规则=",
        "expected_gpu_count=": "GPU 基线数量=",
        "restricted=": "限制资源范围=",
        "rules=": "授权规则数=",
        "global_alert_notify=": "全局告警通知=",
        "partial=": "部分范围修改=",
        "replaced=": "替换规则数=",
    }
    localized = detail
    for source, target in replacements.items():
        localized = localized.replace(source, target)
    localized = (
        localized.replace("角色=admin", "角色=管理员")
        .replace("角色=operator", "角色=运维")
        .replace("角色=viewer", "角色=只读")
    )
    localized = localized.replace("True", "是").replace("False", "否")
    return localized.replace(", ", "，").replace("->", "→")


def add_audit(
    db: Session,
    *,
    action: str,
    target_type: str,
    user: User | None = None,
    target_id: str | None = None,
    target_name: str | None = None,
    success: bool = True,
    detail: str = "",
    request: Request | None = None,
    settings: Settings | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else "system",
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        success=success,
        detail=detail[:4000],
        ip_address=request_ip(request, settings) if request and settings else None,
    )
    db.add(entry)
    return entry
