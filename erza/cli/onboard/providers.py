"""Provider, model, preset and fallback configuration for the onboard wizard."""

from functools import lru_cache
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from erza.cli.onboard.fields import (
    _format_value,
    _get_constraint_hint,
    _get_field_display_name,
    _get_field_type_info,
    _input_bool,
    _input_with_existing,
)
from erza.cli.onboard.prompts import (
    _BACK_PRESSED,
    _get_questionary,
    _pause,
    _select_with_back,
    _show_config_panel,
    _show_section_header,
    console,
)
from erza.config.schema import Config, ModelPresetConfig
from erza.providers.model_catalog import (
    format_token_count,
    get_model_context_limit,
    get_model_suggestions,
)

# --- Field Hints for Select Fields ---
# Maps field names to (choices, hint_text)
# To add a new select field with hints, add an entry:
#   "field_name": (["choice1", "choice2", ...], "hint text for the field")
_SELECT_FIELD_HINTS: dict[str, tuple[list[str], str]] = {
    "reasoning_effort": (
        ["low", "medium", "high"],
        "low / medium / high - enables LLM thinking mode",
    ),
}

# Cache of model-preset names populated at runtime so that field handlers can
# offer existing presets as choices (e.g. AgentDefaults.model_preset).
_MODEL_PRESET_CACHE: set[str] = set()


# --- Pydantic Model Configuration ---


def _get_current_provider(model: BaseModel) -> str:
    """Get the current provider setting from a model (if available)."""
    if hasattr(model, "provider"):
        return getattr(model, "provider", "auto") or "auto"
    return "auto"


def _input_model_with_autocomplete(display_name: str, current: Any, provider: str) -> str | None:
    """Get model input with autocomplete suggestions."""
    from prompt_toolkit.completion import Completer, Completion

    default = str(current) if current else ""

    class DynamicModelCompleter(Completer):
        """Completer that dynamically fetches model suggestions."""

        def __init__(self, provider_name: str):
            self.provider = provider_name

        def get_completions(self, document, _complete_event):
            text = document.text_before_cursor
            suggestions = get_model_suggestions(text, provider=self.provider, limit=50)
            for model in suggestions:
                # Skip if model doesn't contain the typed text
                if text.lower() not in model.lower():
                    continue
                yield Completion(
                    model,
                    start_position=-len(text),
                    display=model,
                )

    value = (
        _get_questionary()
        .autocomplete(
            f"{display_name}:",
            choices=[""],  # Placeholder, actual completions from completer
            completer=DynamicModelCompleter(provider),
            default=default,
            qmark=">",
        )
        .ask()
    )

    return value if value is not None else None


def _input_context_window_with_recommendation(
    display_name: str, current: Any, model_obj: BaseModel
) -> int | None:
    """Get context window input with option to fetch recommended value."""
    current_val = current if current else ""

    choices = ["Enter new value"]
    if current_val:
        choices.append("Keep existing value")
    choices.append("[?] Get recommended value")

    choice = (
        _get_questionary()
        .select(
            display_name,
            choices=choices,
            default="Enter new value",
        )
        .ask()
    )

    if choice is None:
        return None

    if choice == "Keep existing value":
        return None

    if choice == "[?] Get recommended value":
        # Get the model name from the model object
        model_name = getattr(model_obj, "model", None)
        if not model_name:
            console.print("[yellow]! Please configure the model field first[/yellow]")
            return None

        provider = _get_current_provider(model_obj)
        context_limit = get_model_context_limit(model_name, provider)

        if context_limit:
            console.print(
                f"[green]+ Recommended context window: {format_token_count(context_limit)} tokens[/green]"
            )
            return context_limit
        else:
            console.print("[yellow]! Could not fetch model info, please enter manually[/yellow]")
            # Fall through to manual input

    # Manual input
    value = (
        _get_questionary()
        .text(
            f"{display_name}:",
            default=str(current_val) if current_val else "",
        )
        .ask()
    )

    if value is None or value == "":
        return None

    try:
        return int(value)
    except ValueError:
        console.print("[yellow]! Invalid number format, value not saved[/yellow]")
        return None


def _handle_model_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """Handle the 'model' field with autocomplete and context-window auto-fill."""
    provider = _get_current_provider(working_model)
    new_value = _input_model_with_autocomplete(field_display, current_value, provider)
    if new_value is not None and new_value != current_value:
        setattr(working_model, field_name, new_value)
        _try_auto_fill_context_window(working_model, new_value)


def _handle_context_window_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """Handle context_window_tokens with recommendation lookup."""
    new_value = _input_context_window_with_recommendation(
        field_display, current_value, working_model
    )
    if new_value is not None:
        setattr(working_model, field_name, new_value)


