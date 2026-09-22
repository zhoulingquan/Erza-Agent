"""Top-level onboarding flow: general settings, summary, exit handling and the entry point."""

from dataclasses import dataclass

from pydantic import BaseModel
from rich.panel import Panel
from rich.table import Table

from erza.cli.onboard.channels import _configure_channels, _get_channel_names
from erza.cli.onboard.fields import _format_value, _get_field_display_name, _get_field_type_info
from erza.cli.onboard.prompts import _get_questionary, _pause, _show_main_menu_header, console
from erza.cli.onboard.providers import (
    _configure_model_presets,
    _configure_providers,
    _configure_pydantic_model,
    _get_provider_names,
    _sync_preset_cache,
)
from erza.config.loader import get_config_path, load_config
from erza.config.schema import Config


@dataclass
class OnboardResult:
    """Result of an onboarding session."""

    config: Config
    should_save: bool


# --- General Settings ---

_SETTINGS_SECTIONS: dict[str, tuple[str, str, set[str] | None]] = {
    "Agent Settings": (
        "Agent Defaults",
        "Configure default model, temperature, and behavior",
        None,
    ),
    "Channel Common": (
        "Channel Common",
        "Configure cross-channel behavior: progress, tool hints, retries",
        None,
    ),
    "API Server": ("API Server", "Configure OpenAI-compatible API endpoint", None),
    "Gateway": ("Gateway Settings", "Configure server host, port", None),
    "Tools": (
        "Tools Settings",
        "Configure web search, shell exec, and other tools",
        {"mcp_servers"},
    ),
}

_SETTINGS_GETTER = {
    "Agent Settings": lambda c: c.agents.defaults,
    "Channel Common": lambda c: c.channels,
    "API Server": lambda c: c.api,
    "Gateway": lambda c: c.gateway,
    "Tools": lambda c: c.tools,
}

_SETTINGS_SETTER = {
    "Agent Settings": lambda c, v: setattr(c.agents, "defaults", v),
    "Channel Common": lambda c, v: setattr(c, "channels", v),
    "API Server": lambda c, v: setattr(c, "api", v),
    "Gateway": lambda c, v: setattr(c, "gateway", v),
    "Tools": lambda c, v: setattr(c, "tools", v),
}


def _configure_general_settings(config: Config, section: str) -> None:
    """Configure a general settings section (header + model edit + writeback)."""
    meta = _SETTINGS_SECTIONS.get(section)
    if not meta:
        return
    display_name, subtitle, skip = meta
    model = _SETTINGS_GETTER[section](config)
    updated = _configure_pydantic_model(model, display_name, skip_fields=skip)
    if updated is not None:
        _SETTINGS_SETTER[section](config, updated)


# --- Summary ---


def _summarize_model(obj: BaseModel) -> list[tuple[str, str]]:
    """Recursively summarize a Pydantic model. Returns list of (field, value) tuples."""
    items: list[tuple[str, str]] = []
    for field_name, field_info in type(obj).model_fields.items():
        value = getattr(obj, field_name, None)
        if value is None or value == "" or value == {} or value == []:
            continue
        display = _get_field_display_name(field_name, field_info)
        ftype = _get_field_type_info(field_info)
        if ftype.type_name == "model" and isinstance(value, BaseModel):
            for nested_field, nested_value in _summarize_model(value):
                items.append((f"{display}.{nested_field}", nested_value))
            continue
        formatted = _format_value(value, rich=False, field_name=field_name)
        if formatted != "[not set]":
            items.append((display, formatted))
    return items


def _print_summary_panel(rows: list[tuple[str, str]], title: str) -> None:
    """Build a two-column summary panel and print it."""
    if not rows:
        return
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    for field, value in rows:
        table.add_row(field, value)
    console.print(Panel(table, title=f"[bold]{title}[/bold]", border_style="blue"))


