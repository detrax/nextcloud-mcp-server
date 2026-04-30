"""Parse Nextcloud webhook payloads into DocumentTask objects.

Maps Nextcloud webhook events to vector-sync DocumentTasks. The handler at
``/webhooks/nextcloud`` calls :func:`extract_document_task` and forwards any
non-None result to the same processor send-stream the scanner uses.

Currently scoped to file (note) events. Calendar / Tables events fall through
to ``None`` for now; those parsers can be added in follow-up changes.

See ADR-010 for the design and ``webhook-testing-findings.md`` for real
captured payloads.
"""

import logging
import re

from nextcloud_mcp_server.vector.scanner import DocumentTask

logger = logging.getLogger(__name__)

_FILE_EVENT_CREATED = "OCP\\Files\\Events\\Node\\NodeCreatedEvent"
_FILE_EVENT_WRITTEN = "OCP\\Files\\Events\\Node\\NodeWrittenEvent"
_FILE_EVENT_BEFORE_DELETED = "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent"

# Matches paths inside any user's Notes folder ending in .md, e.g.
# "/admin/files/Notes/Sub/Note.md" or "/alice/files/Notes/foo.md".
_NOTES_PATH_RE = re.compile(r"^/[^/]+/files/Notes/.+\.md$")


def extract_document_task(payload: dict) -> DocumentTask | None:
    """Convert a Nextcloud webhook payload into a DocumentTask.

    Returns None for any event we don't (yet) handle, or any event whose
    target isn't a markdown file under a user's Notes folder. Callers should
    treat None as "ignored" — not an error.
    """
    try:
        event = payload["event"]
        event_class = event["class"]
        user_id = payload["user"]["uid"]
        time = int(payload.get("time", 0) or 0)
    except (KeyError, TypeError, ValueError):
        logger.debug("Webhook payload has missing or malformed envelope fields")
        return None

    if event_class in (
        _FILE_EVENT_CREATED,
        _FILE_EVENT_WRITTEN,
        _FILE_EVENT_BEFORE_DELETED,
    ):
        return _parse_file_event(event_class, event, user_id, time)

    logger.debug("Ignoring webhook for unsupported event: %s", event_class)
    return None


def _parse_file_event(
    event_class: str, event: dict, user_id: str, time: int
) -> DocumentTask | None:
    node = event.get("node") or {}
    path = node.get("path", "")
    node_id = node.get("id")

    if not _NOTES_PATH_RE.match(path):
        # Not a note file — could be a parent folder, an unrelated file, etc.
        return None

    if node_id is None:
        # BeforeNodeDeletedEvent should still carry node.id; if it doesn't
        # we can't address the Qdrant points to delete. Skip rather than
        # guess — the polling scanner will catch up via its grace period.
        logger.warning(
            "Webhook %s for note %s missing node.id; falling back to scanner",
            event_class,
            path,
        )
        return None

    operation = "delete" if event_class == _FILE_EVENT_BEFORE_DELETED else "index"

    return DocumentTask(
        user_id=user_id,
        doc_id=str(node_id),
        doc_type="note",
        operation=operation,
        modified_at=time,
    )
