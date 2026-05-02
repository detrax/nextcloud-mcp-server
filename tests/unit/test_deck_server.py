import pytest

from nextcloud_mcp_server.models.deck import DeckCard
from nextcloud_mcp_server.server.deck import _truncate_card_descriptions

pytestmark = pytest.mark.unit


def _make_card(card_id: int, description: str | None) -> DeckCard:
    return DeckCard(
        id=card_id,
        title=f"Card {card_id}",
        stackId=1,
        type="plain",
        order=card_id,
        archived=False,
        owner="testuser",
        description=description,
    )


def test_truncate_card_descriptions_no_op_when_limit_is_none():
    """When description_max_length is None, descriptions are left untouched."""
    cards = [_make_card(1, "x" * 5000)]
    _truncate_card_descriptions(cards, None)
    assert cards[0].description is not None
    assert len(cards[0].description) == 5000


def test_truncate_card_descriptions_truncates_long_descriptions():
    """Descriptions over the limit are truncated and marked with an ellipsis."""
    cards = [_make_card(1, "x" * 5000), _make_card(2, "short")]
    _truncate_card_descriptions(cards, 100)
    assert cards[0].description is not None
    assert len(cards[0].description) == 101  # 100 chars + ellipsis
    assert cards[0].description.endswith("…")
    assert cards[1].description == "short"


def test_truncate_card_descriptions_handles_none_description():
    """Cards with no description are skipped without error."""
    cards = [_make_card(1, None)]
    _truncate_card_descriptions(cards, 100)
    assert cards[0].description is None


def test_truncate_card_descriptions_at_exact_boundary():
    """Descriptions at exactly the limit should not be truncated."""
    cards = [_make_card(1, "x" * 100)]
    _truncate_card_descriptions(cards, 100)
    assert cards[0].description == "x" * 100
