import json
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
