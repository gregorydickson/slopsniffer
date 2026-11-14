import itertools
from collections.abc import Iterator

import duckdb
import pytest

from slopsniffer.cli import main
from slopsniffer.errors import ConfigError
from slopsniffer.grading import render
from slopsniffer.ingest import claude, engine
from tests.grading.support import memory_db
from tests.helpers import REPO_ROOT

SESSION = "s-main"
CONTENT_KINDS = ("message", "tool_call", "tool_result", "context")
SETTINGS = [(40, 10), (60, 0), (120, 30), (400, 100), (100_000, 2_000)]


@pytest.fixture(scope="module")
def ingested(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch_module: pytest.MonkeyPatch
) -> Iterator[duckdb.DuckDBPyConnection]:
    data = tmp_path_factory.mktemp("data")
    monkeypatch_module.setenv("SLOPSNIFFER_HOME", str(data))
    monkeypatch_module.setenv("HOME", str(data / "home"))
    monkeypatch_module.setitem(
        engine.REGISTRY, "claude", claude.source(REPO_ROOT / "tests" / "fixtures" / "claude")
    )
    assert main(["ingest", "--harness", "claude", "--json"]) == 0
    con = duckdb.connect(str(data / "telemetry.duckdb"), read_only=True)
    yield con
    con.close()


@pytest.fixture(scope="module")
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def event_ids(con: duckdb.DuckDBPyConnection, kinds: tuple[str, ...]) -> list[str]:
    marks = ", ".join("?" * len(kinds))
    rows = con.execute(
        f"SELECT event_id FROM events WHERE session_id = ? AND kind IN ({marks}) "
        "ORDER BY seq, event_id",
        [SESSION, *kinds],
    ).fetchall()
    return [r[0] for r in rows]


def chunks_of(con: duckdb.DuckDBPyConnection, tokens: int, overlap: int) -> list[render.Chunk]:
    return render.chunk_transcript(render.render_session(con, SESSION), tokens, overlap)


@pytest.mark.parametrize(("tokens", "overlap"), SETTINGS)
def test_union_of_chunk_event_ranges_is_the_full_rendered_event_set(
    ingested: duckdb.DuckDBPyConnection, tokens: int, overlap: int
) -> None:
    chunks = chunks_of(ingested, tokens, overlap)
    covered = [e for c in chunks for e in c.event_ids]
    full = event_ids(ingested, CONTENT_KINDS)
    assert set(covered) == set(full)
    assert list(dict.fromkeys(covered)) == full
    assert all(c.event_range == (c.event_ids[0], c.event_ids[-1]) for c in chunks)


def test_bookkeeping_events_are_not_rendered(ingested: duckdb.DuckDBPyConnection) -> None:
    bookkeeping = set(event_ids(ingested, ("cost", "meta", "request", "unknown")))
    assert bookkeeping
    covered = {e for c in chunks_of(ingested, 60, 10) for e in c.event_ids}
    assert covered.isdisjoint(bookkeeping)


@pytest.mark.parametrize(("tokens", "overlap"), [s for s in SETTINGS if s[0] < 1000])
def test_consecutive_chunks_share_at_least_one_event(
    ingested: duckdb.DuckDBPyConnection, tokens: int, overlap: int
) -> None:
    chunks = chunks_of(ingested, tokens, overlap)
    assert len(chunks) > 1
    for before, after in itertools.pairwise(chunks):
        assert set(before.event_ids) & set(after.event_ids)
        assert after.event_ids[0] in before.event_ids


def test_a_transcript_that_fits_one_chunk_is_one_chunk(ingested: duckdb.DuckDBPyConnection) -> None:
    chunks = chunks_of(ingested, 100_000, 2_000)
    assert [c.index for c in chunks] == [0]


def test_overlap_tail_is_the_longest_run_of_trailing_events_within_chunk_overlap(
    ingested: duckdb.DuckDBPyConnection,
) -> None:
    transcript = render.render_session(ingested, SESSION)
    sizes = {s.event_id: s.tokens for s in transcript.segments}
    tokens = 400
    assert max(sizes.values()) <= tokens // 2
    grown = False
    for overlap in (0, 25, 80, 150):
        chunks = render.chunk_transcript(transcript, tokens, overlap)
        assert len(chunks) > 1
        for before, after in itertools.pairwise(chunks):
            shared = [e for e in after.event_ids if e in before.event_ids]
            assert tuple(shared) == before.event_ids[len(before.event_ids) - len(shared) :]
            if len(shared) > 1:
                assert sum(sizes[e] for e in shared) <= overlap
                grown = True
            earlier = before.event_ids.index(shared[0])
            if earlier > 0:
                assert (
                    sum(sizes[e] for e in shared) + sizes[before.event_ids[earlier - 1]] > overlap
                )
    assert grown


