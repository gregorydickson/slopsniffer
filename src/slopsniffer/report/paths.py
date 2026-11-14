"""Home-relative display paths, applied only to report copies and rendered text."""

import re
from pathlib import Path
from typing import cast

from slopsniffer.contracts import Report


def display_text(text: str) -> str:
    home = str(Path.home()).rstrip("/")
    if not home:
        return text
    return re.sub(r"(?<![\w/])" + re.escape(home) + r"(?=/|$|[\s'\"<>:,)])", "~", text)


def display_report(report: Report) -> Report:
    """Copy the document so database values and callers' input remain untouched."""
    return cast(Report, _display(report))


def _display(value: object) -> object:
    if isinstance(value, str):
        return display_text(value)
    if isinstance(value, dict):
        return {display_text(k): _display(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_display(v) for v in value]
    return value
