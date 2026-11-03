"""Loads config.yaml, retailers.yaml, seed brands and secrets."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
PRICE_LIST_DIR = DATA_DIR / "price_lists"


def _load_dotenv() -> None:
    """Minimal .env loader for running locally (GitHub uses Secrets instead)."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def env(name: str, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise SystemExit(
            f"Missing secret {name}. Add it in GitHub: Settings -> Secrets and "
            f"variables -> Actions (or in your local .env file)."
        )
    return value


def load_yaml(name: str) -> dict:
    with open(ROOT / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config() -> dict:
    return load_yaml("config.yaml")


def load_retailers() -> dict:
    return load_yaml("retailers.yaml")


def load_seed_brands() -> list[str]:
    path = ROOT / "seed_brands.txt"
    brands = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            brands.append(line)
    return brands
