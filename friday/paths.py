"""Default locations for persistent FRIDAY data; paths are created by their owners."""

from pathlib import Path


FRIDAY_ROOT = Path(r"D:\FRIDAY")
DATA_ROOT = FRIDAY_ROOT / "data"
MEMORY_DB_PATH = DATA_ROOT / "memory" / "friday-memory.sqlite3"
LOGS_PATH = DATA_ROOT / "logs"
CACHE_PATH = DATA_ROOT / "cache"
VAULT_PATH = FRIDAY_ROOT / "vault"
MODELS_PATH = FRIDAY_ROOT / "models"
RUNTIME_PATH = FRIDAY_ROOT / "runtime"
SCRIPTS_PATH = FRIDAY_ROOT / "scripts"
