"""The caller's JSON schema: checked before use, and the yardstick a model's reply is validated against."""

from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from supacrawl.exceptions import ValidationError

MAX_REPORTED_ERRORS = 5


def schema_validator(schema: Any) -> Any:
    """A validator for ``schema``; refuses one that is not valid JSON Schema or does not describe an object."""
    if not isinstance(schema, dict):
        raise ValidationError("schema must be a JSON object", field="schema", value=schema)
    if schema.get("type", "object") != "object":
        raise ValidationError(
            f"schema must describe an object, got type {schema['type']!r}; put a list inside a property",
            field="schema",
        )
    validator_class = validator_for(schema)
    try:
        validator_class.check_schema(schema)
    except SchemaError as e:
        raise ValidationError(f"schema is not valid JSON Schema: {e.message}", field="schema") from e
    return validator_class(schema)


def schema_errors(validator: Any, data: Any) -> list[str]:
    """Each way ``data`` breaks the schema, as ``<json path>: <message>``."""
    errors = sorted(validator.iter_errors(data), key=lambda e: e.json_path)
    return [f"{e.json_path}: {e.message}" for e in errors[:MAX_REPORTED_ERRORS]]
