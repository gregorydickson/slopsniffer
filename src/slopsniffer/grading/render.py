"""Full-transcript rendering into overlapping chunks.

A session renders to one text unit per event of a content-bearing kind: the message text,
the tool calls the event made and the results it returned. Context blocks (CLAUDE.md, skill
listings, ...) are stated once in a header shared by every chunk; inside event text the
verbatim block is replaced by its marker. Nothing is truncated: an event larger than a
chunk is split into pieces that each carry the event id.

Chunks hold whole events (or pieces) and repeat the tail of the previous chunk: the longest
run of trailing segments within ``chunk_overlap`` tokens, but always at least the last one,
so consecutive chunks share an event. No segment exceeds half of ``chunk_tokens`` and a tail
beyond the last segment never exceeds the other half, so a tail plus one new segment always
fits: ``chunk_tokens`` bounds the transcript text of every chunk (the header travels with
each chunk on top of it). Only an event with no body, whose header alone is over the
segment cap, can break the bound; it still advances the transcript.
"""

from collections import defaultdict
from dataclasses import dataclass

import duckdb

from slopsniffer.config import GradingConfig
from slopsniffer.errors import ConfigError

RENDERED_KINDS = ("message", "tool_call", "tool_result", "context")
CHARS_PER_TOKEN = 4
MARKER_LENGTH = 12


def estimate_tokens(text: str) -> int:
    """Deterministic estimate: one token per four characters, rounded up."""
    return -(-len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Segment:
    """One event, or one piece of an event too large for a chunk."""

    event_id: str
    head: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.head}\n{self.body}" if self.body else self.head

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class Transcript:
    session_id: str
    header: str
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class Chunk:
    index: int
    header: str
    text: str
    tokens: int
    event_ids: tuple[str, ...]

    @property
    def event_range(self) -> tuple[str, str]:
        return self.event_ids[0], self.event_ids[-1]


@dataclass(frozen=True)
class _Block:
    event_id: str
    kind: str
    source_path: str | None
    content_hash: str
    content: str

    @property
    def marker(self) -> str:
        return f"⟦ctx:{self.content_hash[:MARKER_LENGTH]}⟧"


# Each rendering query is restricted to one selected session, never history.
# Context bodies are likewise fetched only for that session's event identities.
_EVENTS = f"""
SELECT e.event_id, e.kind, e.record_type, m.role, m.text
FROM events e LEFT JOIN messages m ON m.event_id = e.event_id
WHERE e.session_id = ? AND e.kind IN ({", ".join("?" * len(RENDERED_KINDS))})
ORDER BY e.seq NULLS LAST, e.byte_offset, e.event_id
"""

_CALLS = """
SELECT call_id, use_event_id, result_event_id, tool_name, CAST(input AS VARCHAR),
       result_text, is_error
FROM tool_calls WHERE session_id = ? ORDER BY call_id
"""

_BLOCKS = """
SELECT c.event_id, c.kind, c.source_path, c.content_hash, b.content
FROM context_blocks c
JOIN blobs b ON b.content_hash = c.content_hash
JOIN events e ON e.event_id = c.event_id
WHERE e.session_id = ?
ORDER BY e.seq NULLS LAST, e.byte_offset, c.event_id, c.ordinal
"""


def render_session(con: duckdb.DuckDBPyConnection, session_id: str) -> Transcript:
    events = con.execute(_EVENTS, [session_id, *RENDERED_KINDS]).fetchall()
    uses: defaultdict[str, list[str]] = defaultdict(list)
    results: defaultdict[str, list[str]] = defaultdict(list)
    for call_id, use_id, result_id, tool, tool_input, result_text, is_error in con.execute(
        _CALLS, [session_id]
    ).fetchall():
        uses[use_id].append(f"-> tool_use {tool} [{call_id}] {tool_input or ''}")
        if result_id is not None:
            status = "error" if is_error else "ok"
            results[result_id].append(
                f"<- tool_result {tool} [{call_id}] {status}\n{result_text or ''}"
            )
    blocks: defaultdict[str, list[_Block]] = defaultdict(list)
    for row in con.execute(_BLOCKS, [session_id]).fetchall():
        blocks[row[0]].append(_Block(*row))
    segments: list[Segment] = []
    for event_id, kind, record_type, role, text in events:
        own = blocks.get(event_id, [])
        parts = [_deflate(text, own)] if text else []
        if not text and kind == "context":
            parts.append(" ".join(b.marker for b in own) or record_type)
        parts += uses.get(event_id, []) + results.get(event_id, [])
        label = role or kind
        segments.append(Segment(event_id, f"[e:{event_id}] {label}", "\n".join(parts)))
    return Transcript(session_id, _header(blocks), tuple(segments))


