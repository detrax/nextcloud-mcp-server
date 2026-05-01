"""Unit tests for verify-on-read (ADR-019)."""

from types import SimpleNamespace

import anyio
import httpx
import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.search import verification
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.verification import (
    _verify_deck_cards,
    _verify_files,
    _verify_news_items,
    _verify_notes,
    get_supported_doc_types,
    verify_search_results,
)
from nextcloud_mcp_server.vector.scanner import INDEXED_DOC_TYPES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sem(slots: int = 20) -> anyio.Semaphore:
    return anyio.Semaphore(slots)


def _make_result(
    doc_id: int | str,
    doc_type: str = "note",
    chunk_index: int = 0,
    score: float = 0.9,
    metadata: dict | None = None,
) -> SearchResult:
    return SearchResult(
        id=doc_id,
        doc_type=doc_type,
        title=f"{doc_type}_{doc_id}",
        excerpt="...",
        score=score,
        chunk_index=chunk_index,
        metadata=metadata,
    )


def _http_error(status_code: int) -> HTTPStatusError:
    request = httpx.Request("GET", "http://test.local/x")
    response = httpx.Response(status_code=status_code, request=request)
    return HTTPStatusError(f"{status_code}", request=request, response=response)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_supported_doc_types_covers_indexed_types():
    """ADR-019 CI guard: every doc_type indexed by the scanner has a verifier.

    `INDEXED_DOC_TYPES` is the single source of truth in `vector/scanner.py`;
    this test fails if a new indexed type is added without a registered
    verifier in `search/verification.py`.
    """
    assert get_supported_doc_types() >= INDEXED_DOC_TYPES


# ---------------------------------------------------------------------------
# Note verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_notes_200_keeps_all(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(return_value={"id": 1, "content": "x"})
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(
        client, [_make_result(1), _make_result(2), _make_result(3)], _sem()
    )

    assert result == {1, 2, 3}
    assert notes_client.get_note.await_count == 3


@pytest.mark.unit
async def test_verify_notes_404_drops(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(404))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(42)], _sem())

    assert result == set()


@pytest.mark.unit
async def test_verify_notes_403_drops(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(403))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(42)], _sem())

    assert result == set()


@pytest.mark.unit
async def test_verify_notes_transient_5xx_keeps(mocker):
    """Transient errors must NOT silently shrink results."""
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(503))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(42)], _sem())

    assert result == {42}


@pytest.mark.unit
async def test_verify_notes_unexpected_exception_keeps(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=RuntimeError("boom"))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(7)], _sem())

    assert result == {7}


@pytest.mark.unit
async def test_verify_notes_mixed_outcomes(mocker):
    """Mix of accessible, deleted, and transient — only deleted is dropped."""

    async def side_effect(note_id):
        if note_id == 1:
            return {"id": 1}
        if note_id == 2:
            raise _http_error(404)  # deleted
        if note_id == 3:
            raise _http_error(500)  # transient → keep
        raise AssertionError(f"unexpected id {note_id}")

    notes_client = SimpleNamespace(get_note=mocker.AsyncMock(side_effect=side_effect))
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(
        client, [_make_result(1), _make_result(2), _make_result(3)], _sem()
    )

    assert result == {1, 3}


