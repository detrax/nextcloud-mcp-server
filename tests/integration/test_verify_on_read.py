"""Integration tests for verify-on-read access checks (ADR-019).

These tests exercise ``verify_search_results`` against a real Nextcloud
instance — the verification path's whole purpose is to consult Nextcloud as
the source of truth, so unit-level mocks don't catch protocol or status-code
mismatches between our verifier and the real API.

Qdrant is mocked out (``delete_document_points`` and the payload-resolution
helpers) so these tests don't require a running vector database. The unit
suite in ``tests/unit/search/test_verification.py`` covers the Qdrant-side
behaviour separately.
"""

import logging
import uuid

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.search import verification
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.verification import verify_search_results

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration


def _result_for_note(note_id: int) -> SearchResult:
    return SearchResult(
        id=note_id,
        doc_type="note",
        title=f"note_{note_id}",
        excerpt="...",
        score=0.9,
    )


async def test_verify_keeps_accessible_note(
    nc_client: NextcloudClient, temporary_note: dict, mocker
):
    """A note that exists in Nextcloud must be kept by verification."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    note_id = temporary_note["id"]
    results = [_result_for_note(note_id)]

    kept = await verify_search_results(nc_client, results)

    assert [r.id for r in kept] == [note_id]
    spy_evict.assert_not_awaited()


async def test_verify_drops_deleted_note_and_schedules_eviction(
    nc_client: NextcloudClient, mocker
):
    """The core ghost-record scenario.

    Create a note, delete it via the API (no webhook delivery), then run
    verification with a SearchResult still pointing at the gone-but-indexed
    document. verify-on-read must drop it and schedule eviction.
    """
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    # Create a note we'll delete to simulate a ghost record
    unique_suffix = uuid.uuid4().hex[:8]
    created = await nc_client.notes.create_note(
        title=f"verify-on-read ghost {unique_suffix}",
        content="This note will be deleted before verification runs.",
        category="VerifyOnReadTest",
    )
    note_id = created["id"]

    # Delete via API directly. In production a webhook *should* fire and
    # evict from Qdrant — but the whole point of ADR-019 is that we cannot
    # rely on this. Verification must catch the drift independently.
    await nc_client.notes.delete_note(note_id=note_id)

    # Confirm the note is really gone before running verification, so the
    # test fails fast if the API behaves unexpectedly.
    with pytest.raises(HTTPStatusError) as exc_info:
        await nc_client.notes.get_note(note_id)
    assert exc_info.value.response.status_code == 404

    kept = await verify_search_results(nc_client, [_result_for_note(note_id)])

    assert kept == [], "deleted note must not pass verification"
    spy_evict.assert_awaited_once_with(note_id, "note", nc_client.username)


async def test_verify_mixed_accessible_and_deleted(
    nc_client: NextcloudClient, temporary_note: dict, mocker
):
    """Verification must drop only the inaccessible result, keep the rest."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    # temporary_note stays alive for the duration of the test.
    accessible_id = temporary_note["id"]

    # Make a second note and immediately delete it to create a ghost id.
    unique_suffix = uuid.uuid4().hex[:8]
    ghost = await nc_client.notes.create_note(
        title=f"verify-on-read ghost mix {unique_suffix}",
        content="ghost",
        category="VerifyOnReadTest",
    )
    ghost_id = ghost["id"]
    await nc_client.notes.delete_note(note_id=ghost_id)

    results = [
        _result_for_note(accessible_id),
        _result_for_note(ghost_id),
    ]
    kept = await verify_search_results(nc_client, results)

    assert [r.id for r in kept] == [accessible_id]
    spy_evict.assert_awaited_once_with(ghost_id, "note", nc_client.username)


async def test_verify_dedupes_chunks_of_same_document(
    nc_client: NextcloudClient, temporary_note: dict, mocker
):
    """Multiple chunks of the same note must produce ONE Nextcloud round-trip."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    # Spy through to the real notes client to count round-trips
    real_get_note = nc_client.notes.get_note
    spy_get_note = mocker.AsyncMock(side_effect=real_get_note)
    mocker.patch.object(nc_client.notes, "get_note", spy_get_note)

    note_id = temporary_note["id"]
    # Three chunks of the same note (chunk_index varies)
    results = [
        SearchResult(
            id=note_id,
            doc_type="note",
            title="note",
            excerpt=f"chunk {i}",
            score=0.9 - i * 0.1,
            chunk_index=i,
        )
        for i in range(3)
    ]

    kept = await verify_search_results(nc_client, results)

    # All three chunks kept (they're all from the same accessible note)
    assert len(kept) == 3
    # ...but verification only fetched the note ONCE
    assert spy_get_note.await_count == 1
