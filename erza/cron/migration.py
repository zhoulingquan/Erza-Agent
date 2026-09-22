"""Cron store migration helpers (canonical home).

``migrate_cron_store`` moved here verbatim from ``erza.cli.commands``
so the composition root (``erza/composition/gateway.py``) no longer
reaches into the CLI entry layer's private namespace
(``commands._migrate_cron_store``). ``erza.cli.commands`` keeps a
re-export for backward compatibility (tests import it from there).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from erza.config.schema import Config


def migrate_cron_store(config: Config) -> None:
    """One-time migration: move legacy global cron store into the workspace."""
    from erza.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_path), str(new_path))


# Backward-compat alias: historical private name used by CLI/tests.
_migrate_cron_store = migrate_cron_store