# ---------------------------------------------------------------------------
# News batch verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_news_items_intersects_with_fetched_set(mocker):
    """News verifier does ONE fetch and intersects, regardless of how many ids."""
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(return_value=[{"id": 10}, {"id": 20}, {"id": 30}])
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(10, doc_type="news_item"),
            _make_result(20, doc_type="news_item"),
            _make_result(99, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == {10, 20}
    assert news_client.get_items.await_count == 1


@pytest.mark.unit
async def test_verify_news_items_api_404_drops_all(mocker):
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=_http_error(404))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(1, doc_type="news_item"),
            _make_result(2, doc_type="news_item"),
            _make_result(3, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == set()


@pytest.mark.unit
async def test_verify_news_items_transient_keeps_all(mocker):
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=_http_error(502))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(1, doc_type="news_item"),
            _make_result(2, doc_type="news_item"),
            _make_result(3, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == {1, 2, 3}


# ---------------------------------------------------------------------------
# File verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_files_uses_path_from_metadata(mocker):
    """File verifier reads path from SearchResult.metadata, no Qdrant round-trip."""
    webdav_client = SimpleNamespace(
        get_file_info=mocker.AsyncMock(return_value={"id": 100})
    )
    client = SimpleNamespace(webdav=webdav_client, username="alice")

    result = await _verify_files(
        client,
        [_make_result(100, doc_type="file", metadata={"path": "Documents/foo.txt"})],
        _sem(),
    )

    assert result == {100}
    webdav_client.get_file_info.assert_awaited_once_with("Documents/foo.txt")


@pytest.mark.unit
async def test_verify_files_404_via_get_file_info_drops(mocker):
    """get_file_info returns None on 404 — that's a definitive drop."""
    webdav_client = SimpleNamespace(get_file_info=mocker.AsyncMock(return_value=None))
    client = SimpleNamespace(webdav=webdav_client, username="alice")

    result = await _verify_files(
        client,
        [_make_result(123, doc_type="file", metadata={"path": "gone.txt"})],
        _sem(),
    )

    assert result == set()


@pytest.mark.unit
async def test_verify_files_missing_path_metadata_keeps_unverified(mocker):
    """Without a path in metadata we cannot verify — fail open, don't drop."""
    webdav_client = SimpleNamespace(
        get_file_info=mocker.AsyncMock(side_effect=AssertionError("must not be called"))
    )
    client = SimpleNamespace(webdav=webdav_client, username="alice")

    # No metadata at all
    result = await _verify_files(client, [_make_result(555, doc_type="file")], _sem())
    assert result == {555}
    webdav_client.get_file_info.assert_not_awaited()

    # Metadata present but no "path" key
    result = await _verify_files(
        client, [_make_result(556, doc_type="file", metadata={})], _sem()
    )
    assert result == {556}
    webdav_client.get_file_info.assert_not_awaited()


@pytest.mark.unit
async def test_verify_files_transient_5xx_keeps(mocker):
    webdav_client = SimpleNamespace(
        get_file_info=mocker.AsyncMock(side_effect=_http_error(503))
    )
    client = SimpleNamespace(webdav=webdav_client, username="alice")

    result = await _verify_files(
        client,
        [_make_result(7, doc_type="file", metadata={"path": "x.txt"})],
        _sem(),
    )

    assert result == {7}


# ---------------------------------------------------------------------------
# Deck card verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_deck_cards_uses_metadata_fast_path(mocker):
    """Deck verifier reads board_id+stack_id from metadata, no Qdrant round-trip."""
    deck_client = SimpleNamespace(get_card=mocker.AsyncMock(return_value=object()))
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )

    assert result == {42}
    deck_client.get_card.assert_awaited_once_with(board_id=1, stack_id=2, card_id=42)


@pytest.mark.unit
async def test_verify_deck_cards_403_drops(mocker):
    """Board unshared with user → 403 from get_card → drop."""
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=_http_error(403))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )

    assert result == set()


@pytest.mark.unit
async def test_verify_deck_cards_missing_metadata_keeps_unverified(mocker):
    """Legacy data without board_id/stack_id → keep, do NOT iterate or call API."""
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=AssertionError("must not be called"))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    # No metadata at all
    result = await _verify_deck_cards(
        client, [_make_result(42, doc_type="deck_card")], _sem()
    )
    assert result == {42}

    # Only board_id (stack_id missing)
    result = await _verify_deck_cards(
        client,
        [_make_result(43, doc_type="deck_card", metadata={"board_id": 1})],
        _sem(),
    )
    assert result == {43}

    # Only stack_id (board_id missing)
    result = await _verify_deck_cards(
        client,
        [_make_result(44, doc_type="deck_card", metadata={"stack_id": 2})],
        _sem(),
    )
    assert result == {44}

    deck_client.get_card.assert_not_awaited()


