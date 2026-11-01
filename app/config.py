import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    BASE_DIR = BASE_DIR
    FLASK_ENV = "production"
    SECRET_KEY = "zwol-sync-default-secret-key-2026"
    PORT = 8505

    # Database
    DB_HOST = "127.0.0.1"
    DB_PORT = 3307
    DB_NAME = "zwol"
    DB_USER = "ketelhuis"
    DB_PASSWORD = ""
    DB_TABLE_PREFIX = "sync_"
    SQLITE_PATH = BASE_DIR / "data" / "zwol_sync.db"

    # SilvaSoft API
    SILVASOFT_USERNAME = ""
    SILVASOFT_API_KEY = ""
    SILVASOFT_BASE_URL = "https://rest-api.silvasoft.nl/rest"

    # HubSpot API
    HUBSPOT_ACCESS_TOKEN = ""

    # Queue Worker Settings
    SILVASOFT_QUEUE_INTERVAL_SECONDS = 240  # 4 minuten (max 15/uur)


def load_env():
    """Eenvoudige en robuuste .env parser zonder externe dependencies."""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key:
                            os.environ[key] = val
                            if hasattr(Config, key):
                                current_val = getattr(Config, key)
                                if isinstance(current_val, bool):
                                    setattr(Config, key, val.lower() in ("true", "1", "yes"))
                                elif isinstance(current_val, int):
                                    try:
                                        setattr(Config, key, int(val))
                                    except ValueError:
                                        pass
                                else:
                                    setattr(Config, key, val)
        except Exception as e:
            print(f"Waarschuwing: Kon .env niet inlezen ({e})")
