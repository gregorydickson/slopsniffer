from pathlib import Path
from typing import Any, is_typeddict

import pytest
from jsonschema import Draft202012Validator

from slopsniffer import contracts
from slopsniffer.cli import COMMANDS
from tests.helpers import CLI_SCHEMAS_DIR, load_schema, schema_files, schema_registry

SchemaObject = tuple[Path, set[str], set[str]]


def typeddicts() -> dict[str, Any]:
    return {
        name: obj
        for name, obj in vars(contracts).items()
        if is_typeddict(obj) and obj.__module__ == contracts.__name__
    }


def schema_objects() -> dict[str, list[SchemaObject]]:
    """Every named object schema: each file's titled root plus its $defs."""
    found: dict[str, list[SchemaObject]] = {}
    for path in schema_files():
        schema = load_schema(path)
        named: dict[str, Any] = {schema["title"]: schema, **schema.get("$defs", {})}
        for name, obj in named.items():
            entry = (path, set(obj["properties"]), set(obj.get("required", [])))
            found.setdefault(name, []).append(entry)
    return found


def test_each_contract_name_is_defined_by_exactly_one_schema_object() -> None:
    objects = schema_objects()
    assert set(objects) == set(typeddicts())
    duplicated = {name for name, entries in objects.items() if len(entries) > 1}
    assert duplicated == set()


@pytest.mark.parametrize("name", sorted(typeddicts()))
def test_typeddict_fields_equal_schema_properties(name: str) -> None:
    typed = typeddicts()[name]
    [(path, properties, required)] = schema_objects()[name]
    fields = set(typed.__required_keys__) | set(typed.__optional_keys__)
    assert fields == properties, path
    assert set(typed.__required_keys__) == required, path


def test_every_subcommand_has_an_output_schema() -> None:
    stems = {path.name.removesuffix(".schema.json") for path in CLI_SCHEMAS_DIR.iterdir()}
    assert stems == set(COMMANDS) | {"error"}


@pytest.mark.parametrize("path", schema_files(), ids=lambda p: p.name)
def test_schema_is_valid_and_every_ref_resolves(path: Path) -> None:
    schema = load_schema(path)
    Draft202012Validator.check_schema(schema)
    resolver = schema_registry().resolver(base_uri=path.name)
    unresolved = []
    for ref in refs(schema):
        try:
            resolver.lookup(ref)
        except Exception as exc:
            unresolved.append((ref, repr(exc)))
    assert unresolved == []


def refs(node: object) -> list[str]:
    if isinstance(node, dict):
        own = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return own + [ref for value in node.values() for ref in refs(value)]
    if isinstance(node, list):
        return [ref for value in node for ref in refs(value)]
    return []
