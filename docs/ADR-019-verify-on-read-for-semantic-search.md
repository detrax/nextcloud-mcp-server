# ADR-019: Verify-on-Read for Semantic Search Results

**Status**: Proposed
**Date**: 2026-05-01
**Depends On**: ADR-007 (Background Vector Sync), ADR-010 (Webhook-Based Vector Sync)

## Context

The vector index in Qdrant is a *recall layer*, not the source of truth. Authoritative state for every indexed document — whether a note exists, whether a file is still shared with the user, whether a deck card is on a board the user can read — lives in Nextcloud, not in our index. Whenever those two views drift, semantic search returns **ghost records**: results that point to documents the user can no longer access (or that no longer exist at all).

### How drift happens

Two mechanisms keep Qdrant in sync with Nextcloud, and both have non-zero latency:

1. **Webhook delivery (ADR-010)**. Nextcloud's `webhook_listeners` app dispatches change notifications via background jobs. The default `cron` job runs every 5 minutes, so even a healthy webhook pipeline opens a 0–5 minute window where deletions/unshares are not yet reflected in the index. Operators with dedicated webhook workers can shrink this, but most production deployments stay on the default cadence.

2. **Periodic scanner (ADR-007)**. The fallback reconciliation scan runs on `vector_sync_scan_interval`. The dev default is 60 seconds, but ADR-010 explicitly recommends raising this to 1 hour or more in production once webhooks are in place, since the scanner exists primarily to recover from missed events. Large deployments may run it once per day.

Beyond cadence, several failure modes cause webhooks to be missed entirely:

- The MCP server is down or unreachable when the webhook fires (Nextcloud does not durably retry).
- Sharing changes (revoking a share, leaving a group) do not always emit file events that match what we registered for.
- Application-level deletions in apps without rich event support (older Deck versions, custom Tables flows) bypass the file-event hooks.

In all of these cases, the document remains in Qdrant until the next periodic scan reconciles it — which may be hours away. Until then, `nc_semantic_search` happily returns the stale entry.

### Why this matters more for semantic search than keyword search

A keyword search via the Notes API is naturally bounded by what the API returns: deleted notes are not in the result set. The vector index is a separate store with its own lifecycle. The further we extend semantic search across apps (notes, files, deck cards, news items today; calendar, contacts, tables, cookbook tomorrow), the more divergent surfaces we expose to drift. Every new doc_type is another path where a webhook can be missed and another type of "ghost" can leak into results.

The risk is not just a confusing UX. For RAG flows like `nc_semantic_search_answer` (ADR-008), a stale result means the LLM is asked to synthesize an answer over content the user no longer has access to — a privacy boundary violation, not just a relevance bug.

### Current state of verification

Verification today is ad-hoc and inconsistent:

| Surface | Verifies? | Mechanism |
|---|---|---|
| `nc_semantic_search` | No | Returns raw Qdrant results. The docstring at `search/semantic.py:52` and `search/bm25_hybrid.py:75` references a `verify_search_results()` helper that was never implemented. |
| `nc_semantic_search_answer` | Partially | `server/semantic.py:431-448` fetches `notes.get_note(id)` and drops on exception — but only for `doc_type == "note"`. Files, news items, and deck cards fall through to the `else` branch (`server/semantic.py:449`) and are returned with their excerpt unverified. |
| `get_chunk_with_context` (when `include_context=True`) | Implicitly, all types | `search/context.py::_fetch_document_text` re-fetches the document; on failure the *context expansion* is skipped but the original (unverified) result is still returned (`server/semantic.py:246-251`). |

There is no single point where the system asks: "is this document still accessible to this user, right now?"

### The four indexed doc types

The vector pipeline (`vector/scanner.py`, `vector/processor.py`) currently indexes four types, each with its own access-check shape:

| doc_type | Cheapest authoritative check | Notes |
|---|---|---|
| `note` | `notes.get_note(id)` — single REST call, 404 on deletion | Per-user store; access is binary (yours or not). |
| `news_item` | `news.get_item(id)` — single REST call | Per-user feeds; clean 404 semantics. |
| `file` | WebDAV `PROPFIND` with `Depth: 0` on `file_path` (already stored in Qdrant payload, see `server/semantic.py:161`) | `read_file()` works but downloads the body — too heavy for a verification check. PROPFIND is the WebDAV equivalent of HEAD. Catches both deletes and unshares. |
| `deck_card` | `deck.get_card(board_id, stack_id, card_id)` using metadata cached in Qdrant (`search/context.py::_get_deck_metadata_from_qdrant`) | Fallback iteration through all boards/stacks (used by context expansion) is O(boards × stacks) and far too expensive to run on every query. |

All four are query-time-cheap **if** we (a) deduplicate per-document before checking and (b) run checks concurrently.

## Decision

Implement **verify-on-read** as the authoritative access gate for semantic search. The vector index decides *what might be relevant*; Nextcloud decides *what the user can see*. We will:

1. Introduce a single `nextcloud_mcp_server/search/verification.py` module exposing `verify_search_results(client, results) -> list[SearchResult]`.
2. Wire it into both `nc_semantic_search` and `nc_semantic_search_answer` as the final step before results leave the server, replacing the ad-hoc note-only verification in the answer tool.
3. Dispatch per `doc_type` to a registry of verifiers using the cheapest authoritative check for each type.
4. Lazily evict from Qdrant when verification reveals a definitively-gone document, so the next query for the same content does not re-pay the verification cost.

The vector index becomes a **hint**, not a contract. We never trust it for access decisions.

## Implementation

### Module shape

```python
# nextcloud_mcp_server/search/verification.py

from typing import Awaitable, Callable, Protocol
import anyio
import httpx

from nextcloud_mcp_server.search.algorithms import SearchResult

# A verifier returns True if the document is currently accessible to the user.
# It MUST distinguish definitive 404/403 (return False) from transient errors
# (raise — caller will keep the result and log a warning).
Verifier = Callable[["NextcloudClientProtocol", int | str], Awaitable[bool]]


async def verify_search_results(
    client: "NextcloudClientProtocol",
    results: list[SearchResult],
    *,
    max_concurrent: int = 20,
    evict_on_missing: bool = True,
) -> list[SearchResult]:
    """Filter search results to those the user can currently access.

    Deduplicates by (doc_id, doc_type) before verifying, so multiple chunks
    from the same document cost a single check. Verifies concurrently under
    a semaphore. Drops results whose verifier returned False; keeps results
    whose verifier raised (transient failure should not produce silent gaps).

    When evict_on_missing=True, schedules async deletion of the Qdrant points
    for the missing document(s) so subsequent queries don't re-pay the cost.
    """
```

### Verifier registry

```python
_VERIFIERS: dict[str, Verifier] = {
    "note": _verify_note,
    "news_item": _verify_news_item,
    "file": _verify_file,
    "deck_card": _verify_deck_card,
}
```

Each verifier follows the same pattern:

```python
async def _verify_note(client, doc_id: int) -> bool:
    try:
        await client.notes.get_note(int(doc_id))
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (403, 404):
            return False
        raise  # transient — caller keeps the result
```

For `file`, use `webdav` PROPFIND (`Depth: 0`) on the `file_path` from the Qdrant payload, not `read_file()`. For `deck_card`, use the cached `(board_id, stack_id)` from `_get_deck_metadata_from_qdrant`; if metadata is absent, treat the result as accessible and log — we will not run the iteration fallback in the hot path.

### Deduplication

A 10-result page typically references 3–4 unique documents because of chunking. Verify each unique `(doc_id, doc_type)` once, then propagate the verdict to all chunks of that document:

```python
unique_keys = {(r.id, r.doc_type) for r in results}
verdicts = {key: await _verify(client, key) for key in unique_keys}  # via task group
return [r for r in results if verdicts.get((r.id, r.doc_type), True)]
```

A failed verification (raised exception) maps to "keep" — we do not want a flaky network blip to silently shrink results.

### Lazy eviction

When a verdict is `False`, queue a Qdrant delete for all points matching `(user_id, doc_id, doc_type)`. The plumbing already exists in `vector/placeholder.py::delete_placeholder_point` (which uses a filter-based delete); we need a sibling `delete_document_points` that omits the `is_placeholder` filter, so it removes real chunks too.

Eviction is fire-and-forget from the verification path — wrap it in a background task group on the lifespan context to avoid blocking the response. If eviction fails, the next query will simply re-verify and re-attempt; this is self-healing.

### Wiring

In `server/semantic.py::nc_semantic_search`, after the existing dedup and `[:limit]` slice, but **before** context expansion (which is expensive and pointless on inaccessible results):

```python
search_results = all_results[:limit * 2]  # fetch extra to absorb evictions
search_results = await verify_search_results(client, search_results)
search_results = search_results[:limit]
```

Note the over-fetch: verification can shrink the page, so we ask for `limit * 2` candidates and trim *after* verification. This preserves the user's requested page size when ghosts are present without paying for full re-search.

In `server/semantic.py::nc_semantic_search_answer`, replace the per-type `if result.doc_type == "note"` branch (lines 428-453) with a call to `verify_search_results` followed by the existing full-content fetch (which can stay note-specific, since only notes use full content; the rest still use excerpts).

### What we deliberately do NOT do

- **No verification cache.** The whole point of verify-on-read is that the answer can change between calls. A short-TTL cache (say, 30s) is plausible if benchmarks show verification dominating latency, but it is not in the v1 scope.
- **No verifier for unsupported doc_types.** If a future doc_type lands in Qdrant without a registered verifier, log a warning and pass the result through. Verification is opt-in per type; missing a verifier is a soft failure.
- **No deck-card iteration fallback.** The fallback in `_fetch_document_text` exists for context expansion, where O(boards × stacks) is acceptable for a single result. In verification we may run the check on every chunk in every search; the fallback would amplify search latency unacceptably.

## Consequences

### Positive

