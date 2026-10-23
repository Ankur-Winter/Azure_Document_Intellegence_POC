from __future__ import annotations

import re
from collections.abc import Sequence


def normalize_text(text: str) -> str:
    """Normalize whitespace and case while preserving punctuation for CER."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def edit_distance(left: Sequence[str] | str, right: Sequence[str] | str) -> int:
    """Levenshtein distance with memory proportional to the shorter input."""
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, left_item in enumerate(left, start=1):
        current = [i]
        for j, right_item in enumerate(right, start=1):
            current.append(
                min(
                    current[j - 1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (left_item != right_item),
                )
            )
        previous = current
    return previous[-1]


def compare_text(extracted: str, expected: str) -> dict[str, float | bool | int]:
    """Return normalized CER/WER and corresponding bounded accuracy values."""
    actual = normalize_text(extracted)
    reference = normalize_text(expected)
    char_errors = edit_distance(actual, reference)
    actual_words = actual.split()
    reference_words = reference.split()
    word_errors = edit_distance(actual_words, reference_words)
    cer = char_errors / max(1, len(reference))
    wer = word_errors / max(1, len(reference_words))
    return {
        "exact_match": actual == reference,
        "char_errors": char_errors,
        "reference_chars": len(reference),
        "character_error_rate": cer,
        "character_accuracy": max(0.0, 1.0 - cer),
        "word_errors": word_errors,
        "reference_words": len(reference_words),
        "word_error_rate": wer,
        "word_accuracy": max(0.0, 1.0 - wer),
    }

