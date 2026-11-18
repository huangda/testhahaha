"""候選人個人檔案：黨籍、生日、現職、簡歷。

資料來源為中文維基百科（免金鑰）。實作上有三個關鍵點：

1. **批次抓取**：用 MediaWiki 的 `action=query` 一次查 20 個條目。
   逐條打 REST API 會被 429 限流（實測打十幾次就開始擋）。
2. **消歧義頁防護**：像「張峻」這種條目是消歧義頁，直接查會拿到別人的生平，
   必須用 `pageprops.disambiguation` 擋掉。
3. **政治人物校驗**：抓到的簡介要含政治相關字眼才採用，否則視為查錯人。

結果快取在 `data/cache/profiles/`（預設 7 天）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from typing import Any, Iterable

from .http import fetch_json
from .paths import CONFIG_DIR, DATA_DIR, ensure_dirs

WIKI_API = "https://zh.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
UA = {"User-Agent": "TEM-election-monitor/1.0 (local research tool; python-urllib)"}
CACHE_TTL = 7 * 24 * 3600
MISS_TTL = 6 * 3600  # 查不到時只快取 6 小時，避免一次限流被記住一週
WIKIDATA_TTL = 30 * 24 * 3600  # 黨籍幾乎不變，反查結果可以放久一點
WIKIDATA_MISS_TTL = 1800  # 查不到（或剛好被限流）只記 30 分鐘，稍後自動重試
BATCH = 20  # MediaWiki 對 exintro 的 exlimit 上限

PARTIES = [
    "中國國民黨", "民主進步黨", "台灣民眾黨", "時代力量", "親民黨", "新黨",
    "台灣基進", "台灣團結聯盟", "社會民主黨", "綠黨", "民國黨", "無黨團結聯盟",
    "台灣維新", "小民參政歐巴桑聯盟", "勞動黨", "中華統一促進黨", "台灣工黨",
]
PARTY_COLORS = {
    "中國國民黨": "#3b82f6",
    "民主進步黨": "#22c55e",
    "台灣民眾黨": "#22d3ee",
    "時代力量": "#f59e0b",
    "親民黨": "#f97316",
    "新黨": "#eab308",
    "台灣基進": "#0ea5e9",
    "台灣團結聯盟": "#14b8a6",
    "社會民主黨": "#ec4899",
    "綠黨": "#84cc16",
    "民國黨": "#a3a3a3",
    "無黨團結聯盟": "#94a3b8",
    "台灣維新": "#8b8cf0",
    "小民參政歐巴桑聯盟": "#f472b6",
    "勞動黨": "#ef4444",
    "中華統一促進黨": "#facc15",
    "台灣工黨": "#a855f7",
    "無黨籍": "#8794ad",
}

# 維基簡介常寫簡稱（民進黨籍、國民黨籍），一律回傳全稱
PARTY_ALIASES = {
    "民進黨": "民主進步黨",
    "國民黨": "中國國民黨",
    "民眾黨": "台灣民眾黨",
    "台聯": "台灣團結聯盟",
    "基進黨": "台灣基進",
    "時力": "時代力量",
    "社民黨": "社會民主黨",
}

# 簡介要出現這些字眼，才認定是被查的那位政治人物
POLITICS_HINTS = (
    "政治人物", "政治", "立法委員", "立委", "市長", "縣長", "議員", "議長",
    "黨籍", "黨員", "選舉", "主席", "院長", "部長", "鄉長", "鎮長", "代表",
    "政務", "候選人", "從政",
)

_BIRTH_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_OFFICE_RE = re.compile(r"現任([^，。；、（）]{2,22})")
_mem: dict[str, tuple[float, dict[str, Any] | None]] = {}
_council_search_mem: dict[str, tuple[float, dict[str, Any] | None]] = {}


def cache_dir():
    return DATA_DIR / "cache" / "profiles"


def _key(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:20]


# ── 解析 ──────────────────────────────────────────────────────────────────

# 派系／職務寫法：這些字接在黨名後面時，代表前面那個黨就是本人的黨籍
_PARTY_FACTION = (
    "新潮流", "派系", "中央", "黨團", "黨部", "秘書長", "主席", "立法院",
    "提名", "成員", "黨員", "不分區", "中常委", "正副",
)
_FACTION_RE = re.compile("(?:" + "|".join(_PARTY_FACTION) + ")")


def _followed_by_role(text: str, party: str) -> bool:
    """黨名後面緊接派系或黨職字眼時，認定為本人的黨籍。"""
    return bool(re.search(re.escape(party) + _FACTION_RE.pattern, text))


def parse_party(extract: str) -> str:
    """從維基簡介抓黨籍。

    標準寫法是「民主進步黨籍」，但實務上常見三種變體：
      * 簡稱：民進黨籍、國民黨籍
      * 派系／職務寫法：民主進步黨新潮流系成員（整句沒有「籍」字）
      * 無黨籍 / 無黨派
    """
    if not extract:
        return ""
    head = extract[:180]
    for party in PARTIES:
        if f"{party}籍" in extract or f"{party}黨籍" in extract:
            return party
    for short, full in PARTY_ALIASES.items():
        if f"{short}籍" in extract:
            return full
    if "無黨籍" in extract or "無黨派" in extract or "無黨無派" in extract:
        return "無黨籍"
    m = re.search(r"([\u4e00-\u9fff]{2,10}黨)籍", extract)
    if m:
        return m.group(1)
    for party in PARTIES:
        if party in head and _followed_by_role(head, party):
            return party
    for short, full in PARTY_ALIASES.items():
        if short in head and _followed_by_role(head, short):
            return full
    return ""


def parse_birth(extract: str) -> str:
    m = _BIRTH_RE.search(extract[:160])
    if not m:
        return ""
    year, month, day = (int(x) for x in m.groups())
    if not (1900 <= year <= 2010):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_office(extract: str) -> str:
    m = _OFFICE_RE.search(extract)
    return m.group(1).strip()[:22] if m else ""


def looks_like_politician(extract: str, requested: str) -> bool:
    """擋掉消歧義、同名藝人、查錯人。"""
    if not extract or len(extract) < 20:
        return False
    if f"{requested}可以指" in extract[:40]:
        return False
    return any(hint in extract[:400] for hint in POLITICS_HINTS)


def _page_to_profile(
    page: dict[str, Any],
    requested: str,
    *,
    require_political: bool = True,
) -> dict[str, Any] | None:
    props = page.get("pageprops") or {}
    if "disambiguation" in props:          # 消歧義頁，直接放棄
        return None
    extract = (page.get("extract") or "").strip()
    if require_political and not looks_like_politician(extract, requested):
        return None
    party = parse_party(extract)
    title = page.get("title") or requested
    return {
        "title": title,
        "bio": extract,
        "party": party,
        "party_color": PARTY_COLORS.get(party, "#8794ad"),
        "birth": parse_birth(extract),
        "office": parse_office(extract),
        "photo": ((page.get("thumbnail") or {}).get("source")) or "",
        "wiki_url": "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
        "wikidata": props.get("wikibase_item") or "",
        "source": "zh.wikipedia",
    }


# ── 抓取 ──────────────────────────────────────────────────────────────────

def _query(
    titles: list[str],
    *,
    redirects: bool = True,
    timeout: int = 30,
    retries: int = 2,
) -> dict[str, dict[str, Any]]:
    """回傳 {最終標題: page}。"""
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "extracts|pageimages|pageprops",
        "exintro": "1",
        "explaintext": "1",
        "exlimit": "max",
        "piprop": "thumbnail",
        "pithumbsize": "240",
        "titles": "|".join(titles),
    }
    if redirects:
        params["redirects"] = "1"
    data = fetch_json(
        f"{WIKI_API}?{urllib.parse.urlencode(params)}",
        timeout=timeout,
        retries=retries,
        headers=UA,
    )
    query = data.get("query") or {}
    pages = {p.get("title", ""): p for p in (query.get("pages") or [])}

    # 把「要求查的名字」對到「實際的條目標題」（經過正規化與重定向）
    alias: dict[str, str] = {}
    for item in query.get("normalized") or []:
        alias[(item.get("from") or "").replace("_", " ")] = (item.get("to") or "").replace("_", " ")
    for item in query.get("redirects") or []:
        alias[(item.get("from") or "").replace("_", " ")] = (item.get("to") or "").replace("_", " ")

    out: dict[str, dict[str, Any]] = {}
    for requested in titles:
        current = requested.replace("_", " ")
        for _ in range(4):                      # 追連鎖重定向
            nxt = alias.get(current)
            if not nxt or nxt == current:
                break
            current = nxt
        page = pages.get(current)
        if page and page.get("pageid") and not page.get("missing"):
            out[requested] = page
    return out


def fetch_profiles(pairs: Iterable[tuple[str, str]], *, delay: float = 0.4) -> dict[str, dict[str, Any] | None]:
    """pairs: [(中文名, 英文名)]。回傳 {中文名或英文名: profile|None}。"""
    pairs = list(pairs)
    result: dict[str, dict[str, Any] | None] = {}

    # 1) 有中文名的一次批次查
    zh_names = [zh for zh, _en in pairs if zh]
    pages: dict[str, dict[str, Any]] = {}
    for i in range(0, len(zh_names), BATCH):
        chunk = zh_names[i:i + BATCH]
        try:
            pages.update(_query(chunk))
        except Exception:  # noqa: BLE001 - 單批失敗其他批仍可繼續
            pass
        if i + BATCH < len(zh_names):
            time.sleep(delay)

    for zh, _en in pairs:
        page = pages.get(zh)
        result[zh or _en] = _page_to_profile(page, zh) if page else None
    return result


# ── 快取 ──────────────────────────────────────────────────────────────────

def get_profile(name_zh: str, name_en: str = "", *, ttl: int = CACHE_TTL, allow_network: bool = True):
    """取得單一候選人檔案（記憶體 → 磁碟 → 網路）。查不到回傳 None。"""
    key = _key(name_zh or name_en)
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < (ttl if hit[1] else MISS_TTL):
        return hit[1]

    ensure_dirs()
    cache_dir().mkdir(parents=True, exist_ok=True)
    path = cache_dir() / f"{key}.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - float(payload.get("fetched_at") or 0)
            limit = ttl if payload.get("profile") else MISS_TTL
            if age < limit:
                profile = payload.get("profile")
                _mem[key] = (float(payload["fetched_at"]), profile)
                return profile
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            pass

    if not allow_network:
        return None

    profile = None
    try:
        profile = fetch_profiles([(name_zh, name_en)]).get(name_zh or name_en)
    except Exception:  # noqa: BLE001
        profile = None
    save_profile(name_zh, name_en, profile)
    return profile


def save_profile(name_zh: str, name_en: str, profile: dict[str, Any] | None) -> None:
    key = _key(name_zh or name_en)
    ensure_dirs()
    cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        (cache_dir() / f"{key}.json").write_text(
            json.dumps({"fetched_at": time.time(), "profile": profile}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    _mem[key] = (time.time(), profile)


def search_council_profile(
    name: str,
    region: str = "",
    party: str = "",
    wiki_title: str = "",
    *,
    allow_network: bool = True,
) -> dict[str, Any] | None:
    """按议员姓名查找维基百科，优先精确条目，再用县市与政党辅助检索。"""
    name = str(name or "").strip()[:40]
    region = str(region or "").strip()[:30]
    party = str(party or "").strip()[:30]
    wiki_title = str(wiki_title or "").strip()[:80]
    if not name:
        return None
    cache_key = f"{name}|{region}|{party}"
    hit = _council_search_mem.get(cache_key)
    if hit and time.time() - hit[0] < (CACHE_TTL if hit[1] else MISS_TTL):
        return hit[1]

    cached = get_profile(name, allow_network=False)
    if cached:
        result = {**cached, "match_type": "cache", "requested_name": name}
        _council_search_mem[cache_key] = (time.time(), result)
        return result
    if not allow_network:
        return None

    if wiki_title:
        cached_title = get_profile(wiki_title, allow_network=False)
        if cached_title:
            result = {
                **cached_title,
                "match_type": "exact",
                "requested_name": name,
            }
            _council_search_mem[cache_key] = (time.time(), result)
            return result
        try:
            pages = _query([wiki_title, name], timeout=8, retries=1)
        except Exception:  # noqa: BLE001 - 精确条目失败时继续组合检索
            pages = {}
        profile = (
            _page_to_profile(pages[wiki_title], name, require_political=False)
            if wiki_title in pages
            else None
        )
        if not profile and name in pages:
            profile = _page_to_profile(pages[name], name, require_political=False)
        if profile:
            save_profile(wiki_title, "", profile)
            result = {**profile, "match_type": "exact", "requested_name": name}
            _council_search_mem[cache_key] = (time.time(), result)
            return result
        # 名册中的条目标题可能已改名；继续按姓名做一次维基搜索。

    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": name,
        "gsrnamespace": "0",
        "gsrlimit": "8",
        "prop": "extracts|pageimages|pageprops",
        "exintro": "1",
        "explaintext": "1",
        "exlimit": "max",
        "piprop": "thumbnail",
        "pithumbsize": "240",
        "redirects": "1",
    }
    try:
        data = fetch_json(
            f"{WIKI_API}?{urllib.parse.urlencode(params)}",
            timeout=8,
            retries=1,
            headers=UA,
        )
    except Exception:  # noqa: BLE001 - 外部检索失败时按未找到处理
        data = {}

    ranked: list[tuple[int, dict[str, Any]]] = []
    for page in ((data.get("query") or {}).get("pages") or []):
        title = str(page.get("title") or "").strip()
        if not title or name not in title:
            continue
        candidate = _page_to_profile(page, name)
        if not candidate:
            continue
        bio = str(candidate.get("bio") or "")
        score = 0
        score += 4 if title == name else (2 if title.startswith(name) else 0)
        score += 2 if region and region in bio else 0
        score += 1 if party and (party in bio or any(short in bio for short in PARTY_ALIASES if PARTY_ALIASES[short] == party)) else 0
        ranked.append((score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked:
        result = {**ranked[0][1], "match_type": "search", "requested_name": name}
        save_profile(name, "", result)
        _council_search_mem[cache_key] = (time.time(), result)
        return result

    _council_search_mem[cache_key] = (time.time(), None)
    return None


def prefetch(pairs: Iterable[tuple[str, str]], *, ttl: int = CACHE_TTL, force: bool = False) -> dict[str, int]:
    """批次補齊所有候選人檔案，回傳統計。已快取且未過期者不重抓。"""
    pairs = list(pairs)
    stats = {"total": len(pairs), "hit": 0, "fetched": 0, "missing": 0}
    todo: list[tuple[str, str]] = []
    for zh, en in pairs:
        key = _key(zh or en)
        path = cache_dir() / f"{key}.json"
        if not force and path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - float(payload.get("fetched_at") or 0)
                limit = ttl if payload.get("profile") else MISS_TTL
                if age < limit:
                    stats["hit"] += 1
                    _mem[key] = (float(payload["fetched_at"]), payload.get("profile"))
                    continue
            except (json.JSONDecodeError, OSError, KeyError, TypeError):
                pass
        todo.append((zh, en))

    if todo:
        fetched = fetch_profiles(todo)
        for zh, en in todo:
            profile = fetched.get(zh or en)
            save_profile(zh, en, profile)
            if profile:
                stats["fetched"] += 1
            else:
                stats["missing"] += 1
    return stats


# ── 英文名 → 中文名 反查（離線、需節流）──────────────────────────────────

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_POLITICIAN_HINTS = (
    "politician", "political", "mayor", "magistrate", "legislator",
    "member of", "minister", "councillor", "councilor", "activist",
)
_POLITICIAN_HINTS_ZH = (
    "政治人物", "政治", "立法委員", "立委", "市長", "縣長", "議員", "議長",
    "選舉", "候選人", "中華民國", "政黨", "黨籍", "從政",
)


def _zh_label(qid: str) -> str:
    params = urllib.parse.urlencode(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "labels",
            "languages": "zh-hant|zh-hans|zh|zh-tw|zh-cn",
            "format": "json",
        }
    )
    try:
        data = fetch_json(f"{WIKIDATA_API}?{params}", timeout=25, retries=1, headers=UA)
    except Exception:  # noqa: BLE001
        return ""
    labels = ((data.get("entities") or {}).get(qid) or {}).get("labels") or {}
    for lang in ("zh-hant", "zh-tw", "zh-hans", "zh-cn", "zh"):
        value = (labels.get(lang) or {}).get("value")
        if value:
            return value
    return ""


def resolve_names(
    english_names: Iterable[str],
    *,
    throttle: float = 1.0,
    verbose: bool = True,
) -> dict[str, dict[str, str]]:
    """用 Wikidata 反查英文名對應的中文名。

    只接受「英文標籤完全相符」且描述含政治類字眼的實體，避免張冠李戴。
    命中的結果仍需人工確認後再寫入 config/name_map.json。
    """
    out: dict[str, dict[str, str]] = {}
    for name in english_names:
        params = urllib.parse.urlencode(
            {
                "action": "wbsearchentities",
                "search": name,
                "language": "en",
                "uselang": "en",
                "type": "item",
                "limit": "5",
                "format": "json",
            }
        )
        hit_found = None
        try:
            data = fetch_json(f"{WIKIDATA_API}?{params}", timeout=25, retries=1, headers=UA)
            for hit in data.get("search") or []:
                label = (hit.get("label") or "").strip()
                desc = (hit.get("description") or "").strip()
                if label.lower() != name.lower():
                    continue
                if not any(h in desc.lower() for h in _POLITICIAN_HINTS):
                    continue
                hit_found = (hit, desc)
                break
        except Exception:  # noqa: BLE001
            hit_found = None

        if hit_found:
            hit, desc = hit_found
            zh = _zh_label(hit["id"])
            if zh:
                out[name] = {"zh": zh, "qid": hit["id"], "description": desc}
                if verbose:
                    print(f"  [ok]   {name:22s} -> {zh}  ({desc})")
            elif verbose:
                print(f"  [skip] {name:22s} 有 Wikidata 條目但缺中文標籤")
        elif verbose:
            print(f"  [miss] {name:22s} 找不到對應的政治人物")
        time.sleep(throttle)
    return out


# ── 與候選人登記名冊合併 ──────────────────────────────────────────────────

_roster_cache: dict[str, Any] = {}


def _roster():
    if "data" not in _roster_cache:
        try:
            from .roster import load_roster

            _roster_cache["data"] = load_roster()
        except Exception:  # noqa: BLE001
            _roster_cache["data"] = {"regions": {}}
    return _roster_cache["data"]


def roster_entry(region_name: str, name_zh: str = "", name_en: str = ""):
    try:
        from .roster import entry_for

        return entry_for(_roster(), region_name, name_zh, name_en)
    except Exception:  # noqa: BLE001
        return None


# ── 人工核對的補充名冊 ────────────────────────────────────────────────────

_extra_cache: dict[str, list[dict[str, Any]]] = {}


def _extra_entries() -> list[dict[str, Any]]:
    if "data" not in _extra_cache:
        try:
            payload = json.loads((CONFIG_DIR / "candidates_extra.json").read_text(encoding="utf-8"))
            _extra_cache["data"] = payload.get("entries") or []
        except (OSError, json.JSONDecodeError):
            _extra_cache["data"] = []
    return _extra_cache["data"]


def _norm_name(name: str) -> str:
    return re.sub(r"[\s\-'.]", "", (name or "").lower())


def extra_entry(region_name: str, name_zh: str = "", name_en: str = ""):
    """查人工核對的補充名冊（維基百科與登記名冊都查不到的人）。"""
    entries = _extra_entries()
    en_key = _norm_name(name_en)
    if en_key:
        for item in entries:
            if _norm_name(item.get("en") or "") == en_key:
                return item
    if name_zh:
        for item in entries:
            if item.get("name") == name_zh:
                return item
        if len(name_zh) >= 3:
            for item in entries:
                if (item.get("name") or "").startswith(name_zh):
                    return item
    return None


# ── Wikidata 黨籍反查（維基百科沒有條目時的最後一道補位）──────────────────

_wd_mem: dict[str, tuple[float, dict[str, Any] | None]] = {}


def wikidata_cache_dir():
    return DATA_DIR / "cache" / "wikidata"


def _party_by_label(label: str) -> str:
    """把 Wikidata 的政黨標籤（可能是簡體）對回我們用的繁體黨名。"""
    from .zhconv import to_simplified

    if not label:
        return ""
    simp = to_simplified(label)
    for party in PARTIES:
        if to_simplified(party) == simp:
            return party
    return ""


def _wd_get(params: dict[str, str]) -> dict[str, Any]:
    return fetch_json(f"{WIKIDATA_API}?{urllib.parse.urlencode(params)}", timeout=25, retries=1, headers=UA)


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z]+", (name or "").lower()) if t]


def _name_close(a: str, b: str) -> bool:
    """比對拼音姓名，容忍一個音節的轉寫差異。

    台灣人名在英文裡常有威妥瑪／通用／漢語三種拼法，例如
    「Lin Szu-ming」與 Wikidata 的「Lin Si-ming」其實是同一人。
    只容許「同姓氏 + 同音節數 + 每個音節首字母相同 + 最多一個音節不同」。
    """
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or len(ta) != len(tb) or ta[0] != tb[0]:
        return False
    diff = 0
    for x, y in zip(ta, tb):
        if x == y:
            continue
        diff += 1
        if x[0] != y[0]:
            return False
    return diff <= 1


def _wd_search(term: str, lang: str) -> str:
    """在 Wikidata 找標籤相符、描述含政治字眼的實體，回傳 Q id。"""
    if not term:
        return ""
    try:
        found = _wd_get(
            {
                "action": "wbsearchentities",
                "search": term,
                "language": lang,
                "uselang": lang,
                "type": "item",
                "limit": "5",
                "format": "json",
            }
        )
    except Exception:  # noqa: BLE001
        return ""
    for hit in found.get("search") or []:
        label = (hit.get("label") or "").strip()
        desc = (hit.get("description") or "").strip()
        ok = _name_close(label, term) if lang == "en" else label == term
        hints = _POLITICIAN_HINTS if lang == "en" else _POLITICIAN_HINTS_ZH
        if ok and any(h in desc.lower() for h in hints):
            return hit.get("id") or ""
    return ""


def fetch_wikidata_profile(name_en: str, name_zh: str = "") -> dict[str, Any] | None:
    """反查「中文名 + 黨籍」。

    只接受英文標籤完全相符、且描述含政治字眼的實體，避免查到同名運動員或歌手。
    英文找不到時（台灣人名常有多種拼法），再用中文名試一次。
    """
    if not name_en and not name_zh:
        return None
    qid = _wd_search(name_en, "en") if name_en else ""
    if not qid and name_zh:
        qid = _wd_search(name_zh, "zh")
    if not qid:
        return None

    try:
        entity = (_wd_get(
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "labels|claims",
                "languages": "zh-hant|zh-tw|zh-hans|zh-cn|zh|en",
                "format": "json",
            }
        ).get("entities") or {}).get(qid) or {}
    except Exception:  # noqa: BLE001
        return None

    labels = entity.get("labels") or {}
    zh = ""
    for lang in ("zh-hant", "zh-tw", "zh-hans", "zh-cn", "zh"):
        zh = ((labels.get(lang) or {}).get("value") or "").strip()
        if zh:
            break

    party_ids: list[str] = []
    for claim in (entity.get("claims") or {}).get("P102") or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        if isinstance(value, dict) and value.get("id"):
            party_ids.append(value["id"])

    party = ""
    if party_ids:
        try:
            parties = (_wd_get(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(party_ids[:5]),
                    "props": "labels",
                    "languages": "zh-hant|zh-tw|zh-hans|zh-cn|zh|en",
                    "format": "json",
                }
            ).get("entities") or {})
            for pid in party_ids[:5]:
                for lang in ("zh-hant", "zh-tw", "zh-hans", "zh-cn", "zh", "en"):
                    label = (((parties.get(pid) or {}).get("labels") or {}).get(lang) or {}).get("value")
                    party = _party_by_label(label or "")
                    if party:
                        break
                if party:
                    break
        except Exception:  # noqa: BLE001
            party = ""

    if not zh and not party:
        return None
    return {"zh": zh, "party": party, "qid": qid, "source": "wikidata"}


def wikidata_lookup(
    name_en: str,
    name_zh: str = "",
    *,
    allow_network: bool = True,
    force: bool = False,
) -> dict[str, Any] | None:
    """取得 Wikidata 反查結果（記憶體 → 磁碟 → 網路）。"""
    if not name_en and not name_zh:
        return None
    key = _key("wd:" + (name_en or name_zh).lower())
    hit = _wd_mem.get(key)
    if hit and not force:
        # 沒抓到黨籍可能只是當下被限流，只當短期結果，過幾小時再試
        limit = WIKIDATA_TTL if (hit[1] or {}).get("party") else WIKIDATA_MISS_TTL
        if time.time() - hit[0] < limit:
            return hit[1]

    path = wikidata_cache_dir() / f"{key}.json"
    if path.exists() and not force:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - float(payload.get("fetched_at") or 0)
            profile = payload.get("profile")
            limit = WIKIDATA_TTL if (profile or {}).get("party") else WIKIDATA_MISS_TTL
            if age < limit:
                _wd_mem[key] = (float(payload["fetched_at"]), profile)
                return profile
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            pass

    if not allow_network:
        return None

    profile = fetch_wikidata_profile(name_en, name_zh)
    ensure_dirs()
    wikidata_cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps({"fetched_at": time.time(), "profile": profile}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    _wd_mem[key] = (time.time(), profile)
    return profile


def resolve_profile(
    region_name: str,
    name_zh: str = "",
    name_en: str = "",
    *,
    allow_network: bool = True,
):
    """合併「維基百科個人頁」「候選人登記名冊」「補充名冊」「Wikidata」，回傳彈窗要用的個人檔案。"""
    wiki = get_profile(name_zh, name_en, allow_network=allow_network)
    entry = roster_entry(region_name, name_zh, name_en)
    extra = extra_entry(region_name, name_zh, name_en)
    roster_party = (entry or {}).get("party") or (extra or {}).get("party") or ""

    # 前三層都問不出黨籍時，才動用 Wikidata（離線只讀快取，不會卡住畫面）
    wd: dict[str, Any] = {}
    if name_en and not (wiki or {}).get("party") and not roster_party:
        wd = wikidata_lookup(name_en, name_zh, allow_network=allow_network) or {}

    if not wiki and not entry and not extra and not wd:
        return None

    out: dict[str, Any] = dict(wiki) if wiki else {
        "title": (entry or extra or {}).get("name") or wd.get("zh") or name_zh or name_en,
        "bio": "",
        "party": "",
        "birth": "",
        "office": "",
        "photo": "",
        "wiki_url": "",
        "source": "roster" if entry else "extra",
    }
    if extra:
        out["experience"] = extra.get("experience") or ""
        out["office"] = out.get("office") or extra.get("office") or ""
        if not out.get("title") or out.get("title") == name_en:
            out["title"] = extra.get("name") or out.get("title") or name_en
    if entry:
        party = out.get("party") or entry.get("party") or ""
        out["party"] = party
        out["party_color"] = PARTY_COLORS.get(party, "#8794ad")
        out["experience"] = entry.get("experience") or ""
        out["roster_name"] = entry.get("name") or ""
        if not out.get("bio"):
            bits = [f"{entry.get('name', '')}。"]
            if entry.get("party"):
                bits.append(f"政黨：{entry['party']}。")
            if entry.get("experience"):
                bits.append(f"經歷：{entry['experience']}。")
            if region_name:
                bits.append(f"{region_name}長候選人。")
            out["bio"] = "".join(bits)
            out["source"] = "roster"

    party = out.get("party") or roster_party or wd.get("party") or ""
    if wd:
        if not out.get("title") or out.get("title") == name_en:
            out["title"] = wd.get("zh") or out.get("title") or name_en
        if wd.get("zh") and not out.get("bio"):
            out["bio"] = f"{wd['zh']}，{party}。" if party else f"{wd['zh']}。"
    out["party"] = party
    out["party_color"] = PARTY_COLORS.get(party, "#8794ad")
    if not out.get("experience"):
        out["experience"] = (extra or {}).get("experience") or ""
    return out
