"""Verify-on-read access checks for semantic search results (ADR-019).

The vector index is a recall layer; Nextcloud is the source of truth for
access. This module filters search results by checking each unique document
against Nextcloud at query time, dropping any that the user can no longer
access (deleted, unshared, etc.) and lazily evicting them from the index.

Per-doc_type verifiers are registered in ``_VERIFIERS``. Each takes the
authenticated client, a list of doc_ids, and the user_id, and returns the
subset of doc_ids that are currently accessible. The dispatch deliberately
groups by doc_type so doc-types with cheap batch endpoints (news_item) can
do a single fetch rather than one round-trip per result.

Failure policy:

- Definitive 403/404 from Nextcloud → drop the result and schedule eviction.
- Transient errors (5xx, network blips, unexpected exceptions) → keep the
  result and log a warning. We never silently shrink result sets due to
  flakes; the next query will re-verify.
- Unsupported doc_type (no registered verifier) → keep the result and log a
  warning. Verification is opt-in per type; a missing verifier is a soft
  failure, not a search failure.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from httpx import HTTPStatusError
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.vector.eviction import delete_document_points
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


BatchVerifier = Callable[[Any, list[int | str], str], Awaitable[set[int | str]]]
"""(client, doc_ids, user_id) -> set of accessible doc_ids."""


# ---------------------------------------------------------------------------
# Per-doc-type verifiers
# ---------------------------------------------------------------------------


def _is_definitive_404_or_403(exc: BaseException) -> bool:
    """Return True if exc indicates the document is definitively inaccessible."""
    if isinstance(exc, HTTPStatusError):
        return exc.response.status_code in (403, 404)
    return False


async def _verify_notes(
    client: Any, doc_ids: list[int | str], user_id: str
) -> set[int | str]:
    accessible: set[int | str] = set()

    async def check(doc_id: int | str) -> None:
        try:
            await client.notes.get_note(int(doc_id))
            accessible.add(doc_id)
        except HTTPStatusError as e:
            if _is_definitive_404_or_403(e):
                return
            logger.warning(
                "Transient error verifying note %s: %s %s; keeping result",
                doc_id,
                e.response.status_code,
                e,
            )
            accessible.add(doc_id)
        except Exception as e:
            logger.warning(
                "Unexpected error verifying note %s: %s; keeping result",
                doc_id,
                e,
            )
            accessible.add(doc_id)

    async with anyio.create_task_group() as tg:
        for doc_id in doc_ids:
            tg.start_soon(check, doc_id)

    return accessible


async def _verify_files(
    client: Any, doc_ids: list[int | str], user_id: str
) -> set[int | str]:
    accessible: set[int | str] = set()

    async def check(doc_id: int | str) -> None:
        # Resolve file_id → file_path from Qdrant payload
        file_path = await _resolve_file_path(user_id, doc_id)
        if file_path is None:
            # Cannot verify without a path; treat as accessible to avoid
            # silently dropping legitimate results when payload is missing
            logger.warning(
                "No file_path in Qdrant for file_id %s; keeping result "
                "(verification skipped)",
                doc_id,
            )
            accessible.add(doc_id)
            return

        try:
            info = await client.webdav.get_file_info(file_path)
            if info is None:
                # get_file_info returns None on definitive 404
                return
            accessible.add(doc_id)
        except HTTPStatusError as e:
            if _is_definitive_404_or_403(e):
                return
            logger.warning(
                "Transient error verifying file %s (%s): %s %s; keeping result",
                doc_id,
                file_path,
                e.response.status_code,
                e,
            )
            accessible.add(doc_id)
        except Exception as e:
            logger.warning(
                "Unexpected error verifying file %s (%s): %s; keeping result",
                doc_id,
                file_path,
                e,
            )
            accessible.add(doc_id)

    async with anyio.create_task_group() as tg:
        for doc_id in doc_ids:
            tg.start_soon(check, doc_id)

    return accessible


async def _verify_deck_cards(
    client: Any, doc_ids: list[int | str], user_id: str
) -> set[int | str]:
    accessible: set[int | str] = set()

    async def check(doc_id: int | str) -> None:
        # Resolve card_id → (board_id, stack_id) from Qdrant payload
        meta = await _resolve_deck_metadata(user_id, int(doc_id))
        if meta is None:
            # Without metadata we cannot run the cheap fast-path. Per ADR-019
            # we deliberately do NOT fall back to O(boards × stacks) iteration
            # in the search hot path; treat as accessible.
            logger.warning(
                "No deck metadata in Qdrant for card %s; keeping result "
                "(verification skipped, legacy data without board_id/stack_id)",
                doc_id,
            )
            accessible.add(doc_id)
            return

        try:
            await client.deck.get_card(
                board_id=meta["board_id"],
                stack_id=meta["stack_id"],
                card_id=int(doc_id),
            )
            accessible.add(doc_id)
        except HTTPStatusError as e:
            if _is_definitive_404_or_403(e):
                return
            logger.warning(
                "Transient error verifying deck card %s: %s %s; keeping result",
                doc_id,
                e.response.status_code,
                e,
            )
            accessible.add(doc_id)
        except Exception as e:
            logger.warning(
                "Unexpected error verifying deck card %s: %s; keeping result",
                doc_id,
                e,
            )
            accessible.add(doc_id)

    async with anyio.create_task_group() as tg:
        for doc_id in doc_ids:
            tg.start_soon(check, doc_id)

    return accessible


async def _verify_news_items(
    client: Any, doc_ids: list[int | str], user_id: str
) -> set[int | str]:
    """Batch-verify news items with a single fetch.

    The Nextcloud News API has no per-item endpoint, so ``news.get_item`` is
    implemented as a fetch-all + filter — which would be O(N × all_items) if
    called per id. Instead we fetch once and intersect.
    """
    requested = {int(d) for d in doc_ids}

    try:
        items = await client.news.get_items(batch_size=-1, get_read=True)
    except HTTPStatusError as e:
        # If the News API itself is gone (app disabled, user lost access),
        # treat *all* requested items as inaccessible. Eviction will reclaim.
        if _is_definitive_404_or_403(e):
            logger.info(
                "News API returned %s for user %s; treating all %d news_items as inaccessible",
                e.response.status_code,
                user_id,
                len(requested),
            )
            return set()
        logger.warning(
            "Transient error fetching news items for verification: %s %s; keeping all results",
            e.response.status_code,
            e,
        )
        return set(doc_ids)
    except Exception as e:
        logger.warning(
            "Unexpected error fetching news items for verification: %s; keeping all results",
            e,
        )
        return set(doc_ids)

    present_ids = {int(item.get("id")) for item in items if item.get("id") is not None}
    # Map back to the original doc_id types (the caller may pass ints or strs)
    accessible: set[int | str] = set()
    for d in doc_ids:
        if int(d) in present_ids and int(d) in requested:
            accessible.add(d)
    return accessible


_VERIFIERS: dict[str, BatchVerifier] = {
    "note": _verify_notes,
    "file": _verify_files,
    "deck_card": _verify_deck_cards,
    "news_item": _verify_news_items,
}


def get_supported_doc_types() -> set[str]:
    """Return the set of doc_types that have registered verifiers.

    Used by CI guards and tests to ensure every indexed doc_type has a
    verifier (see ADR-019 implementation checklist).
    """
    return set(_VERIFIERS.keys())


# ---------------------------------------------------------------------------
# Qdrant payload lookup helpers
# ---------------------------------------------------------------------------


async def _resolve_file_path(user_id: str, doc_id: int | str) -> str | None:
    """Look up file_path for a file_id from any chunk's Qdrant payload."""
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        scroll_result = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value="file")),
                ]
            ),
            limit=1,
            with_payload=["file_path"],
            with_vectors=False,
        )

        if scroll_result[0]:
            point = scroll_result[0][0]
            file_path = point.payload.get("file_path") if point.payload else None
            if file_path:
                return str(file_path)
        return None

    except Exception as e:
        logger.debug("Error resolving file_path for file_id %s: %s", doc_id, e)
        return None