def _handle_model_preset_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """Handle the 'model_preset' field with a list of existing presets."""
    preset_names = sorted(_MODEL_PRESET_CACHE)
    choices = ["(clear/unset)"] + preset_names
    default_choice = str(current_value) if current_value else "(clear/unset)"
    new_value = _select_with_back(field_display, choices, default=default_choice)
    if new_value is _BACK_PRESSED:
        return
    if new_value == "(clear/unset)":
        setattr(working_model, field_name, None)
    elif new_value is not None:
        setattr(working_model, field_name, new_value)


def _handle_provider_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """Handle the 'provider' field with a list of registered providers."""
    provider_names = sorted(_get_provider_names().keys())
    choices = ["auto"] + provider_names
    default_choice = str(current_value) if current_value else "auto"
    new_value = _select_with_back(field_display, choices, default=default_choice)
    if new_value is _BACK_PRESSED:
        return
    if new_value is not None:
        setattr(working_model, field_name, new_value)


def _handle_fallback_models_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """Handle the 'fallback_models' field with preset-aware list management."""
    from erza.config.schema import InlineFallbackConfig

    items: list[Any] = list(current_value) if isinstance(current_value, list) else []
    preset_names = sorted(_MODEL_PRESET_CACHE)

    while True:
        console.clear()
        console.print(f"[bold]{field_display}[/bold]")
        if items:
            for idx, item in enumerate(items, 1):
                if isinstance(item, InlineFallbackConfig):
                    console.print(f"  {idx}. {item.model} ({item.provider}) [inline]")
                else:
                    console.print(f"  {idx}. {item}")
        else:
            console.print("  [dim](empty)[/dim]")
        console.print()

        choices = ["[+] Add preset"]
        if items:
            choices.append("[-] Remove last")
            choices.append("[X] Clear all")
        choices.append("[Done]")
        choices.append("<- Back")

        answer = (
            _get_questionary()
            .select(
                "Manage fallback models:",
                choices=choices,
                qmark=">",
            )
            .ask()
        )

        if answer is None or answer == "<- Back":
            return
        if answer == "[Done]":
            setattr(working_model, field_name, items)
            return
        if answer == "[+] Add preset":
            if not preset_names:
                console.print("[yellow]! No presets defined yet.[/yellow]")
                _get_questionary().press_any_key_to_continue().ask()
                continue
            add_choices = [p for p in preset_names if p not in items]
            if not add_choices:
                console.print("[yellow]! All presets already added.[/yellow]")
                _get_questionary().press_any_key_to_continue().ask()
                continue
            picked = _select_with_back("Select preset:", add_choices)
            if picked is _BACK_PRESSED or picked is None:
                continue
            items.append(picked)
        elif answer == "[-] Remove last" and items:
            items.pop()
        elif answer == "[X] Clear all" and items:
            items.clear()


_FIELD_HANDLERS: dict[str, Any] = {
    "model": _handle_model_field,
    "context_window_tokens": _handle_context_window_field,
    "model_preset": _handle_model_preset_field,
    "provider": _handle_provider_field,
    "fallback_models": _handle_fallback_models_field,
}


def _is_str_or_none(annotation: Any) -> bool:
    """Check whether a field annotation is ``str | None`` (or ``Optional[str]``)."""
    origin = get_origin(annotation)
    if origin is None:
        return False
    args = get_args(annotation)
    return str in args and type(None) in args


