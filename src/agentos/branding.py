"""Single place where the product name lives, so renaming stays cheap.

Everything user-visible (CLI name, config filename, state directory) is derived
from these constants. Nothing else in the codebase should hardcode the name.
"""

from __future__ import annotations

APP_NAME = "agentos"
CLI_NAME = "agentctl"
CONFIG_FILENAME = f"{APP_NAME}.yaml"
STATE_DIRNAME = f".{APP_NAME}"
DB_FILENAME = "state.db"
