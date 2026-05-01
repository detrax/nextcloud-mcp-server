"""Verify-on-read access checks for semantic search results (ADR-019).

The vector index is a recall layer; Nextcloud is the source of truth for
access. This module filters search results by checking each unique document
against Nextcloud at query time, dropping any that the user can no longer
access (deleted, unshared, etc.) and lazily evicting them from the index.

Per-doc_type verifiers are registered in ``_VERIFIERS``. Each takes the
authenticated client, the (deduplicated) list of ``SearchResult``s for that
doc_type, and a shared concurrency semaphore. They return the subset of
``doc_id`` values that are currently accessible. Verifiers read whatever
metadata they need (file path, deck card board/stack ids) directly from the
SearchResult — these fields are populated at index-time and propagated by
the algorithm layer (see ``search/bm25_hybrid.py`` and ``search/semantic.py``)
so verification adds zero extra Qdrant round-trips.

Concurrency is bounded by a shared semaphore (default 20) so a large search
result page (or a multi-doc_type query) cannot exhaust the httpx connection
pool or trigger Nextcloud rate limiting. The 20-slot default matches the
context-expansion convention in ``server/semantic.py``.

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
from anyio.abc import TaskGroup
from httpx import HTTPStatusError

from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.vector.eviction import delete_document_points

logger = logging.getLogger(__name__)


# Default cap on concurrent verification round-trips against Nextcloud. Matches
# the convention in ``server/semantic.py`` for context-expansion fan-out.
DEFAULT_VERIFICATION_CONCURRENCY = 20


BatchVerifier = Callable[
    [Any, list[SearchResult], anyio.Semaphore], Awaitable[set[int | str]]
]
"""(client, results, semaphore) -> set of doc_ids accessible to the user."""


# ---------------------------------------------------------------------------
# Per-doc-type verifiers
# ---------------------------------------------------------------------------


def _is_definitive_404_or_403(exc: BaseException) -> bool:
    """Return True if exc indicates the document is definitively inaccessible."""
    if isinstance(exc, HTTPStatusError):
        return exc.response.status_code in (403, 404)
    return False


async def _verify_notes(
    client: Any, results: list[SearchResult], semaphore: anyio.Semaphore
) -> set[int | str]:
    accessible: set[int | str] = set()

    async def check(result: SearchResult) -> None:
        async with semaphore:
            doc_id = result.id
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
        for r in results:
            tg.start_soon(check, r)

    return accessible


async def _verify_files(
    client: Any, results: list[SearchResult], semaphore: anyio.Semaphore
) -> set[int | str]:
    accessible: set[int | str] = set()

    async def check(result: SearchResult) -> None:
        doc_id = result.id
        # file_path is propagated from the Qdrant payload by the algorithm
        # layer (bm25_hybrid.py / semantic.py). No extra Qdrant round-trip.
        file_path = (result.metadata or {}).get("path")
        if not file_path:
            # Cannot verify without a path; treat as accessible to avoid
            # silently dropping legitimate results when payload is missing
            # (legacy data, or a future doc_type that doesn't propagate path).
            logger.warning(
                "No file path in metadata for file_id %s; keeping result "
                "(verification skipped)",
                doc_id,
            )
            accessible.add(doc_id)
            return

        async with semaphore:
            try:
                info = await client.webdav.get_file_info(file_path)
                if info is None:
                    # Contract: WebDAVClient.get_file_info returns None on 404
                    # and raises HTTPStatusError on 403/5xx/network. If that
                    # contract changes (e.g. a future refactor that raises 404
                    # like other client methods), the `except HTTPStatusError`
                    # block below already handles it via _is_definitive_404_or_403.
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
        for r in results:
            tg.start_soon(check, r)

    return accessible


async def _verify_deck_cards(
    client: Any, results: list[SearchResult], semaphore: anyio.Semaphore
) -> set[int | str]:
    accessible: set[int | str] = set()

    async def check(result: SearchResult) -> None:
        doc_id = result.id
        # board_id and stack_id are propagated from the Qdrant payload by the
        # algorithm layer. No extra Qdrant round-trip.
        meta = result.metadata or {}
        board_id = meta.get("board_id")
        stack_id = meta.get("stack_id")
        if board_id is None or stack_id is None:
            # Without metadata we cannot run the cheap fast-path. Per ADR-019
            # we deliberately do NOT fall back to O(boards × stacks) iteration
            # in the search hot path; treat as accessible.
            logger.warning(
                "Incomplete deck metadata for card %s (board_id=%s, stack_id=%s); "
                "keeping result (verification skipped, legacy data)",
                doc_id,
                board_id,
                stack_id,
            )
            accessible.add(doc_id)
            return

        async with semaphore:
            try:
                await client.deck.get_card(
                    board_id=int(board_id),
                    stack_id=int(stack_id),
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
        for r in results:
            tg.start_soon(check, r)

    return accessible


async def _verify_news_items(
    client: Any, results: list[SearchResult], semaphore: anyio.Semaphore
) -> set[int | str]:
    """Batch-verify news items with a single fetch.

    The Nextcloud News API has no per-item endpoint, so ``news.get_item`` is
    implemented as a fetch-all + filter — which would be O(N × all_items) if
    called per id. Instead we fetch once and intersect. The semaphore is
    accepted for signature symmetry but not heavily used (one round-trip total).
    """
    doc_ids = [r.id for r in results]

    async with semaphore:
        try:
            items = await client.news.get_items(batch_size=-1, get_read=True)
        except HTTPStatusError as e:
            # If the News API itself is gone (app disabled, user lost access),
            # treat *all* requested items as inaccessible. Eviction will reclaim.
            if _is_definitive_404_or_403(e):
                logger.info(
                    "News API returned %s for user %s; treating all %d news_items as inaccessible",
                    e.response.status_code,
                    client.username,
                    len(doc_ids),
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

    # Cast safely: a non-numeric id from the API or in our doc_ids would
    # otherwise raise ValueError after the semaphore block exits and surface
    # as a verifier crash. Treat as transient (fail open) instead.
    try:
        present_ids = {
            int(item.get("id")) for item in items if item.get("id") is not None
        }
        # Map back to the original doc_id types (caller may pass ints or strs).
        accessible: set[int | str] = set()
        for d in doc_ids:
            if int(d) in present_ids:
                accessible.add(d)
        return accessible
    except (TypeError, ValueError) as e:
        logger.warning(
            "Non-numeric id while verifying news items (sample=%r, doc_ids=%r): %s; keeping all results",
            items[:3] if items else items,
            doc_ids,
            e,
        )
        return set(doc_ids)


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
# Public entry point
# ---------------------------------------------------------------------------


async def verify_search_results(
    client: Any,
    results: list[SearchResult],
    *,
    evict_on_missing: bool = True,
    max_concurrent: int = DEFAULT_VERIFICATION_CONCURRENCY,
    eviction_task_group: TaskGroup | None = None,
) -> list[SearchResult]:
    """Filter search results to those the user can currently access.

    Deduplicates by ``(doc_id, doc_type)`` before verifying, so multiple
    chunks from the same document cost a single check. Verifiers run
    concurrently per doc_type and concurrently per id within each verifier,
    bounded by a shared semaphore (``max_concurrent``).

    When ``evict_on_missing=True``, points for documents that fail verification
    are deleted from Qdrant. If ``eviction_task_group`` is provided (the
    lifespan-owned task group from ``app.py::VectorSyncState``), eviction is
    fire-and-forget — the search response returns immediately and Qdrant
    deletes happen in the background. If no task group is provided (unit
    tests, modes without vector sync), eviction falls back to running inline
    in a local task group. Eviction failures are logged but never propagated.

    Args:
        client: Authenticated NextcloudClient (must expose ``username``).
        results: SearchResult list from the algorithm layer (may include
            multiple chunks per document).
        evict_on_missing: Schedule lazy eviction for inaccessible docs.
        max_concurrent: Cap on concurrent verification round-trips against
            Nextcloud. Defaults to ``DEFAULT_VERIFICATION_CONCURRENCY``.
        eviction_task_group: Optional long-lived task group on which to
            spawn fire-and-forget eviction. Pass
            ``ctx.request_context.lifespan_context.eviction_task_group``
            from FastMCP tools.

    Returns:
        Filtered list preserving the original order.
    """
    if not results:
        return results

    user_id: str = client.username

    # Group unique (doc_id, doc_type) by doc_type so each verifier sees a
    # deduplicated batch. We pick one SearchResult per (id, doc_type) to carry
    # metadata (path, board_id/stack_id) into the verifier — chunks of the
    # same document share these fields, so any chunk works.
    by_type: dict[str, dict[int | str, SearchResult]] = {}
    for r in results:
        by_type.setdefault(r.doc_type, {}).setdefault(r.id, r)

    # Shared semaphore bounds total Nextcloud round-trips across all
    # per-id verifiers. Without it, a 50-result mostly-notes page could fan
    # out 50 concurrent get_note calls and exhaust the connection pool.
    semaphore = anyio.Semaphore(max_concurrent)

    accessible_by_type: dict[str, set[int | str]] = {}

    async def run_verifier(doc_type: str, unique_results: list[SearchResult]) -> None:
        verifier = _VERIFIERS.get(doc_type)
        if verifier is None:
            logger.warning(
                "No verifier registered for doc_type=%r; keeping %d result(s) unverified",
                doc_type,
                len(unique_results),
            )
            accessible_by_type[doc_type] = {r.id for r in unique_results}
            return
        try:
            accessible_by_type[doc_type] = await verifier(
                client, unique_results, semaphore
            )
        except Exception as e:
            # Verifier itself blew up (not per-id) — fail open.
            logger.error(
                "Verifier for doc_type=%s raised: %s; keeping all %d result(s) unverified",
                doc_type,
                e,
                len(unique_results),
                exc_info=True,
            )
            accessible_by_type[doc_type] = {r.id for r in unique_results}

    async with anyio.create_task_group() as tg:
        for doc_type, id_to_result in by_type.items():
            tg.start_soon(run_verifier, doc_type, list(id_to_result.values()))

    # Compute (doc_id, doc_type) pairs that failed verification
    inaccessible: set[tuple[int | str, str]] = set()
    for doc_type, id_to_result in by_type.items():
        accessible = accessible_by_type.get(doc_type, set(id_to_result.keys()))
        for doc_id in id_to_result.keys():
            if doc_id not in accessible:
                inaccessible.add((doc_id, doc_type))

    if inaccessible:
        logger.info(
            "Verification dropped %d inaccessible document(s): %s",
            len(inaccessible),
            sorted((str(d), t) for d, t in inaccessible),
        )

    # Filter results, preserving order. All chunks of an inaccessible document
    # are dropped together (dedup happened before verification, but the result
    # list still contains all chunks).
    kept = [r for r in results if (r.id, r.doc_type) not in inaccessible]

    # Lazy eviction.
    #
    # Preferred path: spawn evict() on the lifespan-owned task group via
    # `start_soon`, which returns immediately — the search response is not
    # blocked on Qdrant deletes. If the server is shutting down, the task
    # group is cleared back to None (see app.py) and we fall through to the
    # inline path. Cancellation mid-eviction is fine: the next query will
    # re-verify and re-attempt (self-healing per ADR-019).
    #
    # Fallback path: when no task group is supplied (unit tests, deployment
    # modes without vector sync), run eviction inline in a local task group.
    # This preserves prior behaviour for tests that rely on eviction being
    # complete by the time `verify_search_results` returns.
    if evict_on_missing and inaccessible:

        async def evict(doc_id: int | str, doc_type: str) -> None:
            try:
                await delete_document_points(doc_id, doc_type, user_id)
            except Exception as e:
                logger.warning(
                    "Failed to evict %s_%s from Qdrant: %s", doc_type, doc_id, e
                )

        if eviction_task_group is not None:
            for doc_id, doc_type in inaccessible:
                eviction_task_group.start_soon(evict, doc_id, doc_type)
        else:
            async with anyio.create_task_group() as tg:
                for doc_id, doc_type in inaccessible:
                    tg.start_soon(evict, doc_id, doc_type)

    return kept
