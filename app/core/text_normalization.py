from __future__ import annotations

from typing import Any

_MOJIBAKE_MARKERS = ("Ã", "Â", "â", "Ð", "Ñ")


def _mojibake_score(value: str) -> int:
    return sum(value.count(marker) for marker in _MOJIBAKE_MARKERS)


def repair_mojibake_text(value: str) -> str:
    """Best-effort repair for UTF-8 text decoded as latin-1/cp1252."""
    if not isinstance(value, str):
        return value

    repaired = value
    if _mojibake_score(repaired) == 0:
        return repaired

    for _ in range(3):
        try:
            candidate = repaired.encode("latin-1").decode("utf-8")
        except UnicodeError:
            break

        if candidate == repaired:
            break
        if _mojibake_score(candidate) > _mojibake_score(repaired):
            break

        repaired = candidate
        if _mojibake_score(repaired) == 0:
            break

    return repaired


def repair_mojibake_data(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake_text(value)
    if isinstance(value, list):
        return [repair_mojibake_data(item) for item in value]
    if isinstance(value, dict):
        return {
            repair_mojibake_text(key) if isinstance(key, str) else key: repair_mojibake_data(item)
            for key, item in value.items()
        }
    return value
