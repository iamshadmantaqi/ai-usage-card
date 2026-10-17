"""The privacy guard. Only model names, tool names, dates and numbers may reach the SVG."""
from __future__ import annotations

import re

# A home or volume path, a message or request id, a UUID, a long hex id, a tilde path.
FORBIDDEN = (
    re.compile(r"/Users/"),
    re.compile(r"/Volumes/"),
    re.compile(r"/private/"),
    re.compile(r"/home/"),
    re.compile(r"[0-9a-fA-F]{20,}"),
    re.compile(r"msg_"),
    re.compile(r"req_"),
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
    re.compile(r"~"),
)


class PrivacyError(Exception):
    """The output holds something that looks like a path or an id."""


def looks_private(text: str) -> bool:
    return any(pattern.search(text) for pattern in FORBIDDEN)


def check_privacy(text: str) -> None:
    for pattern in FORBIDDEN:
        if pattern.search(text):
            raise PrivacyError("output matches forbidden pattern %s" % pattern.pattern)
