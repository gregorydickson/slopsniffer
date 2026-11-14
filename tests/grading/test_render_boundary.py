"""Rendered evidence retains nested context, nullable tool data and event ordering."""

import json
import os
import subprocess
import sys
from itertools import pairwise

import pytest

from slopsniffer.errors import ConfigError
from slopsniffer.grading import render
from tests.grading.support import memory_db
from tests.helpers import REPO_ROOT


def test_render_session_keeps_context_tool_results_and_order() -> None:
    full, nested, blank = "Full clause and nested", "nested", " "
    full_hash, nested_hash, blank_hash = "f" * 64, "a" * 64, "b" * 64
    with memory_db() as con:
        con.execute(
            "INSERT INTO sessions (session_id, harness) VALUES ('s', 'claude'), ('other', 'codex')"
        )
        events = [
            ("msg", "s", 1, "assistant", "message"),
            ("ctx", "s", 2, "system", "context"),
            ("bare-context", "s", 3, "summary", "context"),
            ("use", "s", 4, "assistant", "tool_call"),
            ("result", "s", 5, "user", "tool_result"),
            ("bookkeeping", "s", 6, "meta", "unknown"),
            ("null-last", "s", None, "user", "message"),
            ("alien", "other", 0, "user", "message"),
        ]
        for eid, sid, seq, record, kind in events:
            con.execute(
                "INSERT INTO events (event_id, session_id, seq, record_type, kind, "
                "source_file_id, byte_offset, byte_length) VALUES (?, ?, ?, ?, ?, 1, 0, 1)",
                [eid, sid, seq, record, kind],
            )
        for eid, role, text in [
            ("msg", "assistant", f"{full} / {nested}"),
            ("null-last", "user", "End"),
            ("alien", "user", "Excluded session"),
            ("bookkeeping", "user", "Excluded record"),
        ]:
            con.execute("INSERT INTO messages VALUES (?, ?, ?)", [eid, role, text])
        for digest, content in [(full_hash, full), (nested_hash, nested), (blank_hash, blank)]:
            con.execute("INSERT INTO blobs VALUES (?, ?)", [digest, content])
        for eid, ordinal, kind, path, digest in [
            ("msg", 0, "claude_md", "/rules/CLAUDE.md", full_hash),
            ("msg", 1, "skill_listing", None, nested_hash),
            ("msg", 2, "other", None, blank_hash),
            ("ctx", 0, "claude_md", "/duplicate/CLAUDE.md", full_hash),
            ("ctx", 1, "skill_listing", None, nested_hash),
        ]:
            con.execute(
                "INSERT INTO context_blocks VALUES (?, ?, ?, ?, ?)",
                [eid, ordinal, kind, path, digest],
            )
        for call_id, result_id, tool, tool_input, result_text, is_error in [
            ("c1", "result", "Bash", '{"command":"echo café"}', "café", True),
            ("c2", "result", "Read", None, None, False),
            ("c3", None, "Edit", None, None, False),
        ]:
            con.execute(
                "INSERT INTO tool_calls (call_id, session_id, use_event_id, result_event_id, "
                "tool_name, input, result_text, is_error) VALUES (?, 's', 'use', ?, ?, ?, ?, ?)",
                [call_id, result_id, tool, tool_input, result_text, is_error],
            )
        transcript = render.render_session(con, "s")
        assert render.render_session(con, "other").header == ""
    full_marker, nested_marker, blank_marker = (
        "⟦ctx:ffffffffffff⟧",
        "⟦ctx:aaaaaaaaaaaa⟧",
        "⟦ctx:bbbbbbbbbbbb⟧",
    )
    assert transcript.session_id == "s"
    assert transcript.header == (
        f"## Context blocks in force\n{nested_marker} skill_listing -\n{nested}\n"
        f"{blank_marker} other -\n{blank}\n"
        f"{full_marker} claude_md /rules/CLAUDE.md\n{full}"
    )
    assert [(s.event_id, s.text) for s in transcript.segments] == [
        ("msg", f"[e:msg] assistant\n{full_marker} / {nested_marker}"),
        ("ctx", f"[e:ctx] context\n{full_marker} {nested_marker}"),
        ("bare-context", "[e:bare-context] context\nsummary"),
        (
            "use",
            '[e:use] tool_call\n-> tool_use Bash [c1] {"command":"echo café"}\n'
            "-> tool_use Read [c2] \n-> tool_use Edit [c3] ",
        ),
        (
            "result",
            "[e:result] tool_result\n<- tool_result Bash [c1] error\ncafé\n"
            "<- tool_result Read [c2] ok\n",
        ),
        ("null-last", "[e:null-last] user\nEnd"),
    ]