@pytest.mark.parametrize(("tokens", "overlap"), [(100, 99), (100, 60), (240, 239), (400, 399)])
def test_no_chunk_exceeds_chunk_tokens_even_with_overlap_near_the_chunk_size(
    ingested: duckdb.DuckDBPyConnection, tokens: int, overlap: int
) -> None:
    chunks = chunks_of(ingested, tokens, overlap)
    assert all(c.tokens <= tokens for c in chunks)
    assert {e for c in chunks for e in c.event_ids} == set(event_ids(ingested, CONTENT_KINDS))


def test_events_whose_header_alone_exceeds_the_cap_still_advance_and_overlap() -> None:
    segments = tuple(
        render.Segment(f"ev{i}", f"[e:ev{i}] user-header-padding-{'x' * 30}", "") for i in range(5)
    )
    chunks = render.chunk_transcript(render.Transcript("s", "", segments), 16, 2)
    assert {e for c in chunks for e in c.event_ids} == {s.event_id for s in segments}
    for before, after in itertools.pairwise(chunks):
        assert set(before.event_ids) & set(after.event_ids)


def test_context_blocks_are_stated_once_in_the_header_and_not_in_event_text(
    ingested: duckdb.DuckDBPyConnection,
) -> None:
    transcript = render.render_session(ingested, SESSION)
    blocks = ingested.execute(
        "SELECT DISTINCT c.content_hash, b.content FROM context_blocks c "
        "JOIN blobs b USING (content_hash) JOIN events e ON e.event_id = c.event_id "
        "WHERE e.session_id = ?",
        [SESSION],
    ).fetchall()
    markers = [f"⟦ctx:{h[:12]}⟧ " for h, _ in blocks]
    assert markers
    assert all(transcript.header.count(m) == 1 for m in markers)
    body = "\n".join(s.text for s in transcript.segments)
    claude_md = next(c for _, c in blocks if "Always write tests first." in c)
    assert claude_md in transcript.header
    assert claude_md not in body
    assert all(c.header == transcript.header for c in chunks_of(ingested, 60, 10))


def test_tool_calls_and_results_are_rendered_in_full(ingested: duckdb.DuckDBPyConnection) -> None:
    text = "\n".join(c.text for c in chunks_of(ingested, 100_000, 0))
    rows = ingested.execute(
        "SELECT tool_name, call_id, CAST(input AS VARCHAR), result_text FROM tool_calls "
        "WHERE session_id = ?",
        [SESSION],
    ).fetchall()
    assert rows
    for tool, call_id, tool_input, result in rows:
        assert f"-> tool_use {tool} [{call_id}] {tool_input}" in text
        if result is not None:
            assert f"<- tool_result {tool} [{call_id}]" in text
            assert result in text


def test_an_event_larger_than_a_chunk_is_split_without_losing_a_character() -> None:
    con = memory_db()
    big = "".join(f"line {i:05d}\n" for i in range(3000))
    con.execute("INSERT INTO sessions (session_id, harness) VALUES ('big', 'claude')")
    for seq, (eid, kind) in enumerate([("e1", "message"), ("e2", "message"), ("e3", "message")], 1):
        con.execute(
            "INSERT INTO events (event_id, session_id, seq, record_type, kind, source_file_id,"
            " byte_offset, byte_length) VALUES (?, 'big', ?, 'user', ?, 1, ?, 1)",
            [eid, seq, kind, seq],
        )
    con.execute("INSERT INTO messages VALUES ('e1', 'user', 'before')")
    con.execute("INSERT INTO messages VALUES ('e2', 'assistant', ?)", [big])
    con.execute("INSERT INTO messages VALUES ('e3', 'user', 'after')")
    transcript = render.render_session(con, "big")
    chunks = render.chunk_transcript(transcript, 500, 100)
    cap = 500 // 2
    pieces = render._pieces(transcript.segments[1], cap)
    assert len(pieces) > 1
    assert "".join(p.body for p in pieces) == big
    assert all(p.tokens <= cap for p in pieces)
    assert {e for c in chunks for e in c.event_ids} == {"e1", "e2", "e3"}
    assert all(c.tokens <= 500 for c in chunks)
    for before, after in itertools.pairwise(chunks):
        assert set(before.event_ids) & set(after.event_ids)


def test_empty_session_renders_no_chunks() -> None:
    con = memory_db()
    assert render.chunk_transcript(render.render_session(con, "nobody"), 100, 10) == []


@pytest.mark.parametrize(("tokens", "overlap"), [(100, 100), (100, 150), (0, 0), (100, -1)])
def test_overlap_not_below_chunk_size_is_a_config_error(tokens: int, overlap: int) -> None:
    transcript = render.Transcript("s", "", (render.Segment("e", "[e:e] user", "hi"),))
    with pytest.raises(ConfigError):
        render.chunk_transcript(transcript, tokens, overlap)


def test_estimator_is_deterministic_and_monotonic() -> None:
    texts = ["", "a", "abcd", "abcde", "x" * 1000]
    assert [render.estimate_tokens(t) for t in texts] == [render.estimate_tokens(t) for t in texts]
    sizes = [render.estimate_tokens(t) for t in texts]
    assert sizes == sorted(sizes)
    assert render.estimate_tokens("é" * 40) == render.estimate_tokens("e" * 40)