def _configure_pydantic_model(
    model: BaseModel,
    display_name: str,
    *,
    skip_fields: set[str] | None = None,
) -> BaseModel | None:
    """Configure a Pydantic model interactively.

    Returns the updated model only when the user explicitly selects "Done".
    Back and cancel actions discard the section draft.
    """
    skip_fields = skip_fields or set()
    working_model = model.model_copy(deep=True)

    fields = [
        (name, info)
        for name, info in type(working_model).model_fields.items()
        if name not in skip_fields
    ]
    if not fields:
        console.print(f"[dim]{display_name}: No configurable fields[/dim]")
        return working_model

    def get_choices() -> list[str]:
        items = []
        for fname, finfo in fields:
            value = getattr(working_model, fname, None)
            display = _get_field_display_name(fname, finfo)
            formatted = _format_value(value, rich=False, field_name=fname)
            items.append(f"{display}: {formatted}")
        return items + ["[Done]"]

    last_field_name: str | None = None
    while True:
        console.clear()
        _show_config_panel(display_name, working_model, fields)
        choices = get_choices()
        default_choice = None
        if last_field_name:
            for idx, (fname, _) in enumerate(fields):
                if fname == last_field_name:
                    default_choice = choices[idx]
                    break
        answer = _select_with_back("Select field to configure:", choices, default=default_choice)

        if answer is _BACK_PRESSED or answer is None:
            return None
        if answer == "[Done]":
            return working_model

        field_idx = next((i for i, c in enumerate(choices) if c == answer), -1)
        if field_idx < 0 or field_idx >= len(fields):
            return None

        last_field_name = fields[field_idx][0]

        field_name, field_info = fields[field_idx]
        current_value = getattr(working_model, field_name, None)
        ftype = _get_field_type_info(field_info)
        field_display = _get_field_display_name(field_name, field_info) + _get_constraint_hint(
            field_info
        )

        # Nested Pydantic model - recurse
        if ftype.type_name == "model":
            nested = current_value
            created = nested is None
            if nested is None and ftype.inner_type:
                nested = ftype.inner_type()
            if nested and isinstance(nested, BaseModel):
                updated = _configure_pydantic_model(nested, field_display)
                if updated is not None:
                    setattr(working_model, field_name, updated)
                elif created:
                    setattr(working_model, field_name, None)
            continue

        # Registered special-field handlers
        handler = _FIELD_HANDLERS.get(field_name)
        if handler:
            handler(working_model, field_name, field_display, current_value)
            continue

        # Select fields with hints (e.g. reasoning_effort)
        if field_name in _SELECT_FIELD_HINTS:
            choices_list, hint = _SELECT_FIELD_HINTS[field_name]
            select_choices = choices_list + ["(clear/unset)"]
            console.print(f"[dim]  Hint: {hint}[/dim]")
            new_value = _select_with_back(
                field_display, select_choices, default=current_value or select_choices[0]
            )
            if new_value is _BACK_PRESSED:
                continue
            if new_value == "(clear/unset)":
                setattr(working_model, field_name, None)
            elif new_value is not None:
                setattr(working_model, field_name, new_value)
            continue

        # Generic field input
        if ftype.type_name == "literal" and ftype.inner_type:
            select_choices = [str(v) for v in ftype.inner_type]
            default_choice = (
                str(current_value) if current_value in ftype.inner_type else select_choices[0]
            )
            new_value = _select_with_back(field_display, select_choices, default=default_choice)
            if new_value is _BACK_PRESSED:
                continue
            if new_value is not None:
                setattr(working_model, field_name, new_value)
            continue
        if ftype.type_name == "bool":
            new_value = _input_bool(field_display, current_value)
        else:
            new_value = _input_with_existing(
                field_display, current_value, ftype.type_name, field_info=field_info
            )
        if new_value is not None:
            # Normalize empty string to None for optional string fields so that
            # clearing an api_key / api_base actually removes the value.
            if new_value == "" and _is_str_or_none(field_info.annotation):
                new_value = None
            setattr(working_model, field_name, new_value)


def _try_auto_fill_context_window(model: BaseModel, new_model_name: str) -> None:
    """Show the detected context window for the newly chosen model.

    With context_window_tokens defaulting to None (auto-detect), this no
    longer writes a concrete value into the config — the agent loop performs
    the actual auto-detection at runtime based on the current model. We only
    display the detected limit so the user knows what will be used.

    If the user has already set an explicit value, we respect it and stay quiet.
    """
    if not hasattr(model, "context_window_tokens"):
        return

    current_context = getattr(model, "context_window_tokens", None)
    # Only show the hint when the user hasn't set a custom value.
    if current_context is not None:
        return

    provider = _get_current_provider(model)
    context_limit = get_model_context_limit(new_model_name, provider)

    if context_limit:
        console.print(
            f"[dim](i) Detected context window: {format_token_count(context_limit)} tokens (auto)[/dim]"
        )


# --- Model Preset Configuration ---


def _sync_preset_cache(config: Config) -> None:
    """Synchronise the module-level preset name cache from config."""
    _MODEL_PRESET_CACHE.clear()
    _MODEL_PRESET_CACHE.update(config.model_presets.keys())


