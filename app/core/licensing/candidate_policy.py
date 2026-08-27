from __future__ import annotations

from collections.abc import Sequence


# WhatsApp can present more than one ten-row interactive page, and existing
# catalogue flows legitimately contain 10-21 distinct identities (for example,
# Copilot, Defender for Endpoint, and E5).  Above this boundary a bare catalogue
# word is no longer a safe product choice: presenting dozens of unrelated plans
# obscures the requested SKU and encourages accidental selection.
MAX_PRESENTABLE_SKU_CANDIDATES = 25


def requires_candidate_narrowing(candidates: Sequence[object]) -> bool:
    """Return whether the seller must qualify a catalogue query before selection.

    This is deliberately a presentation/confirmation guard, not a search limit.
    The complete candidate set remains attached to the pending operation so no SKU
    is silently truncated or made impossible to select after a precise title reply.
    """

    return len(candidates) > MAX_PRESENTABLE_SKU_CANDIDATES


def combine_product_qualifier(original_query: str, qualifier: str) -> str:
    """Combine a follow-up qualifier without duplicating an already complete query."""

    original = " ".join(original_query.split()).strip()
    detail = " ".join(qualifier.split()).strip()
    if not original:
        return detail
    if not detail:
        return original
    original_words = original.casefold().split()
    detail_words = detail.casefold().split()
    if all(word in detail_words for word in original_words):
        return detail
    if all(word in original_words for word in detail_words):
        return original
    return f"{original} {detail}"


def candidate_narrowing_question(product_query: str, candidate_count: int) -> str:
    """Build the shared, seller-facing narrowing request for every entry path."""

    query = " ".join(product_query.split()).strip() or "that product"
    return (
        f'I found {candidate_count} catalogue SKUs related to "{query}". That wording is '
        "too broad to choose safely, so I have not selected or changed any SKU. Please "
        "add a distinguishing detail such as the full product family, workload, edition "
        "or plan, or the exact Product ID / SKU ID from the source document."
    )
