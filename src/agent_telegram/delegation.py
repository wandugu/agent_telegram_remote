"""Names displayed in handed-over tasks."""

from typing import Any


def caller_name(params: dict[str, Any]) -> str:
    """The agent that handed this job over, for the executor prompts."""

    origin = params.get("_dispatch_source") if isinstance(params.get("_dispatch_source"), dict) else {}
    source = str(origin.get("source_agent") or params.get("source_agent") or "").strip().casefold()
    return {
        "claude": "Claude",
        "codex": "Codex",
        "antigravity": "Antigravity",
        "pi": "Pi",
        "dsh": "DSH",
    }.get(source, "上游代理")  # i18n: data


def spaced(name: str) -> str:
    """A Latin name between Chinese text gets spaces; the generic upstream label does not."""

    return f" {name} " if name.isascii() else name


