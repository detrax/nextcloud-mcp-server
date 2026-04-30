"""HTTP receiver for Nextcloud webhooks.

Routes inbound webhooks to the same processor send-stream the scanner uses.
The receiver is registered as a Starlette route at ``/webhooks/nextcloud``
in :mod:`nextcloud_mcp_server.app`.
"""

import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.webhook_parser import extract_document_task

logger = logging.getLogger(__name__)

_warned_about_missing_secret = False


def _warn_missing_secret_once() -> None:
    """Log a one-time WARNING when WEBHOOK_SECRET is unset.

    The receiver still accepts unauthenticated POSTs in this case so existing
    deployments keep working, but the operator should know they're running
    without webhook auth.
    """
    global _warned_about_missing_secret
    if _warned_about_missing_secret:
        return
    _warned_about_missing_secret = True
    logger.warning(
        "WEBHOOK_SECRET is not set; /webhooks/nextcloud accepts "
        "unauthenticated requests. Set WEBHOOK_SECRET and re-register "
        "webhooks to enable Authorization: Bearer validation."
    )


async def handle_nextcloud_webhook(request: Request) -> JSONResponse:
    """Receive a Nextcloud webhook and queue a DocumentTask for vector sync.

    Returns quickly so NC's webhook worker is not blocked. The send-stream is
    read from ``request.app.state.document_send_stream``; when vector sync
    isn't running we return 503 so NC retries delivery.

    When ``WEBHOOK_SECRET`` is set, the request must carry
    ``Authorization: Bearer <secret>`` (registered via ``authData`` so NC
    forwards it on every delivery); requests without a valid header are
    rejected with 401 before any further work.
    """
    secret = get_settings().webhook_secret
    if secret:
        provided = request.headers.get("authorization", "")
        expected = f"Bearer {secret}"
        # Always run compare_digest so the constant-time path is taken even
        # when the header is missing — `compare_digest("", expected)` returns
        # False without leaking length information.
        if not hmac.compare_digest(provided, expected):
            logger.warning("Webhook rejected: missing or invalid Authorization header")
            return JSONResponse(
                {"status": "unauthorized"},
                status_code=401,
            )
    else:
        _warn_missing_secret_once()

    try:
        payload = await request.json()
    except Exception as e:
        logger.warning("Webhook payload was not valid JSON: %s", e)
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
