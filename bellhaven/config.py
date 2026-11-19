"""Settings come from environment variables (loaded from .env). Nothing secret lives in code."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITE = "https://analyst-assessment-production.up.railway.app"

BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader so we do not need an extra dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    token: str
    api_base: str
    site_base: str
    db_path: Path
    repo_url: str = ""  # optional public GitHub URL, used only for "View source code" links

    def __repr__(self) -> str:  # never show the token, even in a traceback
        return f"Settings(api_base={self.api_base!r}, site_base={self.site_base!r}, token=***)"


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    token = os.environ.get("CRM_API_TOKEN", "").strip()
    if not token or token == "your-token-here":
        raise SystemExit("CRM_API_TOKEN is not set. Copy .env.example to .env and add your token.")
    site = os.environ.get("SITE_BASE", DEFAULT_SITE).rstrip("/")
    return Settings(
        token=token,
        api_base=os.environ.get("CRM_API_BASE", site + "/api/v1").rstrip("/"),
        site_base=site,
        db_path=Path(os.environ.get("REVIEW_DB", ROOT / "data" / "review.db")),
        repo_url=os.environ.get("GITHUB_REPO_URL", "").strip().rstrip("/"),
    )