# ---------------------------------------------------------------------------
# Top-level verify_search_results
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_search_results_empty_input_passthrough():
    client = SimpleNamespace(username="alice")
    assert await verify_search_results(client, []) == []


@pytest.mark.unit
async def test_verify_search_results_dedupes_chunks_per_document(mocker):
    """Two chunks of the same note → ONE call to the underlying verifier."""
    spy = mocker.AsyncMock(return_value={1})
    mocker.patch.dict(verification._VERIFIERS, {"note": spy}, clear=False)
    mocker.patch.object(verification, "delete_document_points", mocker.AsyncMock())

    results = [
        _make_result(1, doc_type="note", chunk_index=0),
        _make_result(1, doc_type="note", chunk_index=1),
        _make_result(1, doc_type="note", chunk_index=2),
    ]
    client = SimpleNamespace(username="alice")

    kept = await verify_search_results(client, results)

    assert len(kept) == 3  # all kept, all reference the same accessible doc
    spy.assert_awaited_once()
    # Verifier received exactly one SearchResult (the deduplicated representative)
    args, _kwargs = spy.call_args
    assert len(args[1]) == 1
    assert args[1][0].id == 1
    # And a semaphore as the third arg
    assert isinstance(args[2], anyio.Semaphore)


@pytest.mark.unit
async def test_verify_search_results_drops_inaccessible_and_evicts(mocker):
    """Inline-fallback path (no eviction_task_group): evict completes before return."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    # Verifier reports note 1 accessible, note 99 not
    note_verifier = mocker.AsyncMock(return_value={1})
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    results = [
        _make_result(1, doc_type="note"),
        _make_result(99, doc_type="note"),
    ]
    client = SimpleNamespace(username="alice")

    kept = await verify_search_results(client, results)

    assert [r.id for r in kept] == [1]
    spy_evict.assert_awaited_once_with(99, "note", "alice")


@pytest.mark.unit
async def test_verify_search_results_fire_and_forget_eviction(mocker):
    """When eviction_task_group is provided, eviction does not block the response.

    Validates the ADR-019 design: spawn evict() on the lifespan-owned task
    group via start_soon so the search response returns immediately. The
    eviction still runs (verified after the task group exits).
    """
    eviction_started = anyio.Event()
    eviction_may_complete = anyio.Event()
    eviction_completed = anyio.Event()

    async def slow_delete(doc_id, doc_type, user_id):
        eviction_started.set()
        await eviction_may_complete.wait()
        eviction_completed.set()

    mocker.patch.object(
        verification,
        "delete_document_points",
        mocker.AsyncMock(side_effect=slow_delete),
    )

    note_verifier = mocker.AsyncMock(return_value=set())  # both inaccessible
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    results = [_make_result(99, doc_type="note")]
    client = SimpleNamespace(username="alice")

    async with anyio.create_task_group() as tg:
        kept = await verify_search_results(client, results, eviction_task_group=tg)
        # 1. Search response was returned …
        assert kept == []
        # 2. … even though eviction has started but not finished.
        await eviction_started.wait()
        assert not eviction_completed.is_set()
        # 3. Now allow eviction to complete; the task group exit awaits it.
        eviction_may_complete.set()

    # After the task group exits, the eviction must have run.
    assert eviction_completed.is_set()


@pytest.mark.unit
async def test_verify_search_results_no_eviction_when_disabled(mocker):
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    note_verifier = mocker.AsyncMock(return_value=set())  # all inaccessible
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    results = [_make_result(7, doc_type="note")]
    client = SimpleNamespace(username="alice")

    kept = await verify_search_results(client, results, evict_on_missing=False)

    assert kept == []
    spy_evict.assert_not_awaited()


@pytest.mark.unit
async def test_verify_search_results_unknown_doc_type_passes_through(mocker, caplog):
    """No verifier registered for doc_type → keep, log a warning."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)
    # Ensure no verifier for "calendar"
    mocker.patch.dict(
        verification._VERIFIERS,
        {k: v for k, v in verification._VERIFIERS.items() if k != "calendar"},
        clear=True,
    )

    results = [_make_result(1, doc_type="calendar")]
    client = SimpleNamespace(username="alice")

    kept = await verify_search_results(client, results)

    assert len(kept) == 1
    spy_evict.assert_not_awaited()