async def _resolve_deck_metadata(user_id: str, card_id: int) -> dict[str, int] | None:
    """Look up (board_id, stack_id) for a deck card from any chunk's payload."""
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        scroll_result = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=card_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value="deck_card")),
                ]
            ),
            limit=1,
            with_payload=["board_id", "stack_id"],
            with_vectors=False,
        )

        if scroll_result[0]:
            point = scroll_result[0][0]
            payload = point.payload or {}
            board_id = payload.get("board_id")
            stack_id = payload.get("stack_id")
            if board_id is not None and stack_id is not None:
                return {"board_id": int(board_id), "stack_id": int(stack_id)}
        return None

    except Exception as e:
        logger.debug("Error resolving deck metadata for card %s: %s", card_id, e)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def verify_search_results(
    client: Any,
    results: list[SearchResult],
    *,
    evict_on_missing: bool = True,
) -> list[SearchResult]:
    """Filter search results to those the user can currently access.

    Deduplicates by ``(doc_id, doc_type)`` before verifying, so multiple
    chunks from the same document cost a single check. Verifiers run
    concurrently per doc_type (and within each doc_type, per id where that
    is cheaper than batching).

    When ``evict_on_missing=True``, points for documents that fail
    verification are deleted from Qdrant in-line. Eviction failures are
    logged but never propagated.

    Args:
        client: Authenticated NextcloudClient (must expose ``username``).
        results: SearchResult list from the algorithm layer (may include
            multiple chunks per document).
        evict_on_missing: Schedule lazy eviction for inaccessible docs.

    Returns:
        Filtered list preserving the original order.
    """
    if not results:
        return results

    user_id: str = client.username

    # Group unique (doc_id, doc_type) by doc_type so each verifier sees a
    # deduplicated batch.
    by_type: dict[str, set[int | str]] = {}
    for r in results:
        by_type.setdefault(r.doc_type, set()).add(r.id)

    # Run all type verifiers concurrently. Per-id failures are absorbed
    # inside each verifier; this outer task group only fans out per type.
    accessible_by_type: dict[str, set[int | str]] = {}

    async def run_verifier(doc_type: str, doc_ids: set[int | str]) -> None:
        verifier = _VERIFIERS.get(doc_type)
        if verifier is None:
            logger.warning(
                "No verifier registered for doc_type=%r; keeping %d result(s) unverified",
                doc_type,
                len(doc_ids),
            )
            accessible_by_type[doc_type] = doc_ids
            return
        try:
            accessible_by_type[doc_type] = await verifier(
                client, list(doc_ids), user_id
            )
        except Exception as e:
            # Verifier itself blew up (not per-id) — fail open.
            logger.error(
                "Verifier for doc_type=%s raised: %s; keeping all %d result(s) unverified",
                doc_type,
                e,
                len(doc_ids),
                exc_info=True,
            )
            accessible_by_type[doc_type] = doc_ids

    async with anyio.create_task_group() as tg:
        for doc_type, doc_ids in by_type.items():
            tg.start_soon(run_verifier, doc_type, doc_ids)

    # Compute (doc_id, doc_type) pairs that failed verification
    inaccessible: set[tuple[int | str, str]] = set()
    for doc_type, doc_ids in by_type.items():
        accessible = accessible_by_type.get(doc_type, doc_ids)
        for doc_id in doc_ids:
            if doc_id not in accessible:
                inaccessible.add((doc_id, doc_type))

    if inaccessible:
        logger.info(
            "Verification dropped %d inaccessible document(s): %s",
            len(inaccessible),
            sorted((str(d), t) for d, t in inaccessible),
        )

    # Filter results in-place-style, preserving order
    kept = [r for r in results if (r.id, r.doc_type) not in inaccessible]

    # Lazy eviction — fire and forget, but bounded inline so we don't lose
    # the user_id binding by escaping the task group.
    if evict_on_missing and inaccessible:

        async def evict(doc_id: int | str, doc_type: str) -> None:
            try:
                await delete_document_points(doc_id, doc_type, user_id)
            except Exception as e:
                logger.warning(
                    "Failed to evict %s_%s from Qdrant: %s", doc_type, doc_id, e
                )

        async with anyio.create_task_group() as tg:
            for doc_id, doc_type in inaccessible:
                tg.start_soon(evict, doc_id, doc_type)

    return kept
