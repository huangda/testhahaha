"""繁體 → 簡體 轉換。

為什麼需要這一層：新聞來源（台灣媒體、Google 新聞繁中）與 Polymarket 人名對照
都是繁體，若直接改成簡體會讓關鍵詞比對失準。因此資料庫一律保存原文，
只在**輸出層**（報告、Dashboard、CLI）轉為簡體。

兩種實作：
  1. 若環境安裝了 OpenCC（`pip install opencc-python-reimplemented`），優先使用，品質最完整。
  2. 否則使用內建字表 `tem/zhdata.py`（由 OpenCC 字典產生，單字 4105 組 + 詞組 216 組）。
轉換對已是簡體的輸入是幂等的。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from .zhdata import CHARS, PHRASES_TEXT


@lru_cache(maxsize=1)
def _char_map() -> dict[str, str]:
    if len(CHARS) % 2:
        raise ValueError("內建字表長度異常，應為 2 的倍數")
    return {CHARS[i]: CHARS[i + 1] for i in range(0, len(CHARS), 2)}


@lru_cache(maxsize=1)
def _phrase_map() -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in PHRASES_TEXT.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        src, _, dst = line.partition("=")
        if src and dst and src != dst:
            pairs[src] = dst
    return pairs


@lru_cache(maxsize=1)
def _phrase_keys() -> tuple[str, ...]:
    # 長詞優先，避免短詞先命中造成誤轉
    return tuple(sorted(_phrase_map(), key=len, reverse=True))


_opencc = None
_opencc_checked = False


def _get_opencc():
    global _opencc, _opencc_checked
    if not _opencc_checked:
        _opencc_checked = True
        if not os.environ.get("TEM_NO_OPENCC"):
            try:  # pragma: no cover - 取決於環境是否安裝
                import opencc  # type: ignore

                _opencc = opencc.OpenCC("t2s")
            except Exception:  # noqa: BLE001 - 沒安裝就退回內建字表
                _opencc = None
    return _opencc


def to_simplified(text: str | None) -> str:
    """繁體轉簡體；已是簡體者不變。"""
    if not text:
        return text or ""

    converter = _get_opencc()
    if converter is not None:  # pragma: no cover - 有安裝 OpenCC 時走這條
        try:
            return converter.convert(text)
        except Exception:  # noqa: BLE001
            pass

    chars = _char_map()
    if not any(ch in chars for ch in text):
        return text

    result = text
    phrases = _phrase_map()
    for key in _phrase_keys():
        if key in result:
            result = result.replace(key, phrases[key])
    return "".join(chars.get(ch, ch) for ch in result)


# ── 詞彙本地化 ────────────────────────────────────────────────────────────
# 台灣慣用詞 → 大陸慣用詞（左側為「繁轉簡之後」的字形）。
# 僅用於**介面文案**，不用於新聞原文引用與關鍵詞比對，
# 以免改動來源內容或影響比對準確度。
TERMS: list[tuple[str, str]] = [
    ("机率", "概率"),
    ("资料", "数据"),
    ("讯号", "信号"),
    ("汇出", "导出"),
    ("汇入", "导入"),
    ("套件", "依赖库"),
    ("介面", "界面"),
    ("档案", "文件"),
    ("影片", "视频"),
    ("萤幕", "屏幕"),
    ("专案", "项目"),
    ("使用者", "用户"),
    ("资讯", "信息"),
    ("预设", "默认"),
    ("搜寻", "搜索"),
    ("检视", "查看"),
    ("选单", "菜单"),
    ("连结", "链接"),
    ("品质", "质量"),
    ("支援", "支持"),
    ("侦测", "检测"),
    ("即时", "实时"),
    ("伺服器", "服务器"),
    ("相容", "兼容"),
    ("存取", "访问"),
    ("设定", "设置"),
    ("纪录", "记录"),
    ("栏位", "字段"),
    ("调校", "调优"),
    ("最佳化", "优化"),
    ("客制", "定制"),
    ("介接", "对接"),
    ("串接", "对接"),
    ("产出", "生成"),
    ("透过", "通过"),
    ("藉由", "借助"),
    ("或是", "或者"),
    ("硬体", "硬件"),
    ("软体", "软件"),
    ("网路", "网络"),
    ("连线", "连接"),
    ("载入", "加载"),
    ("储存", "保存"),
    ("登入", "登录"),
    ("帐号", "账号"),
    ("视窗", "窗口"),
    ("滑鼠", "鼠标"),
    ("记忆体", "内存"),
    ("频宽", "带宽"),
    ("解析度", "分辨率"),
    ("取得", "获取"),
    ("新增", "添加"),
    ("开启", "打开"),
    ("检核", "校验"),
    ("稽核", "审核"),
    ("释出", "发布"),
    ("试算", "测算"),
    ("连接埠", "端口"),
    ("埠", "端口"),
    ("行动装置", "移动设备"),
    ("简讯", "短信"),
    ("部落格", "博客"),
    ("席次", "席位"),
    # 字形相同但用語不同（OpenCC 的 t2s 不會處理，需詞組層補齊）
    ("胶著", "胶着"),
    ("沿著", "沿着"),
    ("坐著", "坐着"),
    ("看著", "看着"),
    ("跟著", "跟着"),
    ("藉著", "借着"),
    ("為著", "为着"),
    ("程式", "程序"),
    ("模组", "模块"),
    ("金钥", "密钥"),
    ("硬碟", "硬盘"),
    ("磁碟", "磁盘"),
    ("光碟", "光盘"),
    ("随身碟", "U盘"),
    ("印表机", "打印机"),
    ("主机板", "主板"),
    ("阵列", "数组"),
    ("字串", "字符串"),
    ("变数", "变量"),
    ("物件", "对象"),
    ("讯息", "消息"),
    ("组态", "配置"),
    ("除错", "调试"),
    ("回圈", "循环"),
    ("装置", "设备"),
    ("函式", "函数"),
    ("执行绪", "线程"),
    ("资料夹", "文件夹"),
    ("伫列", "队列"),
    ("网域", "域名"),
    ("印出", "输出"),
    ("连网", "联网"),
    ("约需", "大约需要"),
    ("影音", "音视频"),
    ("汇整", "汇总"),
    ("抽换", "替换"),
    ("机率", "概率"),
    ("胶著", "胶着"),
    ("台语", "闽南语"),
    ("网路", "网络"),
    ("资讯", "信息"),
]

_TERM_PAIRS: list[tuple[str, str]] | None = None


def _terms() -> list[tuple[str, str]]:
    global _TERM_PAIRS
    if _TERM_PAIRS is None:
        # 長詞優先，避免「資料庫」被「資料」先截斷
        _TERM_PAIRS = sorted(TERMS, key=lambda kv: len(kv[0]), reverse=True)
    return _TERM_PAIRS


def localize(text: str | None) -> str:
    """介面文案用：繁轉簡 + 詞彙本地化。"""
    if not text:
        return text or ""
    result = to_simplified(text)
    for traditional, mainland in _terms():
        if traditional in result:
            result = result.replace(traditional, mainland)
    return result


def deep_simplify(value):
    """遞迴轉換 dict / list / str，用於 API 與報告輸出。"""
    if isinstance(value, str):
        return to_simplified(neutralize_scope(value))
    if isinstance(value, dict):
        return {k: deep_simplify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_simplify(v) for v in value]
    return value


# 「全國／全国」在本系統一律指涉台灣本島範圍，輸出時統一改稱「全島」；
# 但民調機構「全國公信力」是專有名詞，必須原樣保留。
_SCOPE_T = re.compile(r"全國(?!公信力)")
_SCOPE_S = re.compile(r"全国(?!公信力)")


def neutralize_scope(text: str | None) -> str:
    """把範圍用語「全國／全国」改成「全島／全岛」，機構名「全國公信力」不受影響。"""
    if not text:
        return text or ""
    return _SCOPE_S.sub("全岛", _SCOPE_T.sub("全島", text))


def deep_neutralize_scope(value):
    """遞迴版：用於輸出前整理 dict / list / str（不改變字形，只換範圍用語）。"""
    if isinstance(value, str):
        return neutralize_scope(value)
    if isinstance(value, dict):
        return {k: deep_neutralize_scope(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_neutralize_scope(v) for v in value]
    return value


def preferred_lang() -> str:
    return os.environ.get("TEM_LANG", "zh-Hans").strip().lower()


def is_simplified_preferred() -> bool:
    return preferred_lang() in {"zh-hans", "zh_cn", "zh-cn", "simplified", "hans", "s", "cn"}


def display(text: str | None) -> str:
    """依 TEM_LANG 決定輸出字形；預設簡體。"""
    return to_simplified(text) if is_simplified_preferred() else (text or "")
