import atexit
import functools
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from referencing import Registry, Resource
from referencing.jsonschema import SchemaRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"
CLI_SCHEMAS_DIR = SCHEMAS_DIR / "cli"


def schema_files() -> list[Path]:
    return sorted(SCHEMAS_DIR.rglob("*.schema.json"))


def load_schema(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return loaded


def schema_registry() -> SchemaRegistry:
    """Registry keyed by bare file name, the base cross-file $refs resolve against."""
    return Registry().with_resources(
        (path.name, Resource.from_contents(load_schema(path))) for path in schema_files()
    )


def fake_bin(name: str, script: Path, *prefix: str) -> Path:
    """A directory holding executable ``name`` that runs ``script`` with ``prefix`` arguments.

    macOS can stall the first launch of a newly written executable for tens of seconds,
    so each distinct fake is written once per test run, launched once to absorb that
    stall, and reused. Tests drive fakes through environment variables, never the file.
    """
    return _fake_bin(name, script, prefix)


@functools.cache
def _fake_bin(name: str, script: Path, prefix: tuple[str, ...]) -> Path:
    bin_dir = Path(tempfile.mkdtemp(prefix=f"fake-{name}-"))
    atexit.register(shutil.rmtree, bin_dir, ignore_errors=True)
    shim = bin_dir / name
    shim.write_text(
        f"#!{sys.executable}\nimport runpy, sys\n"
        f"sys.argv[1:1] = {list(prefix)!r}\n"
        f"runpy.run_path({str(script)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    subprocess.run([str(shim)], capture_output=True, timeout=300, check=False, env={})
    return bin_dir
