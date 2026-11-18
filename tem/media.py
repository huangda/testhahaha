"""新聞來源的公開傾向標記。

這裡只做介面上的來源分組，不把媒體傾向當成事實判斷或預測權重。
無法明確歸類的來源一律放入「其他／中性」，避免過度標籤化。
"""

from __future__ import annotations

from .zhconv import to_simplified

BLUE_SOURCE_TERMS = (
    "聯合報",
    "联合报",
    "聯合新聞網",
    "联合新闻网",
    "udn",
    "中國時報",
    "中国时报",
    "中時新聞網",
    "中时新闻网",
    "中天新聞",
    "中天新闻",
    "中視",
    "中视",
    "中廣",
    "中广",
    "tvbs",
)
GREEN_SOURCE_TERMS = (
    "自由時報",
    "自由时报",
    "三立新聞",
    "三立新闻",
    "民視新聞",
    "民视新闻",
    "新頭殼",
    "新头壳",
    "newtalk",
    "放言",
    "芋傳媒",
    "芋传媒",
)


def classify_source(source_name: str) -> dict[str, str]:
    name = to_simplified(str(source_name or "")).replace(" ", "").casefold()
    blue = any(
        term.casefold() in name
        for term in (to_simplified(value).replace(" ", "").casefold() for value in BLUE_SOURCE_TERMS)
    )
    green = any(
        term.casefold() in name
        for term in (to_simplified(value).replace(" ", "").casefold() for value in GREEN_SOURCE_TERMS)
    )
    if blue and not green:
        return {"camp": "blue", "label": "偏蓝来源"}
    if green and not blue:
        return {"camp": "green", "label": "偏绿来源"}
    return {"camp": "other", "label": "其他／中性"}
