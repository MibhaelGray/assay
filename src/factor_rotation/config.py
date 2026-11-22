import os
from pathlib import Path
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")  # the owner's zone: every time written for a reader is Central (2026-11-15)


def _load_dotenv() -> None:
    """FR_* settings from .env in the working directory, so manual runs match the scheduled ones.
    Variables already set in the environment win."""
    path = Path(".env")
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.startswith("FR_") and key not in os.environ:
            os.environ[key] = value.strip().strip('"')


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    data_root: str = field(default_factory=lambda: _env("FR_DATA_ROOT", "data"))
    # The live gateway's port. Nothing here connects to it; the paper broker refuses to run on it (paper/broker.py).
    ib_port: int = field(default_factory=lambda: int(_env("FR_IB_PORT", "4001")))


def settings() -> Settings:
    return Settings()
