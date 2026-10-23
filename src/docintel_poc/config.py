from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    endpoint: str
    key: str
    region: str | None = None


def load_settings() -> Settings:
    """Load local .env values and reject obvious template placeholders."""
    load_dotenv(ROOT / ".env")
    endpoint = os.getenv("AZURE_DI_ENDPOINT", "").strip()
    key = os.getenv("AZURE_DI_KEY", "").strip()
    region = os.getenv("AZURE_DI_REGION", "").strip() or None

    missing = []
    if not endpoint or "<" in endpoint or ">" in endpoint:
        missing.append("AZURE_DI_ENDPOINT")
    if not key or key.startswith("REPLACE_") or "<" in key or ">" in key:
        missing.append("AZURE_DI_KEY")
    if missing:
        names = " and ".join(missing)
        raise ValueError(
            f"Set {names} in .env. Start by copying .env.example to .env and "
            "replacing the placeholders."
        )
    return Settings(endpoint=endpoint.rstrip("/"), key=key, region=region)

