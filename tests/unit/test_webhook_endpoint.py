"""Unit tests for the ``/webhooks/nextcloud`` HTTP receiver.

Builds a minimal Starlette app around ``handle_nextcloud_webhook`` so we can
drive it with ``TestClient`` without standing up the full FastMCP server.
"""

import anyio
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.vector.webhook_receiver import handle_nextcloud_webhook

pytestmark = pytest.mark.unit


def _make_app(send_stream=None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/webhooks/nextcloud", handle_nextcloud_webhook, methods=["POST"])
        ]
    )
    app.state.document_send_stream = send_stream
    return app


_NOTE_CREATED = {
    "user": {"uid": "admin", "displayName": "admin"},
    "time": 1762850245,
    "event": {
        "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        "node": {
            "id": 437,
            "path": "/admin/files/Notes/Webhooks/Webhook Test Note.md",
        },
    },
}


_NOTE_DELETED = {
    "user": {"uid": "alice"},
    "time": 1762851093,
    "event": {
        "class": "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent",
        "node": {"id": 99, "path": "/alice/files/Notes/foo.md"},
    },
}


def test_index_event_queues_task_and_returns_200():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["operation"] == "index"
    assert response.json()["doc_id"] == "437"

    task = receive_stream.receive_nowait()
    assert task.user_id == "admin"
    assert task.doc_id == "437"
    assert task.operation == "index"
    assert task.doc_type == "note"


def test_delete_event_queues_delete_task():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_DELETED)

    assert response.status_code == 200
    assert response.json()["operation"] == "delete"

    task = receive_stream.receive_nowait()
    assert task.operation == "delete"
    assert task.doc_id == "99"
    assert task.user_id == "alice"


def test_unsupported_event_is_ignored():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    payload = {
        "user": {"uid": "admin"},
        "time": 1,
        "event": {
            "class": "OCP\\Calendar\\Events\\CalendarObjectCreatedEvent",
            "objectData": {"id": 7},
        },
    }

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

    with pytest.raises(anyio.WouldBlock):
        receive_stream.receive_nowait()


def test_invalid_json_returns_400():
    app = _make_app(send_stream=None)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/nextcloud",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["status"] == "error"


def test_returns_503_when_send_stream_not_wired():
    """Vector sync not running → tell NC to retry instead of dropping the
    event."""
    app = _make_app(send_stream=None)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_returns_500_when_stream_is_closed():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=1)
    receive_stream.close()  # close receiver → send raises BrokenResourceError
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 500
    assert response.json()["status"] == "error"
