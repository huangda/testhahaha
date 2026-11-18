"""設定載入：JSON 設定檔 + 環境變數。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .paths import CONFIG_DIR


def _load_json(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"缺少設定檔：{path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class Region:
    code: str
    name: str
    aliases: list[str]
    type: str
    polymarket_slug: str | None = None
    is_nationwide: bool = False

    @property
    def keywords(self) -> list[str]:
        return [self.name, *self.aliases]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "aliases": self.aliases,
            "type": self.type,
            "polymarket_slug": self.polymarket_slug,
            "is_nationwide": self.is_nationwide,
        }


@dataclass
class Settings:
    regions: list[Region] = field(default_factory=list)
    nationwide: Region | None = None
    feeds: list[dict[str, Any]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    election_keywords: list[str] = field(default_factory=list)
    strong_election_keywords: list[str] = field(default_factory=list)
    taiwan_exclusive_election_keywords: list[str] = field(default_factory=list)
    taiwan_context_keywords: list[str] = field(default_factory=list)
    priority_local_candidates: list[dict[str, Any]] = field(default_factory=list)
    name_map: dict[str, str] = field(default_factory=dict)
    election: str = ""
    election_date: str = ""
    polymarket_tag: str = "taiwan-elections"

    # LLM
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: int = 90

    # Newswen / 新文易數
    tag_observe_api_key: str = ""

    # YouTube Data API v3（官方公开视频与评论）
    youtube_api_key: str = ""

    # TikHub 社交媒体公开内容
    tikhub_api_key: str = ""

    # Facebook 公开页面（Playwright，开源免费）
    facebook_cookies: str = ""
    facebook_headless: bool = True

    # Forecast
    # 相容舊介面欄位；新融合模型固定為 10%，不再由此項調節。
    sentiment_weight: float = 0.10
    min_articles_for_signal: int = 3


    def region_by_name(self, name: str) -> Region | None:
        for r in self.regions:
            if name in (r.code, r.name):
                return r
        if self.nationwide and name in (self.nationwide.code, self.nationwide.name):
            return self.nationwide
        return None

    def region_names(self) -> dict[str, str]:
        """{region_code: 顯示名稱}，含全島（nationwide）。

        顯示名稱一律以設定檔為準，這樣改 config/regions.json 之後，
        歷史快照裡的舊名稱也會跟著更新，不會出現「設定改了、畫面還是舊字」。
        """
        out = {r.code: r.name for r in self.regions}
        if self.nationwide:
            out[self.nationwide.code] = self.nationwide.name
        return out

    def display_name(self, english: str) -> str:
        return self.name_map.get(english, english)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    from .dotenv import load_env_file

    load_env_file()
    regions_raw = _load_json("regions.json")
    sources_raw = _load_json("sources.json")
    name_map_raw = _load_json("name_map.json")

    settings = Settings()
    meta = regions_raw.get("meta", {})
    settings.election = meta.get("election", "")
    settings.election_date = meta.get("election_date", "")
    settings.polymarket_tag = meta.get("polymarket_tag", "taiwan-elections")

    for item in regions_raw.get("regions", []):
        settings.regions.append(
            Region(
                code=item["code"],
                name=item["name"],
                aliases=list(item.get("aliases", [])),
                type=item.get("type", "mayor"),
                polymarket_slug=item.get("polymarket_slug"),
            )
        )
    nw = regions_raw.get("nationwide")
    if nw:
        settings.nationwide = Region(
            code=nw["code"],
            name=nw["name"],
            aliases=list(nw.get("aliases", [])),
            type="nationwide",
            polymarket_slug=nw.get("polymarket_slug"),
            is_nationwide=True,
        )

    settings.feeds = [f for f in sources_raw.get("feeds", []) if f.get("enabled", True)]
    settings.queries = list(sources_raw.get("queries", []))
    settings.election_keywords = list(sources_raw.get("election_keywords", []))
    settings.strong_election_keywords = list(
        sources_raw.get("strong_election_keywords", [])
    )
    settings.taiwan_exclusive_election_keywords = list(
        sources_raw.get("taiwan_exclusive_election_keywords", [])
    )
    settings.taiwan_context_keywords = list(
        sources_raw.get("taiwan_context_keywords", [])
    )
    settings.priority_local_candidates = [
        dict(item)
        for item in sources_raw.get("priority_local_candidates", [])
        if isinstance(item, dict) and item.get("region") and item.get("name")
    ]
    settings.name_map = dict(name_map_raw.get("politicians", {}))

    settings.llm_enabled = _as_bool(os.environ.get("TEM_LLM_ENABLED"), False)
    settings.llm_base_url = os.environ.get("TEM_LLM_BASE_URL", settings.llm_base_url).rstrip("/")
    settings.llm_api_key = os.environ.get("TEM_LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    settings.llm_model = os.environ.get("TEM_LLM_MODEL", settings.llm_model)
    settings.llm_timeout = int(os.environ.get("TEM_LLM_TIMEOUT", settings.llm_timeout))
    settings.tag_observe_api_key = os.environ.get("TAG_OBSERVE_API_KEY", "")
    settings.youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "") or os.environ.get(
        "TEM_YOUTUBE_API_KEY", ""
    )
    settings.tikhub_api_key = os.environ.get("TIKHUB_API_KEY", "")
    settings.facebook_cookies = os.environ.get("FACEBOOK_COOKIES", "")
    settings.facebook_headless = _as_bool(
        os.environ.get("FACEBOOK_HEADLESS"),
        True,
    )
    if settings.llm_api_key and os.environ.get("TEM_LLM_ENABLED") is None:
        settings.llm_enabled = True

    settings.min_articles_for_signal = int(
        os.environ.get("TEM_MIN_ARTICLES", settings.min_articles_for_signal)
    )
    return settings
