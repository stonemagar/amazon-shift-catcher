import json
from pathlib import Path


def load_locations(path: Path) -> list[str]:
    """Load previously seen location names from a local JSON file."""
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if isinstance(payload, dict):
        values = payload.get("locations", [])
    elif isinstance(payload, list):
        values = payload
    else:
        values = []

    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        compact = " ".join(value.split()).strip()
        key = compact.casefold()
        if compact and key not in seen:
            seen.add(key)
            result.append(compact)
    return result


def save_unique_locations(path: Path, locations: list[str]) -> list[str]:
    """Add only new case-insensitive location names and return what was added."""
    existing = load_locations(path)
    seen = {value.casefold() for value in existing}
    added = []

    for value in locations:
        compact = " ".join(value.split()).strip()
        if not compact:
            continue
        key = compact.casefold()
        if key in seen:
            continue
        seen.add(key)
        existing.append(compact)
        added.append(compact)

    if not added and path.exists():
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"locations": existing}
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return added
