"""产生 Polymarket 嵌入码与前端小工具 HTML。"""

from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .models import MarketSnapshot


def snippet_for_region(snapshot: MarketSnapshot, settings: Settings) -> dict[str, Any]:
    leaders = sorted(snapshot.candidates, key=lambda c: -(c.pm_prob or 0))[:2]
    leader = leaders[0] if leaders else None
    region = settings.region_by_name(snapshot.region_code) or settings.region_by_name(snapshot.region_name)
    # 台湾用法：「新北市长选举」「南投县长选举」，故接在县市名之后只补一个「长」字
    office = "长"
    title = f"{leader.name_zh}会赢得下一届{snapshot.region_name}{office}选举吗？" if leader else snapshot.region_name

    from .polymarket import build_embed

    embed = build_embed(snapshot.event_slug, title, leader.yes_price if leader else None)
    embed.update(
        {
            "region_code": snapshot.region_code,
            "region_name": snapshot.region_name,
            "leader_en": leader.name_en if leader else None,
            "leader_zh": leader.name_zh if leader else None,
            "yes_price": leader.yes_price if leader else None,
            "pm_prob": leader.pm_prob if leader else None,
            "top_candidates": [
                {
                    "name_en": c.name_en,
                    "name_zh": c.name_zh,
                    "yes_price": c.yes_price,
                    "pm_prob": c.pm_prob,
                }
                for c in leaders
            ],
        }
    )
    return embed


def build_embeds(snapshots: dict[str, MarketSnapshot], settings: Settings) -> list[dict[str, Any]]:
    out = [snippet_for_region(s, settings) for s in snapshots.values()]
    out.sort(key=lambda x: -(x.get("pm_prob") or 0))
    return out


def build_gallery_html(embeds: list[dict[str, Any]], *, title: str = "2026 台湾九合一选举 — Polymarket 实时赔率") -> str:
    cards = []
    for item in embeds:
        pct = f"{round((item.get('yes_price') or 0) * 100)}%"
        top = item.get("top_candidates") or []
        rows = "".join(
            f'<div class="row"><span>{c["name_zh"]}</span>'
            f'<span class="pct">{round((c.get("pm_prob") or 0) * 100, 1)}%</span></div>'
            for c in top
        )
        cards.append(
            f"""    <article class="card">
      <header><h3>{item['region_name']}</h3><span class="badge">{pct}</span></header>
      <p class="lead">{item.get('leader_zh') or '—'}</p>
      {rows}
      <details>
        <summary>Polymarket 嵌入码</summary>
        <pre><code>{_escape(item['figure'])}</code></pre>
        <pre><code>{_escape(item['jsonld'])}</code></pre>
      </details>
      <a class="link" href="{item['event_url']}" target="_blank" rel="noopener">在 Polymarket 查看 →</a>
    </article>"""
        )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, "Microsoft JhengHei", sans-serif; margin: 0; padding: 24px;
         background: #0f1117; color: #e8eaf0; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #8b93a7; font-size: 13px; margin-bottom: 20px; }}
  .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }}
  .card {{ background: #171a23; border: 1px solid #262a36; border-radius: 12px; padding: 14px; }}
  .card header {{ display: flex; justify-content: space-between; align-items: center; }}
  .card h3 {{ margin: 0; font-size: 15px; }}
  .badge {{ background: #1e3a8a; color: #dbeafe; border-radius: 999px; padding: 2px 10px; font-size: 12px; }}
  .lead {{ margin: 10px 0 6px; font-size: 14px; color: #c7cbd8; }}
  .row {{ display: flex; justify-content: space-between; font-size: 13px; padding: 3px 0;
          border-bottom: 1px dashed #262a36; }}
  .pct {{ color: #7dd3fc; font-variant-numeric: tabular-nums; }}
  details {{ margin-top: 10px; }}
  summary {{ cursor: pointer; color: #8b93a7; font-size: 12px; }}
  pre {{ background: #0b0d12; padding: 10px; border-radius: 8px; overflow-x: auto;
         font-size: 11px; color: #9ae6b4; }}
  .link {{ display: inline-block; margin-top: 8px; color: #60a5fa; text-decoration: none; font-size: 13px; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <p class="sub">产生时间 <span id="ts"></span>　·　数据来源：Polymarket Gamma API</p>
  <div class="grid">
{chr(10).join(cards)}
  </div>
  <script>document.getElementById('ts').textContent = new Date().toLocaleString('zh-TW');</script>
</body>
</html>
"""


def _escape(html: str) -> str:
    return (
        html.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_js_widget(embeds: list[dict[str, Any]]) -> str:
    """可贴到任何网站的 JS 小工具：自动渲染各县市赔率卡片。"""
    data = json.dumps(
        [
            {
                "region": e["region_name"],
                "slug": e["event_url"].rsplit("/", 1)[-1],
                "leader": e.get("leader_zh"),
                "yes": e.get("yes_price"),
                "top": e.get("top_candidates") or [],
            }
            for e in embeds
        ],
        ensure_ascii=False,
    )
    return f"""<div id="tem-polymarket-widget"></div>
<script>
(function () {{
  var DATA = {data};
  var root = document.getElementById('tem-polymarket-widget');
  if (!root) return;
  var css = document.createElement('style');
  css.textContent = '#tem-polymarket-widget{{font-family:system-ui,sans-serif;display:grid;' +
    'grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}}' +
    '#tem-polymarket-widget .c{{border:1px solid #e5e7eb;border-radius:10px;padding:10px}}' +
    '#tem-polymarket-widget h4{{margin:0 0 6px;font-size:14px}}' +
    '#tem-polymarket-widget .p{{color:#2563eb;font-weight:600;font-size:18px}}';
  document.head.appendChild(css);
  DATA.forEach(function (item) {{
    var el = document.createElement('div');
    el.className = 'c';
    var pct = Math.round((item.yes || 0) * 100);
    el.innerHTML = '<h4>' + item.region + '</h4><div class="p">' + pct + '%</div>' +
      '<div style="font-size:12px;color:#6b7280">' + (item.leader || '') + '</div>' +
      '<a style="font-size:12px" target="_blank" rel="noopener" ' +
      'href="https://polymarket.com/event/' + item.slug + '">Polymarket →</a>';
    root.appendChild(el);
  }});
}})();
</script>
"""
