"""Unit tests for verify-on-read (ADR-019)."""

from types import SimpleNamespace

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    doc_id: int,
    doc_type: str = "note",
    chunk_index: int = 0,
    score: float = 0.9,
) -> SearchResult:
    return SearchResult(
        id=doc_id,
        doc_type=doc_type,
        title=f"{doc_type}_{doc_id}",
        excerpt="...",
        score=score,
        chunk_index=chunk_index,
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
    """ADR-019 implementation checklist: every indexed doc_type has a verifier.

    Indexed types are defined in vector/scanner.py and vector/processor.py:
    note, file, deck_card, news_item.
    """
    expected = {"note", "file", "deck_card", "news_item"}
    assert get_supported_doc_types() >= expected


# ---------------------------------------------------------------------------
# Note verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_notes_200_keeps_all(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(return_value={"id": 1, "content": "x"})
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [1, 2, 3], "alice")

    assert result == {1, 2, 3}
    assert notes_client.get_note.await_count == 3


@pytest.mark.unit
async def test_verify_notes_404_drops(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(404))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [42], "alice")

    assert result == set()


@pytest.mark.unit
async def test_verify_notes_403_drops(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(403))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [42], "alice")

    assert result == set()


@pytest.mark.unit
async def test_verify_notes_transient_5xx_keeps(mocker):
    """Transient errors must NOT silently shrink results."""
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(503))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [42], "alice")

    assert result == {42}


@pytest.mark.unit
async def test_verify_notes_unexpected_exception_keeps(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=RuntimeError("boom"))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [7], "alice")

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

    result = await _verify_notes(client, [1, 2, 3], "alice")

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

    result = await _verify_news_items(client, [10, 20, 99], "alice")

    assert result == {10, 20}
    assert news_client.get_items.await_count == 1


@pytest.mark.unit
async def test_verify_news_items_api_404_drops_all(mocker):
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=_http_error(404))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(client, [1, 2, 3], "alice")

    assert result == set()


@pytest.mark.unit
async def test_verify_news_items_transient_keeps_all(mocker):
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=_http_error(502))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(client, [1, 2, 3], "alice")

    assert result == {1, 2, 3}


# ---------------------------------------------------------------------------
# File verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_files_uses_propfind_when_path_resolves(mocker):
    mocker.patch.object(
        verification, "_resolve_file_path", return_value="Documents/foo.txt"
    )
    webdav_client = SimpleNamespace(
        get_file_info=mocker.AsyncMock(return_value={"id": 100})
    )
    client = SimpleNamespace(webdav=webdav_client, username="alice")

    result = await _verify_files(client, [100], "alice")

    assert result == {100}
    webdav_client.get_file_info.assert_awaited_once_with("Documents/foo.txt")


@pytest.mark.unit
async def test_verify_files_404_via_get_file_info_drops(mocker):
    """get_file_info returns None on 404 — that's a definitive drop."""
    mocker.patch.object(verification, "_resolve_file_path", return_value="gone.txt")
    webdav_client = SimpleNamespace(get_file_info=mocker.AsyncMock(return_value=None))
    client = SimpleNamespace(webdav=webdav_client, username="alice")

    result = await _verify_files(client, [123], "alice")

    assert result == set()


@pytest.mark.unit
async def test_verify_files_missing_payload_keeps_unverified(mocker):
    """Without a file_path we cannot verify — fail open, don't drop."""
    mocker.patch.object(verification, "_resolve_file_path", return_value=None)
    webdav_client = SimpleNamespace(get_file_info=mocker.AsyncMock(return_value=None))
    client = SimpleNamespace(webdav=webdav_client, username="alice")

    result = await _verify_files(client, [555], "alice")

    assert result == {555}
    webdav_client.get_file_info.assert_not_awaited()


# ---------------------------------------------------------------------------
# Deck card verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_deck_cards_uses_metadata_fast_path(mocker):
    mocker.patch.object(
        verification,
        "_resolve_deck_metadata",
        return_value={"board_id": 1, "stack_id": 2},
    )
    deck_client = SimpleNamespace(get_card=mocker.AsyncMock(return_value=object()))
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(client, [42], "alice")

    assert result == {42}
    deck_client.get_card.assert_awaited_once_with(board_id=1, stack_id=2, card_id=42)


@pytest.mark.unit
async def test_verify_deck_cards_403_drops(mocker):
    """Board unshared with user → 403 from get_card → drop."""
    mocker.patch.object(
        verification,
        "_resolve_deck_metadata",
        return_value={"board_id": 1, "stack_id": 2},
    )
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=_http_error(403))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(client, [42], "alice")

    assert result == set()


@pytest.mark.unit
async def test_verify_deck_cards_no_metadata_skips_verification(mocker):
    """Legacy data without board_id/stack_id payload → keep, do NOT iterate."""
    mocker.patch.object(verification, "_resolve_deck_metadata", return_value=None)
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=AssertionError("must not be called"))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(client, [42], "alice")

    assert result == {42}
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
    # Verifier received the single deduplicated id, not three copies
    args, _kwargs = spy.call_args
    assert args[1] == [1]


@pytest.mark.unit
async def test_verify_search_results_drops_inaccessible_and_evicts(mocker):
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
        _make_result(500, doc_type="file"),
        _make_result(999, doc_type="file"),  # to be dropped
    ]
    client = SimpleNamespace(username="alice")

    kept = await verify_search_results(client, results)

    assert {(r.id, r.doc_type) for r in kept} == {(1, "note"), (500, "file")}
    note_verifier.assert_awaited_once()
    file_verifier.assert_awaited_once()