def test_chunk_indices_tokens_and_unique_ids_follow_the_actual_transcript() -> None:
    segments = tuple(render.Segment(f"e{i}", "h", "ABCDEFGHIJKLMNOPQ") for i in range(5))
    transcript = render.Transcript("s", "Context header", segments)
    chunks = render.chunk_transcript(transcript, 12, 6)
    pairs = list(pairwise(segments))
    assert [(c.index, c.header, c.text, c.tokens, c.event_ids) for c in chunks] == [
        (i, "Context header", f"{first.text}\n{second.text}", 10, (first.event_id, second.event_id))
        for i, (first, second) in enumerate(pairs)
    ]
    prefix = [
        ("[e:e] user\nABCDEFGHIJKLM\n[e:e] (continued)\nNOPQRS", 12, ("e",)),
        ("[e:e] (continued)\nNOPQRS\n[e:e] (continued)\nTUVWXY", 12, ("e",)),
    ]
    for body, tail in [
        ("ABCDEFGHIJKLMNOPQRSTUVWXY", []),
        (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            [("[e:e] (continued)\nTUVWXY\n[e:e] (continued)\nZ", 11, ("e",))],
        ),
    ]:
        big = render.Transcript("s", "Header", (render.Segment("e", "[e:e] user", body),))
        assert [(c.text, c.tokens, c.event_ids) for c in render.chunk_transcript(big, 12, 0)] == (
            prefix + tail
        )
    one_character_room = render.Transcript(
        "s",
        "",
        (render.Segment("ev", "[e:ev] tool_result", "ab"),),
    )
    assert [c.text for c in render.chunk_transcript(one_character_room, 10, 0)] == [
        "[e:ev] tool_result\na\n[e:ev] (continued)\nb"
    ]
    with pytest.raises(ConfigError, match="too small to hold one event header"):
        render.chunk_transcript(big, 1, 0)


def test_overlap_includes_an_exact_budget_tail_and_keeps_oversized_neighbors() -> None:
    segments = tuple(render.Segment(f"e{i}", "h", "ABCDEFGHIJKLM") for i in range(6))
    transcript = render.Transcript("s", "", segments)
    assert [c.event_ids for c in render.chunk_transcript(transcript, 20, 8)] == [
        ("e0", "e1", "e2", "e3", "e4"),
        ("e3", "e4", "e5"),
    ]
    mixed = render.Transcript(
        "s",
        "",
        (
            render.Segment("before", "small", ""),
            render.Segment("huge", "x" * 80, ""),
            render.Segment("after", "end", ""),
        ),
    )
    assert [c.event_ids for c in render.chunk_transcript(mixed, 16, 7)] == [
        ("before",),
        ("before", "huge"),
        ("huge", "after"),
    ]


def test_chunk_splitting_advances_with_a_short_process_deadline() -> None:
    # The short parent-side call associates this test with the real splitter.
    short = render.Transcript("s", "", (render.Segment("e", "h", "a"),))
    assert [c.event_ids for c in render.chunk_transcript(short, 12, 0)] == [("e",)]
    script = REPO_ROOT / "tests/fixtures/grading/split_transcript.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        )
    except subprocess.TimeoutExpired:
        pytest.fail("chunk splitting did not advance within five seconds")
    assert json.loads(result.stdout) == [
        {"event_ids": ["e"], "nonempty": True, "bounded": True},
        {"event_ids": [f"e{i}" for i in range(6)], "nonempty": True, "bounded": True},
    ]
