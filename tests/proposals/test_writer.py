"""Proposal file contracts, including names that collide after sanitizing."""

import hashlib
import json
from pathlib import Path

import pytest

from slopsniffer.proposals.writer import UnsafeTarget, Writer, directory_name


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("ll:review", "ll-review"),
        ("XReviewX", "XReviewX"),
        ("..--review.--", "review"),
        ("../", "surface"),
        ("", "surface"),
    ],
)
def test_directory_names_are_safe_single_segments(surface: str, expected: str) -> None:
    assert directory_name(surface) == expected


def test_colliding_names_preserve_both_proposals_and_complete_diffs(
    tmp_path: Path,
    non_utf8_text_defaults: None,
) -> None:
    writer = Writer(tmp_path, [])
    writer.reset()
    current, improved = "keep\nold\n", "keep\nnew é\n"
    evidence = {"session_ids": ["s-b", "s-a"], "skill": "review"}
    first = writer.write("ll:review", "real.md", current, improved, evidence)
    second = writer.write("ll/review", "real.md", current, "other\n", evidence)
    suffix = hashlib.sha256(b"ll/review").hexdigest()[:8]
    assert {first.path.parent.name, second.path.parent.name} == {"ll-review", f"ll-review-{suffix}"}
    assert first.path.read_text("utf-8") == improved
    assert second.path.read_text("utf-8") == "other\n"
    assert first.diff_path.read_text("utf-8") == (
        f"--- real.md\n+++ {first.path}\n@@ -1,2 +1,2 @@\n keep\n-old\n+new é\n"
    )
    assert json.loads(first.evidence_path.read_text("utf-8")) == evidence
    # A safe pre-existing folder is usable, but a plain file cannot be the root.
    existing = writer.root / "existing"
    existing.mkdir()
    assert writer.write("existing", "real.md", current, improved, evidence).path.parent == existing
    writer.reset()
    assert not writer.root.exists()
    writer.root.write_text("file")
    with pytest.raises(UnsafeTarget, match="not a plain directory"):
        Writer(tmp_path, [])
