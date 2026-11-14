from pathlib import Path

import pytest

from tests.ingest import claude_support, support

TOOL_RESULT = "".join(chr(0x61 + i % 26) for i in range(200_000))
MESSAGE = "é" * 2 + "message body " * 3846


def test_huge_tool_result_and_message_are_stored_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (len(TOOL_RESULT), len(MESSAGE)) == (200_000, 50_000)
    root = tmp_path / "projects"
    (root / "-big").mkdir(parents=True)
    tool_input = {"command": "x" * 70_000}
    (root / "-big" / "s-big.jsonl").write_bytes(
        support.jsonl(
            {
                "type": "user",
                "sessionId": "s-big",
                "uuid": "u1",
                "message": {"role": "user", "content": MESSAGE},
            },
            {
                "type": "assistant",
                "sessionId": "s-big",
                "uuid": "u2",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": tool_input}
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": "s-big",
                "uuid": "u3",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": TOOL_RESULT}
                    ],
                },
            },
        )
    )
    claude_support.register(monkeypatch, root)
    claude_support.ingest(capsys)

    [(text,)] = support.query(
        "SELECT text FROM messages JOIN events USING (event_id) WHERE parsed->>'uuid' = 'u1'"
    )
    assert text == MESSAGE
    [(result, stored_input)] = support.query(
        "SELECT result_text, input->>'command' FROM tool_calls"
    )
    assert result == TOOL_RESULT
    assert stored_input == tool_input["command"]
