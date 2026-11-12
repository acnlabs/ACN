import httpx
import pytest

from acn.services.board_access_client import check_private_board_access


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "allowed"),
    [
        (200, {"allowed": True}, True),
        (200, {"allowed": False}, False),
        (200, {"allowed": "true"}, False),
        (503, {"allowed": True}, False),
    ],
)
async def test_board_access_requires_explicit_grant(status, payload, allowed):
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/labs/boards/tb_test/access"
        assert request.headers["X-Internal-Token"] == "test-token"
        assert request.content == b'{"principal_id":"auth0|tester"}'
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert (
            await check_private_board_access(
                client, "http://backend.test/", "test-token", "tb_test", "auth0|tester"
            )
            is allowed
        )


@pytest.mark.asyncio
async def test_private_board_detail_requires_current_membership(monkeypatch):
    from unittest.mock import AsyncMock

    from acn.core.errors import ACNHTTPError
    from acn.routes import tasks

    check = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(tasks, "check_private_board_access", check)
    await tasks._assert_private_board_access("ordinary-subnet", "task-1", None)
    await tasks._assert_private_board_access("board:tb_test", "task-1", "agent-1")
    with pytest.raises(ACNHTTPError) as denied:
        await tasks._assert_private_board_access("board:tb_test", "task-1", "agent-1")
    assert denied.value.status_code == 404
    with pytest.raises(ACNHTTPError) as anonymous:
        await tasks._assert_private_board_access("board:tb_test", "task-1", None)
    assert anonymous.value.status_code == 404
    assert check.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", ["accept_task", "agent_accept_task", "submit_task", "agent_submit_task"]
)
async def test_revoked_board_member_cannot_accept_or_submit(monkeypatch, route):
    from inspect import unwrap
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from starlette.requests import Request

    from acn.core.errors import ACNHTTPError
    from acn.routes import tasks

    check = AsyncMock(return_value=False)
    monkeypatch.setattr(tasks, "check_private_board_access", check)
    service = AsyncMock()
    service.get_task.return_value = SimpleNamespace(subnet_slug="board:tb_test")
    actor = (
        {"agent_info": {"agent_id": "agent-1"}}
        if route.startswith("agent_")
        else {"payload": {"type": "agent", "sub": "agent-1"}}
    )
    if route.endswith("submit_task"):
        actor["body"] = tasks.TaskSubmitRequest(submission="result")

    handler = unwrap(getattr(tasks, route))
    with pytest.raises(ACNHTTPError) as denied:
        await handler(
            Request({"type": "http", "headers": []}), "task-1", task_service=service, **actor
        )
    assert denied.value.status_code == 404
    assert check.await_args.args[-1] == "agent-1"
    service.accept_task.assert_not_awaited()
    service.submit_task.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("board_id", "grant", "expected"),
    [
        (None, True, []),
        ("tb_other", True, []),
        ("tb_test", False, []),
        ("tb_test", True, ["task-1"]),
    ],
)
@pytest.mark.parametrize("trusted", [False, True])
async def test_private_task_list_requires_matching_board_and_current_grant(
    monkeypatch, board_id, grant, expected, trusted
):
    from inspect import unwrap
    from unittest.mock import AsyncMock

    from starlette.requests import Request

    from acn.core.entities.task import Task
    from acn.routes import tasks

    monkeypatch.setattr(tasks, "_resolve_caller_identity", AsyncMock(return_value="agent-1"))
    check = AsyncMock(return_value=grant)
    monkeypatch.setattr(tasks, "check_private_board_access", check)
    monkeypatch.setattr(tasks.settings, "internal_api_token", "test-token")
    headers = [
        (b"x-board-viewer", b"forwarded-agent"),
        (b"x-internal-token", b"test-token" if trusted else b"invalid-token"),
    ]
    service = AsyncMock()
    service.list_tasks.return_value = [
        Task(
            task_id="task-1",
            creator_type="human",
            creator_id="owner",
            creator_name="Owner",
            title="Private work",
            description="Private work",
            subnet_slug="board:tb_test",
        ),
    ]
    response = await unwrap(tasks.list_tasks)(
        Request({"type": "http", "headers": headers}),
        mode=None,
        status=None,
        tags=None,
        creator_id=None,
        assignee_id=None,
        group_id=None,
        board_id=board_id,
        limit=20,
        offset=0,
        task_service=service,
        credentials=None,
    )
    assert [task.task_id for task in response.tasks] == expected
    if board_id == "tb_test":
        assert check.await_args.args[-1] == ("forwarded-agent" if trusted else "agent-1")


def test_private_board_publish_http_contract(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from acn.api import app
    from acn.core.entities.task import Task
    from acn.routes import dependencies, tasks

    monkeypatch.setattr(dependencies.settings, "internal_api_token", "test-token")
    monkeypatch.setattr(dependencies.limiter, "enabled", False)
    membership = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(tasks, "check_private_board_access", membership)
    service = AsyncMock()
    service.create_task.return_value = Task(
        task_id="private-task",
        creator_type="human",
        creator_id="auth0|owner",
        creator_name="Owner",
        title="Private task",
        description="Private task description",
        subnet_slug="board:tb_test",
    )
    monkeypatch.setitem(app.dependency_overrides, tasks.get_task_service, lambda: service)
    path = "/api/v1/tasks/internal/boards/tb_test/tasks"
    body = {
        "title": "Private task",
        "description": "Private task description",
        "deadline_hours": 24,
        "reward": "0",
        "use_escrow": False,
    }
    headers = {"X-Creator-Id": "auth0|owner", "X-Internal-Token": "test-token"}

    client = TestClient(app)
    assert (
        client.post(path, json=body, headers={**headers, "X-Internal-Token": "wrong"}).status_code
        == 403
    )
    assert client.post(path, json=body, headers=headers).status_code == 404
    service.create_task.assert_not_awaited()

    created = client.post(path, json=body, headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["subnet_slug"] == "board:tb_test"
    assert service.create_task.await_args.kwargs["creator_id"] == "auth0|owner"
    assert service.create_task.await_args.kwargs["private_board_id"] == "tb_test"
