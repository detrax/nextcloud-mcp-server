"""HTTP receiver for Nextcloud webhooks.

Routes inbound webhooks to the same processor send-stream the scanner uses.
The receiver is registered as a Starlette route at ``/webhooks/nextcloud``
in :mod:`nextcloud_mcp_server.app`.
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.vector.webhook_parser import extract_document_task

logger = logging.getLogger(__name__)


async def handle_nextcloud_webhook(request: Request) -> JSONResponse:
    """Receive a Nextcloud webhook and queue a DocumentTask for vector sync.

    Returns quickly so NC's webhook worker is not blocked. The send-stream is
    read from ``request.app.state.document_send_stream``; when vector sync
    isn't running we return 503 so NC retries delivery.
    """
    try:
        payload = await request.json()
    except Exception as e:
        logger.warning(f"Webhook payload was not valid JSON: {e}")
        return JSONResponse(
            {"status": "error", "message": "invalid JSON"},
            status_code=400,
        )

    task = extract_document_task(payload)
    if task is None:
        event_class = (payload.get("event") or {}).get("class", "<missing>")
        logger.debug("Webhook ignored (unsupported event): %s", event_class)
        return JSONResponse(
            {"status": "ignored", "reason": "unsupported event"},
            status_code=200,
        )

    send_stream = getattr(request.app.state, "document_send_stream", None)
    if send_stream is None:
        logger.warning(
            "Webhook received but vector sync is not running; rejecting so NC retries"
        )
        return JSONResponse(
            {"status": "unavailable", "reason": "vector sync not running"},
            status_code=503,
        )

    try:
        await send_stream.send(task)
    except Exception as e:
        logger.error(
            "Failed to queue webhook task for %s_%s: %s",
            task.doc_type,
            task.doc_id,
            e,
        )
        return JSONResponse(
            {"status": "error", "message": "queue unavailable"},
            status_code=500,
        )

    logger.info(
        "Webhook queued %s_%s (%s) for user %s",
        task.doc_type,
        task.doc_id,
        task.operation,
        task.user_id,
    )
    return JSONResponse(
        {
            "status": "queued",
            "doc_type": task.doc_type,
            "doc_id": task.doc_id,
            "operation": task.operation,
        },
        status_code=200,
    )
