from typing import Any


def untyped(value: object) -> Any:
    """Return the argument typed as Any, so tests can pass deliberately wrong runtime types."""
    return value