def _deflate(text: str, own: list[_Block]) -> str:
    for block in sorted(own, key=lambda b: len(b.content), reverse=True):
        if block.content.strip():
            text = text.replace(block.content, block.marker)
    return text


def _header(blocks: defaultdict[str, list[_Block]]) -> str:
    seen: dict[str, _Block] = {}
    for per_event in blocks.values():
        for block in per_event:
            seen.setdefault(block.content_hash, block)
    entries = [
        f"{b.marker} {b.kind} {b.source_path or '-'}\n{b.content}"
        for b in sorted(seen.values(), key=lambda b: (b.event_id, b.content_hash))
    ]
    return "## Context blocks in force\n" + "\n".join(entries) if entries else ""


def render_chunks(
    con: duckdb.DuckDBPyConnection, grading: GradingConfig, session_id: str
) -> list[Chunk]:
    return chunk_transcript(
        render_session(con, session_id), grading.chunk_tokens, grading.chunk_overlap
    )


def chunk_transcript(transcript: Transcript, chunk_tokens: int, chunk_overlap: int) -> list[Chunk]:
    if chunk_tokens < 1 or not 0 <= chunk_overlap < chunk_tokens:
        raise ConfigError(
            f"grading chunk_overlap ({chunk_overlap}) must be below chunk_tokens ({chunk_tokens})"
        )
    cap = chunk_tokens // 2
    tail_budget = min(chunk_overlap, chunk_tokens - cap)
    segments = [piece for s in transcript.segments for piece in _pieces(s, cap)]
    chunks: list[Chunk] = []
    begin = new = 0
    while new < len(segments):
        end = new + 1
        used = sum(s.tokens for s in segments[begin:end])
        while end < len(segments) and used + segments[end].tokens <= chunk_tokens:
            used += segments[end].tokens
            end += 1
        taken = segments[begin:end]
        chunks.append(
            Chunk(
                index=len(chunks),
                header=transcript.header,
                text="\n".join(s.text for s in taken),
                tokens=sum(s.tokens for s in taken),
                event_ids=tuple(dict.fromkeys(s.event_id for s in taken)),
            )
        )
        begin = _tail_start(segments, begin, end, tail_budget)
        new = end
    return chunks


def _tail_start(segments: list[Segment], begin: int, end: int, budget: int) -> int:
    """Start of the longest run of trailing segments within ``budget`` tokens (at least one)."""
    start = end - 1
    used = segments[start].tokens
    while start > begin and used + segments[start - 1].tokens <= budget:
        start -= 1
        used += segments[start].tokens
    return start


def _pieces(segment: Segment, cap: int) -> list[Segment]:
    """The segment itself, or consecutive pieces each within ``cap`` tokens."""
    if segment.tokens <= cap or not segment.body:
        return [segment]
    continued = f"[e:{segment.event_id}] (continued)"
    pieces: list[Segment] = []
    offset = 0
    while offset < len(segment.body):
        head = segment.head if not pieces else continued
        room = cap * CHARS_PER_TOKEN - len(head) - 1
        if room < 1:
            raise ConfigError(f"grading chunk_tokens is too small to hold one event header ({cap})")
        pieces.append(Segment(segment.event_id, head, segment.body[offset : offset + room]))
        offset += room
    return pieces
