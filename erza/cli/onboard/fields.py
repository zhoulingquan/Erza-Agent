"""Field introspection, masking, formatting, constraint validation and basic input components."""

import json
import types
from typing import Any, Literal, NamedTuple, get_args, get_origin

from pydantic import BaseModel

from erza.cli.onboard.prompts import _get_questionary, console

# --- Type Introspection ---


class FieldTypeInfo(NamedTuple):
    """Result of field type introspection."""

    type_name: str
    inner_type: Any


def _get_field_type_info(field_info) -> FieldTypeInfo:
    """Extract field type info from Pydantic field."""
    annotation = field_info.annotation
    if annotation is None:
        return FieldTypeInfo("str", None)

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is types.UnionType:
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            annotation = non_none_args[0]
            origin = get_origin(annotation)
            args = get_args(annotation)

    _simple_types: dict[type, str] = {bool: "bool", int: "int", float: "float"}

    if origin is list or (hasattr(origin, "__name__") and origin.__name__ == "List"):
        return FieldTypeInfo("list", args[0] if args else str)
    if origin is dict or (hasattr(origin, "__name__") and origin.__name__ == "Dict"):
        return FieldTypeInfo("dict", None)
    for py_type, name in _simple_types.items():
        if annotation is py_type:
            return FieldTypeInfo(name, None)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return FieldTypeInfo("model", annotation)
    if origin is Literal:
        return FieldTypeInfo("literal", list(args))
    return FieldTypeInfo("str", None)


def _get_field_display_name(field_key: str, field_info) -> str:
    """Get display name for a field."""
    if field_info and field_info.description:
        return field_info.description
    name = field_key
    suffix_map = {
        "_s": " (seconds)",
        "_ms": " (ms)",
        "_url": " URL",
        "_path": " Path",
        "_id": " ID",
        "_key": " Key",
        "_token": " Token",
    }
    for suffix, replacement in suffix_map.items():
        if name.endswith(suffix):
            name = name[: -len(suffix)] + replacement
            break
    return name.replace("_", " ").title()


# --- Sensitive Field Masking ---

_SENSITIVE_KEYWORDS = frozenset({"api_key", "token", "secret", "password", "credentials"})


def _is_sensitive_field(field_name: str) -> bool:
    """Check if a field name indicates sensitive content."""
    return any(kw in field_name.lower() for kw in _SENSITIVE_KEYWORDS)


def _mask_value(value: str) -> str:
    """Mask a sensitive value, showing only the last 4 characters."""
    if len(value) <= 4:
        return "****"
    return "*" * (len(value) - 4) + value[-4:]


# --- Value Formatting ---


def _format_value(value: Any, rich: bool = True, field_name: str = "") -> str:
    """Single recursive entry point for safe value display. Handles any depth."""
    if value is None or value == "" or value == {} or value == []:
        return "[dim]not set[/dim]" if rich else "[not set]"
    if _is_sensitive_field(field_name) and isinstance(value, str):
        masked = _mask_value(value)
        return f"[dim]{masked}[/dim]" if rich else masked
    if isinstance(value, BaseModel):
        parts = []
        for fname, _finfo in type(value).model_fields.items():
            fval = getattr(value, fname, None)
            formatted = _format_value(fval, rich=False, field_name=fname)
            if formatted != "[not set]":
                parts.append(f"{fname}={formatted}")
        return ", ".join(parts) if parts else ("[dim]not set[/dim]" if rich else "[not set]")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        # Handle dicts containing BaseModel instances
        parts = []
        for k, v in value.items():
            formatted = _format_value(v, rich=False, field_name=str(k))
            parts.append(f"{k}: {formatted}")
        return ", ".join(parts) if parts else ("[dim]not set[/dim]" if rich else "[not set]")
    return str(value)