def _configure_model_presets(config: Config) -> None:
    """Configure model presets (CRUD)."""
    _sync_preset_cache(config)

    def get_preset_choices() -> list[str]:
        choices: list[str] = []
        for name, preset in config.model_presets.items():
            choices.append(f"{name} ({preset.model})")
        choices.append("[+] Add new preset")
        choices.append("<- Back")
        return choices

    last_preset_name: str | None = None
    while True:
        try:
            console.clear()
            _show_section_header(
                "Model Presets",
                "Create, edit or delete named model presets for quick switching",
            )
            choices = get_preset_choices()
            default_choice = None
            if last_preset_name:
                for c in choices:
                    if c.startswith(last_preset_name + " ("):
                        default_choice = c
                        break
            answer = _select_with_back("Select preset:", choices, default=default_choice)

            if answer is _BACK_PRESSED or answer is None or answer == "<- Back":
                break

            assert isinstance(answer, str)

            if answer == "[+] Add new preset":
                name_input = (
                    _get_questionary()
                    .text(
                        "Preset name:",
                        validate=lambda t: True if t and t.strip() else "Name cannot be empty",
                    )
                    .ask()
                )
                if not name_input:
                    continue
                name = name_input.strip()
                if name in config.model_presets:
                    console.print(f"[yellow]! Preset '{name}' already exists[/yellow]")
                    _pause()
                    continue
                if name == "default":
                    console.print(
                        "[yellow]! 'default' is reserved (auto-generated from Agent Settings)[/yellow]"
                    )
                    _pause()
                    continue
                new_preset = ModelPresetConfig(model="")
                updated = _configure_pydantic_model(new_preset, f"New Preset: {name}")
                if updated is not None:
                    config.model_presets[name] = updated
                    _sync_preset_cache(config)
                    last_preset_name = name
                continue

            # Editing / deleting an existing preset
            preset_name = answer.split(" (", 1)[0]
            preset = config.model_presets.get(preset_name)
            if preset is None:
                continue

            last_preset_name = preset_name

            choices = ["Edit", "Cancel"]
            if preset_name != "default":
                choices.insert(1, "Delete")
            action = _select_with_back(
                f"Preset: {preset_name}",
                choices,
                default="Edit",
            )
            if action is _BACK_PRESSED or action == "Cancel" or action is None:
                continue

            if action == "Delete":
                confirm = (
                    _get_questionary()
                    .confirm(
                        f"Delete preset '{preset_name}'?",
                        default=False,
                    )
                    .ask()
                )
                if confirm:
                    del config.model_presets[preset_name]
                    _sync_preset_cache(config)
                    last_preset_name = None
                continue

            if action == "Edit":
                updated = _configure_pydantic_model(preset, f"Edit Preset: {preset_name}")
                if updated is not None:
                    config.model_presets[preset_name] = updated
                    _sync_preset_cache(config)

        except KeyboardInterrupt:
            console.print("\n[dim]Returning to main menu...[/dim]")
            break


# --- Provider Configuration ---


@lru_cache(maxsize=1)
def _get_provider_info() -> dict[str, tuple[str, bool, bool, str]]:
    """Get provider info from registry (cached)."""
    from erza.providers.registry import PROVIDERS

    return {
        spec.name: (
            spec.display_name or spec.name,
            spec.is_gateway,
            spec.is_local,
            spec.default_api_base,
        )
        for spec in PROVIDERS
    }


def _get_provider_names() -> dict[str, str]:
    """Get provider display names."""
    info = _get_provider_info()
    return {name: data[0] for name, data in info.items() if name}


def _configure_provider(config: Config, provider_name: str) -> None:
    """Configure a single LLM provider."""
    provider_config = getattr(config.providers, provider_name, None)
    if provider_config is None:
        console.print(f"[red]Unknown provider: {provider_name}[/red]")
        return

    display_name = _get_provider_names().get(provider_name, provider_name)
    info = _get_provider_info()
    default_api_base = info.get(provider_name, (None, None, None, None))[3]

    if default_api_base and not provider_config.api_base:
        provider_config.api_base = default_api_base

    updated_provider = _configure_pydantic_model(
        provider_config,
        display_name,
    )
    if updated_provider is not None:
        setattr(config.providers, provider_name, updated_provider)


def _configure_providers(config: Config) -> None:
    """Configure LLM providers."""

    def get_provider_choices() -> list[str]:
        """Build provider choices with config status indicators."""
        choices = []
        for name, display in _get_provider_names().items():
            provider = getattr(config.providers, name, None)
            if provider and provider.api_key:
                choices.append(f"{display} *")
            else:
                choices.append(display)
        return choices + ["<- Back"]

    last_provider_key: str | None = None
    while True:
        try:
            console.clear()
            _show_section_header(
                "LLM Providers", "Select a provider to configure API key and endpoint"
            )
            choices = get_provider_choices()
            default_choice = None
            if last_provider_key:
                display = _get_provider_names().get(last_provider_key)
                if display:
                    for c in choices:
                        if c.replace(" *", "") == display:
                            default_choice = c
                            break
            answer = _select_with_back("Select provider:", choices, default=default_choice)

            if answer is _BACK_PRESSED or answer is None or answer == "<- Back":
                break

            # Type guard: answer is now guaranteed to be a string
            assert isinstance(answer, str)
            # Extract provider name from choice (remove " *" suffix if present)
            provider_name = answer.replace(" *", "")
            # Find the actual provider key from display names
            for name, display in _get_provider_names().items():
                if display == provider_name:
                    last_provider_key = name
                    _configure_provider(config, name)
                    break

        except KeyboardInterrupt:
            console.print("\n[dim]Returning to main menu...[/dim]")
            break
