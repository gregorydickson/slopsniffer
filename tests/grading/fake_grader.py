"""A stand-in for ``codex exec`` and ``claude -p``, driven by ``$FAKE_GRADER_SCENARIO``.

Invoked as ``fake_grader.py <codex|claude> <argv...>`` by the PATH shims. The scenario maps each
backend to a list of behaviours consumed one per call (the last repeating) and ``labels`` maps a
rubric to the label an ``ok`` answer gives (default: the schema's first label). Behaviours:
``ok``, ``display_event`` / ``bracket_event`` (citations retain transcript display markers),
``ok_fenced`` (claude: the verdict only as fenced text in ``result``), ``quota``,
``fail`` (non-zero exit), ``ok_nonzero`` (codex: a valid answer, then exit 1), and the
schema-invalid answers ``not_json``, ``wrong_label``,
``foreign_event``, ``extra_field``. A proposal draft (a schema with ``skill_md``) answers ``ok``
with the prompt's instruction file plus ``DRAFT_LINE``, ``decline`` with ``propose: false`` and
``empty`` with blank text.
Every call appends ``{backend, argv, rubric, event_ids, behaviour, usage}`` to
``$FAKE_GRADER_LOG`` (``rubric`` is None for a draft).
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

EVENT_ID = re.compile(r"\[e:([^\]\s]+)\]")
INVALID = ("not_json", "wrong_label", "foreign_event", "extra_field")
DRAFT_LINE = "- Synthetic rule added by the fake drafter."


def next_behaviour(backend: str) -> str:
    path = Path(os.environ["FAKE_GRADER_SCENARIO"])
    scenario: dict[str, Any] = json.loads(path.read_text())
    queue: list[str] = scenario[backend]
    if len(queue) > 1:
        scenario[backend] = queue[1:]
        path.write_text(json.dumps(scenario))
    return queue[0]


def labels() -> dict[str, str]:
    scenario: dict[str, Any] = json.loads(Path(os.environ["FAKE_GRADER_SCENARIO"]).read_text())
    found: dict[str, str] = scenario.get("labels", {})
    return found


def draft(behaviour: str, prompt: str) -> object:
    if behaviour == "decline":
        return {"propose": False, "reason": "Model variance, not a gap.", "skill_md": ""}
    if behaviour == "empty":
        return {"propose": True, "reason": "A missing rule.", "skill_md": "  \n"}
    return {"propose": True, "reason": "A missing rule.", "skill_md": f"{prompt}\n{DRAFT_LINE}\n"}


def answer(behaviour: str, schema: dict[str, Any], event_ids: list[str], prompt: str) -> object:
    if "skill_md" in schema["properties"]:
        return draft(behaviour, prompt)
    rubric = schema["properties"]["rubric"]["enum"][0]
    label = labels().get(rubric, schema["properties"]["label"]["enum"][0])
    verdict: dict[str, object] = {
        "rubric": rubric,
        "label": label,
        "score": 0.5,
        "reason": f"Synthetic verdict for {rubric}.",
        "cited_event_ids": event_ids[:1],
    }
    if behaviour == "wrong_label":
        verdict["label"] = "not-a-label"
    elif behaviour in ("display_event", "bracket_event"):
        verdict["cited_event_ids"] = [
            ("[e:" + event + "]") if behaviour == "bracket_event" else "e:" + event
            for event in event_ids[:1]
        ]
    elif behaviour == "foreign_event":
        verdict["cited_event_ids"] = ["event-outside-the-chunk"]
    elif behaviour == "extra_field":
        verdict["confidence"] = 0.9
    return verdict


def codex(argv: list[str], prompt: str, behaviour: str) -> tuple[int, dict[str, int]]:
    schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
    out = Path(argv[argv.index("-o") + 1])
    usage = {"input_tokens": len(prompt) // 4, "output_tokens": 7}
    if behaviour == "quota":
        sys.stderr.write("ERROR: You've hit your usage limit. Try again later.\n")
        return 1, {}
    if behaviour == "fail":
        sys.stderr.write("ERROR: stream disconnected before completion\n")
        return 2, {}
    ids = list(dict.fromkeys(EVENT_ID.findall(prompt)))
    text = (
        "Looks fine to me."
        if behaviour == "not_json"
        else json.dumps(answer(behaviour, schema, ids, prompt))
    )
    out.write_text(text)
    sys.stdout.write(json.dumps({"type": "turn.completed", "usage": usage}) + "\n")
    return (1 if behaviour == "ok_nonzero" else 0), usage


def claude(argv: list[str], prompt: str, behaviour: str) -> tuple[int, dict[str, int]]:
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    usage = {
        "input_tokens": 3,
        "cache_creation_input_tokens": len(prompt) // 4,
        "cache_read_input_tokens": 11,
        "output_tokens": 5,
    }
    if behaviour == "quota":
        result = {"type": "result", "is_error": True, "result": "Claude AI usage limit reached"}
        sys.stdout.write(json.dumps(result))
        return 1, {}
    if behaviour == "fail":
        sys.stderr.write("API Error: 500 Internal Server Error\n")
        return 1, {}
    ids = list(dict.fromkeys(EVENT_ID.findall(prompt)))
    doc: dict[str, object] = {"type": "result", "is_error": False, "usage": usage}
    if behaviour == "not_json":
        doc["result"] = "Looks fine to me."
    elif behaviour == "ok_fenced":
        doc["result"] = "```json\n" + json.dumps(answer("ok", schema, ids, prompt)) + "\n```"
    else:
        verdict = answer(behaviour, schema, ids, prompt)
        doc["result"] = json.dumps(verdict)
        doc["structured_output"] = verdict
    sys.stdout.write(json.dumps(doc))
    return 0, usage


def main() -> int:
    backend, argv = sys.argv[1], sys.argv[2:]
    prompt = sys.stdin.read()
    behaviour = next_behaviour(backend)
    if backend == "codex":
        schema_text = Path(argv[argv.index("--output-schema") + 1]).read_text()
        code, usage = codex(argv, prompt, behaviour)
    else:
        schema_text = argv[argv.index("--json-schema") + 1]
        code, usage = claude(argv, prompt, behaviour)
    entry = {
        "backend": backend,
        "argv": argv,
        "rubric": json.loads(schema_text)["properties"].get("rubric", {"enum": [None]})["enum"][0],
        "event_ids": list(dict.fromkeys(EVENT_ID.findall(prompt))),
        "behaviour": behaviour,
        "usage": usage,
    }
    with Path(os.environ["FAKE_GRADER_LOG"]).open("a") as log:
        log.write(json.dumps(entry) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