@pytest.mark.unit
async def test_verify_search_results_verifier_blowup_keeps_all(mocker):
    """A verifier raising an unexpected exception must not silently drop results."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)
    note_verifier = mocker.AsyncMock(side_effect=RuntimeError("qdrant down"))
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    results = [
        _make_result(1, doc_type="note"),
        _make_result(2, doc_type="note"),
    ]
    client = SimpleNamespace(username="alice")

    kept = await verify_search_results(client, results)

    assert [r.id for r in kept] == [1, 2]
    spy_evict.assert_not_awaited()


@pytest.mark.unit
async def test_verify_search_results_preserves_order(mocker):
    """Order of original results must be preserved after filtering."""
    note_verifier = mocker.AsyncMock(return_value={1, 3})
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)
    mocker.patch.object(verification, "delete_document_points", mocker.AsyncMock())

    results = [
        _make_result(1, doc_type="note", score=0.9),
        _make_result(2, doc_type="note", score=0.8),
        _make_result(3, doc_type="note", score=0.7),
    ]
    client = SimpleNamespace(username="alice")

    kept = await verify_search_results(client, results)

    assert [r.id for r in kept] == [1, 3]


@pytest.mark.unit
async def test_verify_search_results_eviction_failure_does_not_propagate(mocker):
    """Eviction failures are logged, never raised — must not break search."""
    mocker.patch.object(
        verification,
        "delete_document_points",
        mocker.AsyncMock(side_effect=RuntimeError("qdrant down")),
    )
    note_verifier = mocker.AsyncMock(return_value=set())
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    client = SimpleNamespace(username="alice")
    # Should NOT raise
    kept = await verify_search_results(client, [_make_result(1, doc_type="note")])
    assert kept == []


@pytest.mark.unit
async def test_verify_search_results_dispatches_per_doc_type_concurrently(mocker):
    """Mixed doc_types must be routed to their respective verifiers."""
    note_verifier = mocker.AsyncMock(return_value={1})
    file_verifier = mocker.AsyncMock(return_value={500})
    mocker.patch.dict(
        verification._VERIFIERS,
        {"note": note_verifier, "file": file_verifier},
        clear=False,
    )
    mocker.patch.object(verification, "delete_document_points", mocker.AsyncMock())

    results = [
        _make_result(1, doc_type="note"),
        _make_result(500, doc_type="file", metadata={"path": "a.txt"}),
        _make_result(999, doc_type="file", metadata={"path": "b.txt"}),  # to be dropped
    ]
    client = SimpleNamespace(username="alice")

    kept = await verify_search_results(client, results)

    assert {(r.id, r.doc_type) for r in kept} == {(1, "note"), (500, "file")}
    note_verifier.assert_awaited_once()
    file_verifier.assert_awaited_once()


@pytest.mark.unit
async def test_verify_search_results_passes_semaphore_to_verifier(mocker):
    """The dispatcher must construct a Semaphore and pass it to verifiers."""
    captured: dict[str, anyio.Semaphore] = {}

    async def verifier(client, results, semaphore):
        captured["sem"] = semaphore
        return {r.id for r in results}

    mocker.patch.dict(verification._VERIFIERS, {"note": verifier}, clear=False)
    mocker.patch.object(verification, "delete_document_points", mocker.AsyncMock())

    client = SimpleNamespace(username="alice")
    await verify_search_results(client, [_make_result(1)], max_concurrent=5)

    assert isinstance(captured["sem"], anyio.Semaphore)