def _show_summary(config: Config) -> None:
    """Display configuration summary using rich."""
    console.print()

    # Providers
    provider_rows = []
    for name, display in _get_provider_names().items():
        provider = getattr(config.providers, name, None)
        status = (
            "[green]configured[/green]"
            if (provider and provider.api_key)
            else "[dim]not configured[/dim]"
        )
        provider_rows.append((display, status))
    _print_summary_panel(provider_rows, "LLM Providers")

    # Channels
    channel_rows = []
    for name, display in _get_channel_names().items():
        channel = getattr(config.channels, name, None)
        if channel:
            enabled = (
                channel.get("enabled", False)
                if isinstance(channel, dict)
                else getattr(channel, "enabled", False)
            )
            status = "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"
        else:
            status = "[dim]not configured[/dim]"
        channel_rows.append((display, status))
    _print_summary_panel(channel_rows, "Chat Channels")

    # Model Presets
    preset_rows = []
    for name, preset in config.model_presets.items():
        preset_rows.append((name, f"{preset.model} (ctx={preset.context_window_tokens})"))
    _print_summary_panel(preset_rows, "Model Presets")

    # Settings sections
    for title, model in [
        ("Agent Settings", config.agents.defaults),
        ("Channel Common", config.channels),
        ("API Server", config.api),
        ("Gateway", config.gateway),
        ("Tools", config.tools),
    ]:
        _print_summary_panel(_summarize_model(model), title)

    _pause()


# --- Main Entry Point ---


def _has_unsaved_changes(original: Config, current: Config) -> bool:
    """Return True when the onboarding session has committed changes."""
    return original.model_dump(by_alias=True) != current.model_dump(by_alias=True)


def _prompt_main_menu_exit(has_unsaved_changes: bool) -> str:
    """Resolve how to leave the main menu."""
    if not has_unsaved_changes:
        return "discard"

    answer = (
        _get_questionary()
        .select(
            "You have unsaved changes. What would you like to do?",
            choices=[
                "[S] Save and Exit",
                "[X] Exit Without Saving",
                "[R] Resume Editing",
            ],
            default="[R] Resume Editing",
            qmark=">",
        )
        .ask()
    )

    if answer == "[S] Save and Exit":
        return "save"
    if answer == "[X] Exit Without Saving":
        return "discard"
    return "resume"


def run_onboard(initial_config: Config | None = None) -> OnboardResult:
    """Run the interactive onboarding questionnaire.

    Args:
        initial_config: Optional pre-loaded config to use as starting point.
                       If None, loads from config file or creates new default.
    """
    _get_questionary()

    if initial_config is not None:
        base_config = initial_config.model_copy(deep=True)
    else:
        config_path = get_config_path()
        if config_path.exists():
            base_config = load_config()
        else:
            base_config = Config()

    original_config = base_config.model_copy(deep=True)
    config = base_config.model_copy(deep=True)
    _sync_preset_cache(config)

    last_main_choice: str | None = None
    while True:
        console.clear()
        _show_main_menu_header()

        try:
            answer = (
                _get_questionary()
                .select(
                    "What would you like to configure?",
                    choices=[
                        "[P] LLM Provider",
                        "[M] Model Presets",
                        "[C] Chat Channel",
                        "[H] Channel Common",
                        "[A] Agent Settings",
                        "[I] API Server",
                        "[G] Gateway",
                        "[T] Tools",
                        "[V] View Configuration Summary",
                        "[S] Save and Exit",
                        "[X] Exit Without Saving",
                    ],
                    default=last_main_choice,
                    qmark=">",
                )
                .ask()
            )
        except KeyboardInterrupt:
            answer = None

        if answer is None:
            action = _prompt_main_menu_exit(_has_unsaved_changes(original_config, config))
            if action == "save":
                return OnboardResult(config=config, should_save=True)
            if action == "discard":
                return OnboardResult(config=original_config, should_save=False)
            continue

        _menu_dispatch = {
            "[P] LLM Provider": lambda: _configure_providers(config),
            "[M] Model Presets": lambda: _configure_model_presets(config),
            "[C] Chat Channel": lambda: _configure_channels(config),
            "[H] Channel Common": lambda: _configure_general_settings(config, "Channel Common"),
            "[A] Agent Settings": lambda: _configure_general_settings(config, "Agent Settings"),
            "[I] API Server": lambda: _configure_general_settings(config, "API Server"),
            "[G] Gateway": lambda: _configure_general_settings(config, "Gateway"),
            "[T] Tools": lambda: _configure_general_settings(config, "Tools"),
            "[V] View Configuration Summary": lambda: _show_summary(config),
        }

        if answer == "[S] Save and Exit":
            return OnboardResult(config=config, should_save=True)
        if answer == "[X] Exit Without Saving":
            return OnboardResult(config=original_config, should_save=False)

        action_fn = _menu_dispatch.get(answer)
        if action_fn:
            last_main_choice = answer
            action_fn()
