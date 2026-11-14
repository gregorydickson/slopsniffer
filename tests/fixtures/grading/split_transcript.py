"""Synthetic long-event and loose-packing cases, isolated behind a short test deadline."""

import json

from slopsniffer.grading.render import Segment, Transcript, chunk_transcript

cases = [
    (Transcript("long", "", (Segment("e", "[e:e] user", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),)), 12, 0),
    (
        Transcript("loose", "", tuple(Segment(f"e{i}", "h", "ABCDEFGHIJKLM") for i in range(6))),
        20,
        8,
    ),
]
results = []
for transcript, size, overlap in cases:
    chunks = chunk_transcript(transcript, size, overlap)
    results.append(
        {
            "event_ids": sorted({eid for chunk in chunks for eid in chunk.event_ids}),
            "nonempty": all(chunk.event_ids for chunk in chunks),
            "bounded": all(0 < chunk.tokens <= size for chunk in chunks),
        }
    )
print(json.dumps(results))
