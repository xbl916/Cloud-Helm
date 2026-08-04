import json
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select, update

from cloudhelm.audit import add_audit
from cloudhelm.dependencies import AgentNode, Config, Db
from cloudhelm.models import Container, Node, Task, TaskStatus
from cloudhelm.schemas import (
    AgentTask,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    TaskResultRequest,
)
from cloudhelm.security import hash_secret

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/enroll", response_model=EnrollResponse)
def enroll(payload: EnrollRequest, db: Db, settings: Config) -> EnrollResponse:
    if not secrets.compare_digest(
        payload.enrollment_token, settings.agent_enrollment_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid enrollment token"
        )

    node = db.scalar(select(Node).where(Node.agent_key == payload.agent_key))
    node_token = secrets.token_urlsafe(40)
    if node:
        node.name = payload.name
        node.hostname = payload.hostname
        node.environment = payload.environment
        node.agent_version = payload.agent_version
        node.agent_token_hash = hash_secret(node_token)
    else:
        node = Node(
            agent_key=payload.agent_key,
            name=payload.name,
            hostname=payload.hostname,
            environment=payload.environment,
            agent_version=payload.agent_version,
            agent_token_hash=hash_secret(node_token),
        )
        db.add(node)
        db.flush()
    add_audit(
        db,
        action="agent.enroll",
        target_type="node",
        target_id=node.id,
        target_name=node.name,
        detail=f"agent_key={payload.agent_key}",
    )
    db.commit()
    return EnrollResponse(node_id=node.id, node_token=node_token)


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def heartbeat(payload: HeartbeatRequest, node: AgentNode, db: Db) -> Response:
    now = datetime.now(UTC)
    node.hostname = payload.hostname
    node.agent_version = payload.agent_version
    node.docker_version = payload.docker_version
    node.os = payload.os
    node.gpu_status = payload.gpu_status
    node.gpu_error = payload.gpu_error
    node.gpus_json = json.dumps(
        [gpu.model_dump() for gpu in payload.gpus],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    node.gpu_updated_at = now
    node.last_seen_at = now

    db.execute(
        update(Container).where(Container.node_id == node.id).values(present=False)
    )
    existing = {
        item.docker_id: item
        for item in db.scalars(
            select(Container).where(Container.node_id == node.id)
        ).all()
    }
    for snapshot in payload.containers:
        item = existing.get(snapshot.docker_id)
        if not item:
            item = Container(
                node_id=node.id, docker_id=snapshot.docker_id, name=snapshot.name
            )
            db.add(item)
        item.name = snapshot.name
        item.image = snapshot.image
        item.status = snapshot.status
        item.health = snapshot.health
        item.compose_project = snapshot.compose_project
        item.compose_service = snapshot.compose_service
        item.cpu_percent = snapshot.cpu_percent
        item.memory_usage = snapshot.memory_usage
        item.memory_limit = snapshot.memory_limit
        item.memory_percent = snapshot.memory_percent
        item.started_at = snapshot.started_at
        item.ports_json = json.dumps(
            snapshot.ports, ensure_ascii=False, separators=(",", ":")
        )
        item.labels_json = json.dumps(
            snapshot.labels, ensure_ascii=False, separators=(",", ":")
        )
        item.gpu_devices_json = json.dumps(
            snapshot.gpu_devices, ensure_ascii=False, separators=(",", ":")
        )
        item.gpu_all = snapshot.gpu_all
        item.present = True
        item.updated_at = now
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/tasks/next", response_model=AgentTask, responses={204: {"description": "No work"}}
)
def next_task(node: AgentNode, db: Db) -> AgentTask | Response:
    retry_before = datetime.now(UTC) - timedelta(seconds=90)
    stale = db.scalars(
        select(Task).where(
            Task.node_id == node.id,
            Task.status == TaskStatus.dispatched,
            Task.dispatched_at < retry_before,
        )
    ).all()
    for item in stale:
        item.status = TaskStatus.pending
        item.dispatched_at = None

    task = db.scalar(
        select(Task)
        .where(Task.node_id == node.id, Task.status == TaskStatus.pending)
        .order_by(Task.created_at.asc())
        .limit(1)
    )
    if not task:
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    task.status = TaskStatus.dispatched
    task.dispatched_at = datetime.now(UTC)
    db.commit()
    return AgentTask(
        id=task.id,
        docker_id=task.docker_id,
        action=task.action,  # type: ignore[arg-type]
        arguments=json.loads(task.arguments_json or "{}"),
    )


@router.post("/tasks/{task_id}/result", status_code=status.HTTP_204_NO_CONTENT)
def task_result(
    task_id: str, payload: TaskResultRequest, node: AgentNode, db: Db, settings: Config
) -> Response:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.node_id == node.id))
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status in (TaskStatus.success, TaskStatus.failed):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    result = (
        (payload.result or "")
        .encode()[: settings.max_task_result_bytes]
        .decode("utf-8", errors="replace")
    )
    error = (
        (payload.error or "")
        .encode()[: settings.max_task_result_bytes]
        .decode("utf-8", errors="replace")
    )
    success = payload.success
    if task.action == "update_image" and success:
        container = db.get(Container, task.container_id) if task.container_id else None
        if not container or not payload.docker_id:
            success = False
            error = "image update result did not include the replacement container ID"
        else:
            arguments = json.loads(task.arguments_json or "{}")
            container.docker_id = payload.docker_id
            container.image = str(arguments.get("target_image") or container.image)
            container.present = True
            container.updated_at = datetime.now(UTC)
    task.status = TaskStatus.success if success else TaskStatus.failed
    task.result = result or None
    task.error = error or None
    task.finished_at = datetime.now(UTC)
    add_audit(
        db,
        action=f"task.{task.action}.result",
        target_type="container",
        target_id=task.container_id,
        success=success,
        detail=(error or result or "completed")[:1000],
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
