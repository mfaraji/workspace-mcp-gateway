"""Which Gmail messages an account shares with the assistant.

Per connected Gmail account the user chooses, on the browser ``/connections`` page only
(never through a model-visible tool), either specific labels (the default is the configured
access label, ``agent-access``) or all mail. The choice lives in the connection's
``connection_metadata["gmail_sharing"]``; when absent the account shares only the default
label, so nothing changes until the user opts in to more.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SHARING_KEY = "gmail_sharing"
MODE_LABELS = "labels"
MODE_ALL = "all"
MAX_LABELS = 25


@dataclass(frozen=True)
class Sharing:
    mode: str
    labels: tuple[str, ...]  # label *names*; meaningful only in labels mode

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "labels": list(self.labels)}


def get_sharing(metadata: dict | None, default_label: str) -> Sharing:
    """The account's sharing setting, defaulting to the single access label."""
    raw = (metadata or {}).get(SHARING_KEY) or {}
    if raw.get("mode") == MODE_ALL:
        return Sharing(MODE_ALL, ())
    labels = tuple(
        name for name in (raw.get("labels") or []) if isinstance(name, str) and name.strip()
    )
    return Sharing(MODE_LABELS, labels or (default_label,))


def normalize(mode: str, labels: list[str] | None) -> Sharing:
    """Validate a requested setting. Raises ``ValueError`` on bad input."""
    if mode == MODE_ALL:
        return Sharing(MODE_ALL, ())
    if mode != MODE_LABELS:
        raise ValueError("mode must be 'labels' or 'all'")
    cleaned = tuple(dict.fromkeys(n.strip() for n in (labels or []) if n and n.strip()))
    if not cleaned:
        raise ValueError("choose at least one label")
    if len(cleaned) > MAX_LABELS:
        raise ValueError(f"choose at most {MAX_LABELS} labels")
    return Sharing(MODE_LABELS, cleaned)
