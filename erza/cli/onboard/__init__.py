"""Interactive onboarding questionnaire for Erza (package facade).

The single-file ``cli/onboard.py`` was split into sibling modules by
responsibility cluster (Phase 7): ``prompts`` / ``fields`` / ``providers`` /
``channels`` / ``wizard``.  This module re-exports the historical import
surface so existing callers and tests keep working unchanged.
"""

import sys
import types

from erza.cli.onboard import channels, fields, prompts, providers, wizard
from erza.cli.onboard.channels import (
    _configure_channel,
    _configure_channels,
    _get_channel_config_class,
    _get_channel_info,
    _get_channel_names,
)
from erza.cli.onboard.fields import (
    FieldTypeInfo,
    _format_value,
    _format_value_for_input,
    _get_constraint_hint,
    _get_field_display_name,
    _get_field_type_info,
    _input_bool,
    _input_text,
    _input_with_existing,
    _is_sensitive_field,
    _mask_value,
    _validate_field_constraint,
)
from erza.cli.onboard.prompts import (
    _BACK_PRESSED,
    _get_questionary,
    _pause,
    _select_with_back,
    _show_config_panel,
    _show_main_menu_header,
    _show_section_header,
    console,
    questionary,
)
from erza.cli.onboard.providers import (
    _FIELD_HANDLERS,
    _MODEL_PRESET_CACHE,
    _SELECT_FIELD_HINTS,
    _configure_model_presets,
    _configure_provider,
    _configure_providers,
    _configure_pydantic_model,
    _get_current_provider,
    _get_provider_info,
    _get_provider_names,
    _handle_context_window_field,
    _handle_fallback_models_field,
    _handle_model_field,
    _handle_model_preset_field,
    _handle_provider_field,
    _input_context_window_with_recommendation,
    _input_model_with_autocomplete,
    _is_str_or_none,
    _sync_preset_cache,
    _try_auto_fill_context_window,
)
from erza.cli.onboard.wizard import (
    _SETTINGS_GETTER,
    _SETTINGS_SECTIONS,
    _SETTINGS_SETTER,
    OnboardResult,
    _configure_general_settings,
    _has_unsaved_changes,
    _print_summary_panel,
    _prompt_main_menu_exit,
    _show_summary,
    _summarize_model,
    run_onboard,
)

_SUBMODULES = ("fields", "prompts", "providers", "channels", "wizard")


class _OnboardFacade(types.ModuleType):
    """Module facade that mirrors attribute writes into the split submodules.

    Tests monkeypatch attributes on ``erza.cli.onboard`` (e.g.
    ``onboard_wizard.questionary``, ``onboard_wizard._select_with_back``)
    and rely on the runtime globals of the implementation being updated.
    Since the implementation now lives in sibling submodules, every write to
    a package attribute is propagated to any submodule that currently binds
    the same name, preserving the late-binding behaviour of the original
    single-file module.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for _sub in _SUBMODULES:
            _mod = sys.modules.get(f"erza.cli.onboard.{_sub}")
            if _mod is not None and hasattr(_mod, name):
                setattr(_mod, name, value)


sys.modules[__name__].__class__ = _OnboardFacade