def _format_value_for_input(value: Any, field_type: str) -> str:
    """Format a value for use as input default."""
    if value is None or value == "":
        return ""
    if field_type == "list" and isinstance(value, list):
        return ",".join(str(v) for v in value)
    if field_type == "dict" and isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _validate_field_constraint(value: Any, field_info) -> str | None:
    """Validate a value against Pydantic Field constraints.

    Returns an error message string if validation fails, None if valid.
    Uses attribute-based detection to handle Pydantic v2 internal types.
    """
    if field_info is None or not hasattr(field_info, "metadata"):
        return None

    for m in field_info.metadata:
        if hasattr(m, "ge") and isinstance(value, (int, float)):
            if value < m.ge:
                return f"Value must be >= {m.ge}"
        if hasattr(m, "gt") and isinstance(value, (int, float)):
            if value <= m.gt:
                return f"Value must be > {m.gt}"
        if hasattr(m, "le") and isinstance(value, (int, float)):
            if value > m.le:
                return f"Value must be <= {m.le}"
        if hasattr(m, "lt") and isinstance(value, (int, float)):
            if value >= m.lt:
                return f"Value must be < {m.lt}"
        if hasattr(m, "min_length") and hasattr(value, "__len__"):
            if len(value) < m.min_length:
                return f"Length must be >= {m.min_length}"
        if hasattr(m, "max_length") and hasattr(value, "__len__"):
            if len(value) > m.max_length:
                return f"Length must be <= {m.max_length}"

    return None


def _get_constraint_hint(field_info) -> str:
    """Derive a human-readable constraint hint from field metadata.

    Returns a string like "(0-10)" or "(>= 0)" to append to field display names.
    """
    if field_info is None or not hasattr(field_info, "metadata"):
        return ""

    ge_val = None
    le_val = None
    for m in field_info.metadata:
        if hasattr(m, "ge"):
            ge_val = m.ge
        if hasattr(m, "le"):
            le_val = m.le

    if ge_val is not None and le_val is not None:
        return f" ({ge_val}-{le_val})"
    if ge_val is not None:
        return f" (>= {ge_val})"
    if le_val is not None:
        return f" (<= {le_val})"
    return ""


# --- Input Handlers ---


def _input_bool(display_name: str, current: bool | None) -> bool | None:
    """Get boolean input via confirm dialog."""
    return (
        _get_questionary()
        .confirm(
            display_name,
            default=bool(current) if current is not None else False,
        )
        .ask()
    )


def _input_text(display_name: str, current: Any, field_type: str, field_info=None) -> Any:
    """Get text input and parse based on field type."""
    default = _format_value_for_input(current, field_type)

    value = _get_questionary().text(f"{display_name}:", default=default).ask()

    if value is None:
        return None

    if field_type == "int":
        try:
            parsed = int(value)
        except ValueError:
            console.print("[yellow]! Invalid number format, value not saved[/yellow]")
            return None
        if field_info:
            error = _validate_field_constraint(parsed, field_info)
            if error:
                console.print(f"[yellow]! {error}, value not saved[/yellow]")
                return None
        return parsed
    elif field_type == "float":
        try:
            parsed = float(value)
        except ValueError:
            console.print("[yellow]! Invalid number format, value not saved[/yellow]")
            return None
        if field_info:
            error = _validate_field_constraint(parsed, field_info)
            if error:
                console.print(f"[yellow]! {error}, value not saved[/yellow]")
                return None
        return parsed
    elif field_type == "list":
        return [v.strip() for v in value.split(",") if v.strip()]
    elif field_type == "dict":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            console.print("[yellow]! Invalid JSON format, value not saved[/yellow]")
            return None

    return value


def _input_with_existing(display_name: str, current: Any, field_type: str, field_info=None) -> Any:
    """Handle input with 'keep existing' option for non-empty values."""
    has_existing = current is not None and current != "" and current != {} and current != []

    if has_existing and not isinstance(current, list):
        choice = (
            _get_questionary()
            .select(
                display_name,
                choices=["Enter new value", "Keep existing value"],
                default="Keep existing value",
            )
            .ask()
        )
        if choice == "Keep existing value" or choice is None:
            return None

    return _input_text(display_name, current, field_type, field_info=field_info)