- **Correctness**: Deletes/unshares are reflected in search results within one query, regardless of webhook delivery delays or scanner intervals. Operators can safely raise `vector_sync_scan_interval` to its production-recommended value without leaking ghost records.
- **Privacy**: RAG flows (`nc_semantic_search_answer`) can no longer synthesize answers over content the user has lost access to.
- **Self-healing index**: Lazy eviction means the index converges toward correctness as users query, without needing the scanner to find every drifted record.
- **Single source of truth**: Removes the docstring/code mismatch where `verify_search_results()` was promised but never delivered.

### Negative

- **Latency tax on every search**: Each unique `(doc_id, doc_type)` adds one Nextcloud round-trip. With 3–4 unique docs and 20-way concurrency, this is one parallel batch — likely under 100ms on a healthy connection, but it *is* on the critical path.
- **API load on Nextcloud**: A query that previously hit only Qdrant now hits Nextcloud once per unique result. For high-QPS deployments this is non-trivial and may need rate limiting (already present in `BaseNextcloudClient` retry logic).
- **More moving parts in the search path**: Errors in verification can mask errors in search. Verifier exceptions must be logged distinctly so debugging stays tractable.
- **Doc_type coverage is now a correctness contract**: When we add a new indexable doc_type, we must add a verifier in the same PR, or accept that ghost records are possible for that type. CI should fail if a doc_type is indexed without a registered verifier.

### Neutral

- The `verify_search_results()` function name in existing docstrings becomes accurate. No public API breakage.
- Webhooks remain valuable — they keep the index *recall* fresh (so semantically-relevant new docs appear in results quickly). Verification only handles the *precision* side (filtering inaccessible ones out).

## Alternatives Considered

**1. Tighten webhook delivery cadence.** Reduce Nextcloud's webhook cron interval from 5 minutes to 1 minute, or run a dedicated webhook worker. *Rejected as a complete solution*: addresses average-case latency but does nothing for missed webhooks, server-down windows, or app surfaces that lack rich events. We still recommend operators do this — it improves recall freshness — but it cannot replace verification.

**2. Synchronous webhook acknowledgement.** Have the MCP server delete from Qdrant inside the webhook handler before returning 2xx. *Rejected*: still doesn't help missed webhooks, and adds a hard dependency from the webhook critical path to Qdrant being reachable. Already partially implemented; verify-on-read complements it rather than replacing it.

**3. Bloom filter / negative cache of recently-deleted IDs.** Maintain an in-memory set of "known deleted" IDs populated by webhook handlers, consulted before returning search results. *Rejected*: cannot answer for unshares (which are user-relative, not global), grows unbounded, and is essentially a worse verifier — verifying against Nextcloud is authoritative and not much slower for the page sizes we deal with.

**4. Verify only in `nc_semantic_search_answer`, not `nc_semantic_search`.** Argue that raw search is "advisory" and verification only matters when the LLM consumes content. *Rejected*: ghost records in raw search results are still misleading to users and to other tools that compose on top of search. The bar for a search tool is "results are accessible," not "results are accessible if you happen to feed them into a sampling tool."

**5. Pre-verification at index time only (no query-time check).** *Already what we have*, and the problem statement.

## Related Decisions

- ADR-007: Background Vector Sync — establishes the polling architecture that produces drift.
- ADR-008: MCP Sampling for Semantic Search — defines the RAG flow that most acutely needs verified results.
- ADR-010: Webhook-Based Vector Sync — reduces but does not eliminate drift; verify-on-read closes the residual gap.
- ADR-013: RAG Evaluation — verification policy should be exercised in eval suites (with both fresh and stale fixtures).

## References

- `nextcloud_mcp_server/search/semantic.py:52` and `search/bm25_hybrid.py:75` — orphaned `verify_search_results()` references.
- `nextcloud_mcp_server/server/semantic.py:428-453` — current note-only verification in `nc_semantic_search_answer`.
- `nextcloud_mcp_server/search/context.py::_fetch_document_text` — per-doc-type fetch logic that informs the verifier registry.
- `nextcloud_mcp_server/vector/placeholder.py::delete_placeholder_point` — filter-based Qdrant delete pattern to extend for full-document eviction.

## Implementation Checklist

- [ ] Create `nextcloud_mcp_server/search/verification.py` with `verify_search_results()` and the verifier registry.
- [ ] Implement `_verify_note`, `_verify_news_item`, `_verify_file` (PROPFIND), `_verify_deck_card` (metadata fast-path only).
- [ ] Add `delete_document_points()` in `vector/placeholder.py` (or a new `vector/eviction.py`) for non-placeholder filter-based deletes.
- [ ] Wire into `nc_semantic_search` with `limit * 2` over-fetch, trim to `limit` after verification.
- [ ] Wire into `nc_semantic_search_answer`, replacing the per-type note branch.
- [ ] Update existing docstrings in `search/semantic.py:52` and `search/bm25_hybrid.py:75` to point at the new helper.
- [ ] Unit tests: each verifier handles 200/403/404/transient distinctly; dedup collapses chunks; eviction is scheduled on `False`.
- [ ] Integration test: index a note, delete via API (no webhook), confirm the next semantic search does not return it.
- [ ] CI guard: enumerate indexed doc_types in `vector/scanner.py` and assert each has a registered verifier.
- [ ] Document the latency budget and rate-limit posture in `docs/configuration.md`.
