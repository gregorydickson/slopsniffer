"""Execute the documented version example and validate the public rubric reference."""

import re
import shlex
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from slopsniffer.grading.rubrics import load_rubrics
from tests.helpers import REPO_ROOT


def test_readme_version_example_runs_and_rubric_table_matches_registry() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    examples = [
        shlex.split(line)
        for block in re.findall(r"```bash\n(.*?)```", readme, re.DOTALL)
        for line in block.splitlines()
        if line.startswith("uv run slopsniffer ")
    ]
    version_examples = [example for example in examples if "--version" in example]
    assert version_examples, "Readers need an executable example for checking their version"
    script = Path(sys.executable).parent / "slopsniffer"
    for example in version_examples:
        result = subprocess.run(
            [str(script), *example[3:]],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == version("slopsniffer")
        assert result.stderr == ""
    documented_rubrics = set(re.findall(r"^\| ([\w-]+) \| `", readme, re.MULTILINE))
    assert documented_rubrics == set(load_rubrics())
