import json
import resource
import sys
from pathlib import Path

import pytest

from tests.ingest import support

pytestmark = pytest.mark.slow

GIB = 1024**3
SOURCE_BYTES = 2 * GIB
PEAK_RSS_LIMIT = 3 * GIB
# ru_maxrss is bytes on macOS and KiB on Linux.
RSS_UNIT = 1 if sys.platform == "darwin" else 1024


def claude_like_line(n: int) -> bytes:
    """A synthetic transcript-shaped record; every 1000th one carries a 1 MiB payload."""
    text = ("lorem ipsum dolor sit amet " * (40000 if n % 1000 == 0 else 30))[:-1]
    record = {
        "type": "assistant" if n % 2 else "user",
        "sessionId": f"session-{n // 50000}",
        "uuid": f"uuid-{n}",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(record).encode() + b"\n"


def write_source(path: Path) -> int:
    written, n = 0, 0
    with path.open("wb") as f:
        while written < SOURCE_BYTES:
            chunk = b"".join(claude_like_line(n + i) for i in range(1000))
            f.write(chunk)
            written += len(chunk)
            n += 1000
    return written


def test_two_gib_ingest_peak_rss_stays_under_three_gib(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "big.jsonl"
    size = write_source(source)
    data_dir = tmp_path / "data"

    result = support.run_child(root, data_dir)
    assert result.returncode == 0, result.stderr

    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * RSS_UNIT
    assert peak < PEAK_RSS_LIMIT, f"peak child RSS {peak / GIB:.2f} GiB"
    rows = support.query(
        "SELECT path, status, committed_offset FROM source_files", data_dir=data_dir
    )
    assert rows == [(str(source.resolve()), "complete", size)]
