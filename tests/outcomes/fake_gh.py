"""A stand-in for ``gh`` driven by a JSON scenario file (``$FAKE_GH_SCENARIO``).

Every invocation is appended to ``$FAKE_GH_LOG`` as one JSON line ``{argv, stdin}``. Supports
``gh api rate_limit`` (the scenario's ``rate`` list is consumed one value per call, the last
value repeating) and ``gh api graphql --input -`` answering the aliased PR and search queries
``slopsniffer.outcomes.github`` builds. Scenario keys: ``authenticated`` (bool), ``rate``
(list of ints), ``prs`` (``"owner/name#N"`` -> PullRequest node), ``search`` (``"owner/name"``
-> list of nodes), ``graphql_fails`` (bool).
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PR_ALIAS = re.compile(
    r'(\w+): repository\(owner: "([^"]+)", name: "([^"]+)"\) \{ pullRequest\(number: (\d+)\)'
)
SEARCH_ALIAS = re.compile(r'(\w+): search\(type: ISSUE, first: \d+, query: "repo:(\S+) ')


def main() -> int:
    argv = sys.argv[1:]
    stdin = sys.stdin.read() if "--input" in argv else None
    with Path(os.environ["FAKE_GH_LOG"]).open("a") as log:
        log.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")
    state_path = Path(os.environ["FAKE_GH_SCENARIO"])
    scenario: dict[str, Any] = json.loads(state_path.read_text())
    if not scenario.get("authenticated", True):
        sys.stderr.write("To get started with GitHub CLI, please run:  gh auth login\n")
        return 4
    if argv[:2] == ["api", "rate_limit"]:
        rates: list[int] = scenario.get("rate", [5000])
        remaining = rates[0]
        if len(rates) > 1:
            scenario["rate"] = rates[1:]
            state_path.write_text(json.dumps(scenario))
        sys.stdout.write(json.dumps({"resources": {"graphql": {"remaining": remaining}}}))
        return 0
    if argv[:2] == ["api", "graphql"] and stdin is not None:
        if scenario.get("graphql_fails"):
            sys.stderr.write("HTTP 502: Bad Gateway\n")
            return 1
        query = json.loads(stdin)["query"]
        data: dict[str, Any] = {}
        prs = scenario.get("prs", {})
        for alias, owner, name, number in PR_ALIAS.findall(query):
            node = prs.get(f"{owner}/{name}#{number}")
            data[alias] = {"pullRequest": node} if node is not None else None
        for alias, repo in SEARCH_ALIAS.findall(query):
            data[alias] = {"nodes": scenario.get("search", {}).get(repo, [])}
        sys.stdout.write(json.dumps({"data": data}))
        return 0
    sys.stderr.write(f"fake gh: unsupported {argv}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
