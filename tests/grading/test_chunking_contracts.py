"""Finite transcripts terminate with complete evidence and actionable config errors."""

import pytest

from slopsniffer.errors import ConfigError
from slopsniffer.grading.render import Segment, Transcript, chunk_transcript
from tests.ingest.deadlines import deadline


def test_long_event_splitting_and_chunk_packing_advance_within_a_deadline() -> None:
    long = Transcript("s", "Context", (Segment("e", "[e:e] user", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),))
    packed = Transcript("s", "", tuple(Segment(f"e{i}", "h", "ABCDEFGHIJKLM") for i in range(6)))
    with deadline(1, "transcript splitting and packing"):
        split_chunks = chunk_transcript(long, 12, 0)
        packed_chunks = chunk_transcript(packed, 20, 8)
    assert [(c.text, c.event_ids) for c in split_chunks] == [
        ("[e:e] user\nABCDEFGHIJKLM\n[e:e] (continued)\nNOPQRS", ("e",)),
        ("[e:e] (continued)\nNOPQRS\n[e:e] (continued)\nTUVWXY", ("e",)),
        ("[e:e] (continued)\nTUVWXY\n[e:e] (continued)\nZ", ("e",)),
    ]
    assert [c.event_ids for c in packed_chunks] == [
        ("e0", "e1", "e2", "e3", "e4"),
        ("e3", "e4", "e5"),
    ]


def test_invalid_chunk_settings_name_both_configured_values() -> None:
    transcript = Transcript("s", "", ())
    with pytest.raises(ConfigError) as caught:
        chunk_transcript(transcript, 12, 13)
    assert str(caught.value) == "grading chunk_overlap (13) must be below chunk_tokens (12)"
