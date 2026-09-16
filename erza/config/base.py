"""Leaf base model for all Erza configuration models.

``Base`` lives here (instead of ``erza.config.schema``) so that
configuration fragments owned by other layers — notably the per-tool
config classes in :mod:`erza.config.tool_configs` — can subclass it
without importing the full schema module. Dependency direction:

``erza.config.schema`` → ``erza.config.tool_configs`` → ``erza.config.base``
``erza.tools.*`` → ``erza.config.tool_configs``

``erza.config.schema`` re-exports ``Base`` so existing
``from erza.config.schema import Base`` imports keep working.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
