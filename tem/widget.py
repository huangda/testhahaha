"""可嵌入网页的候选人赔率卡片生成器。

输出：
  - `tem-widget.js`：自带候选人赔率、民调、舆情和模型说明的零依赖组件
  - `tem-widget-demo.html`：可直接打开的演示页
"""

from __future__ import annotations

import concurrent.futures as futures
import json
from typing import Any

from .config import Settings
from .models import MarketSnapshot

WIDGET_CLASS = "temx"


def collect_widget_data(
    snapshots: dict[str, MarketSnapshot],
    settings: Settings,
    *,
    min_price: float = 0.01,
    max_candidates: int = 60,
    workers: int = 6,
    allow_network: bool = True,
) -> list[dict[str, Any]]:
    """整理候选人卡片所需的赔率、民调、舆情和档案数据。"""
    targets: list[tuple[str, Any]] = []
    for code, snapshot in snapshots.items():
        for cand in snapshot.candidates:
            if (cand.yes_price or 0) < min_price:
                continue
            targets.append((code, cand))
    targets.sort(key=lambda item: -(item[1].yes_price or 0))
    targets = targets[:max_candidates]

    if allow_network:
        try:
            from .profile import prefetch

            prefetch([(cand.name_zh, cand.name_en) for _code, cand in targets])
        except Exception:  # noqa: BLE001 - 档案抓取失败不影响卡片生成
            pass

    def build(job: tuple[str, Any]) -> dict[str, Any]:
        code, cand = job
        profile = None
        try:
            from .profile import resolve_profile

            profile = resolve_profile(
                snapshots[code].region_name,
                cand.name_zh,
                cand.name_en,
                allow_network=allow_network,
            )
        except Exception:  # noqa: BLE001
            profile = None

        signals: dict[str, Any] = {}
        try:
            from .forecast import candidate_signals

            signals = candidate_signals(
                snapshots[code].region_name,
                cand.name_zh,
                cand.name_en,
                cand.market_id,
            )
        except Exception:  # noqa: BLE001
            signals = {}

        return {
            "region": code,
            "region_name": snapshots[code].region_name,
            "candidate_en": cand.name_en,
            "candidate_zh": cand.name_zh,
            "image_url": cand.image_url,
            "price": cand.yes_price,
            "chg_1d": cand.chg_1d,
            "chg_1w": cand.chg_1w,
            "volume": cand.volume,
            "event_slug": snapshots[code].event_slug,
            "profile": profile,
            "poll": signals.get("poll"),
            "attention": signals.get("attention") or {},
            "depth": signals.get("depth") or {},
            "model": signals.get("model") or {},
        }

    out: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        out.extend(pool.map(build, targets))
    out.sort(key=lambda item: -(item["price"] or 0))
    return out


_JS_TEMPLATE = r"""
/*! 台湾九合一选举 · Polymarket 候选人赔率卡片  v__VERSION__
 *  资料 {generated_at} · 来源 Polymarket Gamma API
 *  用法：<div id="tem-widget"></div><script src="tem-widget.js"></script>
 */
(function () {
  'use strict';
  var SCRIPT = document.currentScript;
  var ATTR = function (name, fallback) {
    return (SCRIPT && SCRIPT.getAttribute(name)) || fallback;
  };
  var API = ATTR('data-api', '').replace(/\/$/, '');
  var TARGET = ATTR('data-target', '#tem-widget');
  var LIGHT = ATTR('data-theme', 'dark') === 'light';
  var LIMIT = parseInt(ATTR('data-limit', '0'), 10) || 0;
  var DATA = __DATA__;
  var C = {
    bg: LIGHT ? '#ffffff' : '#0e1320',
    bg2: LIGHT ? '#f6f8fc' : '#141b2b',
    line: LIGHT ? '#e4e9f2' : 'rgba(118,160,255,.16)',
    txt: LIGHT ? '#111827' : '#e9eefb',
    muted: LIGHT ? '#6b7280' : '#8794ad',
    dim: LIGHT ? '#9ca3af' : '#4d5872',
    up: '#10b981',
    down: '#ef4444',
    accent: '#3b9dff'
  };

  function css() {
    if (document.getElementById('temx-style')) return;
    var style = document.createElement('style');
    style.id = 'temx-style';
    style.textContent = [
      '.temx-grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));',
      'font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}',
      '.temx-card{position:relative;padding:13px 14px;border-radius:14px;cursor:pointer;overflow:hidden;',
      'background:' + C.bg2 + ';border:1px solid ' + C.line + ';transition:transform .18s,box-shadow .18s,border-color .18s}',
      '.temx-card:hover{transform:translateY(-3px);border-color:rgba(76,141,255,.55);',
      'box-shadow:0 16px 34px -20px rgba(76,141,255,.95)}',
      '.temx-head{display:flex;align-items:center;gap:9px;min-width:0}',
      '.temx-av{position:relative;width:36px;height:36px;flex:none}',
      '.temx-av span{position:absolute;inset:0;display:grid;place-items:center;border-radius:50%;',
      'background:rgba(118,160,255,.16);color:' + C.muted + ';font-size:12px;font-weight:600}',
      '.temx-av img{position:absolute;inset:0;width:100%;height:100%;border-radius:50%;object-fit:cover;',
      'border:1.5px solid rgba(118,160,255,.4)}',
      '.temx-nm{font-size:13.5px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:' + C.txt + '}',
      '.temx-rg{font-size:10.5px;color:' + C.muted + ';margin-top:2px}',
      '.temx-price{margin-top:12px;font-family:ui-monospace,monospace;font-size:22px;font-weight:650;color:' + C.accent + '}',
      '.temx-chg{font-size:11px;margin-top:2px}',
      '.temx-meta{margin-top:9px;padding-top:8px;border-top:1px solid ' + C.line + ';',
      'font-size:10.5px;color:' + C.dim + '}',
      '.temx-pop{position:fixed;z-index:2147483000;box-sizing:border-box;width:min(390px,calc(100vw - 20px));',
      'max-height:calc(100vh - 20px);overflow:auto;border-radius:16px;background:' + C.bg + ';',
      'border:1px solid ' + C.line + ';color:' + C.txt + ';box-shadow:0 30px 70px -26px rgba(0,0,0,.95);',
      'font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;',
      'opacity:0;transform:translateY(6px);pointer-events:none;',
      'transition:opacity .16s ease,transform .16s cubic-bezier(.22,1,.36,1)}',
      '.temx-pop.on{opacity:1;transform:none;pointer-events:auto}',
      '.temx-pop.temx-sheet{left:8px;right:8px;width:auto;bottom:8px;top:auto!important;max-height:72vh}',
      '.temx-ph{display:flex;gap:11px;align-items:center;padding:14px 15px 10px}',
      '.temx-ph .nm{font-size:15px;font-weight:650;line-height:1.25}',
      '.temx-ph .rg{font-size:11.5px;color:' + C.muted + ';margin-top:2px}',
      '.temx-winrate{margin-left:auto;text-align:right;white-space:nowrap}',
      '.temx-winrate .l{font-size:10px;color:' + C.muted + '}',
      '.temx-winrate b{display:block;margin-top:2px;font:650 21px/1.1 ui-monospace,monospace;color:' + C.accent + '}',
      '.temx-x{margin-left:4px;color:' + C.dim + ';cursor:pointer;font-size:16px;padding:4px 2px}',
      '.temx-prof{padding:0 15px 10px}',
      '.temx-pchips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}',
      '.temx-pchip{font-size:11px;padding:2px 9px;border-radius:99px;border:1px solid ' + C.line + ';',
      'color:' + C.muted + ';background:rgba(118,160,255,.07);white-space:nowrap}',
      '.temx-pchip.party{color:#fff;border-color:transparent;background:var(--pc,#8794ad);font-weight:600}',
      '.temx-bio{font-size:12px;line-height:1.75;color:' + C.muted + ';',
      'display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}',
      '.temx-bio.open{-webkit-line-clamp:unset;max-height:168px;overflow-y:auto}',
      '.temx-prow{display:flex;align-items:center;gap:10px;margin-top:6px}',
      '.temx-more{font-size:11px;color:' + C.accent + ';cursor:pointer}',
      '.temx-wlink{font-size:11px;margin-left:auto;color:' + C.accent + ';text-decoration:none}',
      '.temx-row{display:flex;gap:7px;align-items:baseline;flex-wrap:wrap;padding:0 15px 10px;',
      'font-size:11.5px;color:' + C.muted + '}',
      '.temx-row .t{color:' + C.txt + ';font-weight:600}',
      '.temx-row b{color:#fbbf24;font-family:ui-monospace,monospace}',
      '.temx-note{padding:0 15px 9px;font-size:10.5px;color:' + C.dim + ';line-height:1.65}',
      '.temx-note.warn{color:#f0a63c}',
      '.temx-foot{display:flex;align-items:center;gap:10px;padding:10px 15px 12px;',
      'border-top:1px solid ' + C.line + ';font-size:11.5px;color:' + C.muted + '}',
      '.temx-foot a{margin-left:auto;color:' + C.accent + ';text-decoration:none;font-weight:600}',
      '.temx-load,.temx-empty{padding:40px 15px;text-align:center;color:' + C.muted + ';font-size:12.5px}'
    ].join('');
    document.head.appendChild(style);
  }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
    });
  }
  function pct(value) { return value == null ? '—' : (value * 100).toFixed(1) + '%'; }
  function usd(value) { return value == null ? '—' : '$' + Math.round(value).toLocaleString('en-US'); }
  function signed(value) { return Number(value) >= 0 ? '+' : ''; }
  function changeHtml(value) {
    if (value == null) return '<span style="color:' + C.dim + '">暂无涨跌</span>';
    var color = value >= 0 ? C.up : C.down;
    return '<span style="color:' + color + '">' + (value >= 0 ? '▲' : '▼') + ' ' +
      Math.abs(value * 100).toFixed(1) + '%</span>';
  }
  function ageFrom(birth) {
    if (!birth) return '';
    var date = new Date(birth + 'T00:00:00Z');
    if (isNaN(date.getTime())) return '';
    var now = new Date();
    var age = now.getUTCFullYear() - date.getUTCFullYear();
    var month = now.getUTCMonth() - date.getUTCMonth();
    if (month < 0 || (month === 0 && now.getUTCDate() < date.getUTCDate())) age--;
    return (age > 0 && age < 130) ? ' · ' + age + ' 岁' : '';
  }
  function profileBlock(profile) {
    if (!profile || !profile.bio) return '';
    var chips = [];
    if (profile.party) chips.push('<span class="temx-pchip party" style="--pc:' +
      esc(profile.party_color || '#8794ad') + '">' + esc(profile.party) + '</span>');
    if (profile.birth) chips.push('<span class="temx-pchip">' + esc(profile.birth) + ageFrom(profile.birth) + '</span>');
    if (profile.office) chips.push('<span class="temx-pchip">现任' + esc(profile.office) + '</span>');
    return '<div class="temx-prof"><div class="temx-pchips">' + chips.join('') + '</div>' +
      '<div class="temx-bio">' + esc(profile.bio) + '</div>' +
      '<div class="temx-prow"><span class="temx-more">展开全文</span>' +
      (profile.wiki_url ? '<a class="temx-wlink" href="' + esc(profile.wiki_url) +
        '" target="_blank" rel="noopener">维基百科 →</a>' : '') + '</div></div>';
  }
  function pollBlock(poll) {
    if (!poll || (poll.weighted == null && !poll.matchup)) return '';
    var sentence = '';
    if (poll.leader && poll.margin != null) {
      sentence = '近期加权民调中，' + esc(poll.leader) + '领先' + Number(poll.margin).toFixed(1) + '个百分点';
      if (poll.weighted != null) sentence += '，该候选人的加权支持度为' + Number(poll.weighted).toFixed(1) + '%';
    } else if (poll.weighted != null) {
      sentence = '近期加权民调中，该候选人的支持度为' + Number(poll.weighted).toFixed(1) + '%';
    } else {
      sentence = '近期有可用的对战民调，但尚未形成稳定的加权结果';
    }
    if (poll.sources) sentence += '（共' + poll.sources + '份' + (poll.date ? '，更新至' + esc(poll.date) : '') + '）';
    return '<div class="temx-row"><span class="t">📊 民调</span><span>' + sentence + '。</span></div>';
  }
  function attentionBlock(attention) {
    if (!attention) return '';
    var ptt = attention.ptt || {}, wiki = attention.wikipedia || {}, bits = [];
    if (ptt.mentions) bits.push('PTT 近期 <b>' + ptt.mentions + '</b> 篇');
    if (wiki.views_7d != null) bits.push('维基 7 日均 <b>' + Math.round(wiki.views_7d).toLocaleString() + '</b> 次');
    if (wiki.momentum != null) bits.push('关注动能 <b>' + Number(wiki.momentum).toFixed(2) + '×</b>');
    return bits.length ? '<div class="temx-row"><span class="t">👀 关注度</span>' +
      bits.join('<span>·</span>') + '</div>' : '';
  }
  function depthBlock(depth) {
    if (!depth || !Object.keys(depth).length) return '';
    var bits = [];
    if (depth.spread_pts != null) bits.push('买卖报价平均相差 <b>' + Number(depth.spread_pts).toFixed(1) + '</b> 个百分点');
    if (depth.depth_usdc != null) bits.push('报价上下5%范围内约有 <b>' + usd(depth.depth_usdc) + '</b> 可成交');
    if (depth.flow_imbalance != null) {
      var flow = Number(depth.flow_imbalance) * 100;
      bits.push('近24小时主动' + (flow >= 0 ? '买盘比卖盘多' : '卖盘比买盘多') + ' <b>' + Math.abs(flow).toFixed(0) + '%</b>');
    }
    if (depth.top_share != null) bits.push('最大单一账户持仓占 <b>' + (Number(depth.top_share) * 100).toFixed(0) + '%</b>');
    var warn = depth.depth_usdc != null && depth.depth_usdc < 3000
      ? '<div class="temx-note warn">交易金额较少，赔率容易被少量资金推动，建议结合民调和新闻一起看。</div>' : '';
    return bits.length ? '<div class="temx-row"><span class="t">📉 市场交易情况</span>' +
      bits.join('<span>·</span>') + '</div><div class="temx-note">这些数字只反映市场交易是否活跃，不等于候选人的胜率。</div>' + warn : '';
  }
  function modelBlock(model, name) {
    if (!model || model.fused == null) return '';
    var components = model.components || {};
    var bits = [];
    if (model.market != null) bits.push('市场 <b>' + pct(model.market) + '</b>');
    if (model.poll_prob != null) bits.push('民调 <b>' + pct(model.poll_prob) + '</b>');
    bits.push('综合 <b>' + pct(model.fused) + '</b>');
    var summary = String(components.ai_region_summary || '').trim();
    var priority = components.reference_priority || '市场优先';
    var unified = components.calculation_method === 'ai_unified_final';
    var anchored = unified || components.calculation_method === 'anchored_event_adjustment';
    var basis = unified
      ? 'AI在民调权重 ' + Math.round(Number(components.poll_weight || 0) * 100) +
        '%、市场权重 ' + Math.round(Number(components.market_weight || 0) * 100) +
        '% 的基础胜率上，综合民调委托属性、新闻事件与资料质量给出最终胜率。'
      : anchored
      ? '基础胜率按民调权重 ' + Math.round(Number(components.poll_weight || 0) * 100) +
        '%、市场权重 ' + Math.round(Number(components.market_weight || 0) * 100) +
        '% 计算；AI只对明确事件作最多±5个百分点校正。'
      : (priority === '民调优先'
      ? '本次民调较新、覆盖较多，而市场盘口质量相对较弱，因此以民调为主要依据，市场用于校正，近期新闻舆情只作小幅参考。'
      : (priority === '市场与民调并重'
        ? '本次市场和民调均有参考价值，两项交叉校正，近期新闻舆情只作小幅参考。'
        : '本次民调缺失、偏旧或覆盖不足，因此以市场赔率为主要依据，再用民调校正，近期新闻舆情只作小幅参考。'));
    var note = '<div class="temx-note"><b>综合胜率说明：</b>' +
      esc(name || '该候选人') + '的综合胜率为 ' + pct(model.fused) +
      '。' + basis +
      (summary ? ' 选区判断：' + esc(summary) : '') + '</div>';
    return '<div class="temx-row"><span class="t">🎯 综合胜率</span>' +
      bits.join('<span>·</span>') + '</div>' + note;
  }

  var pop = null, current = null, closeTimer = null, openTimer = null, cache = {};

  function ensurePop() {
    if (pop) return pop;
    pop = document.createElement('div');
    pop.className = 'temx-pop';
    pop.addEventListener('mouseenter', function () { clearTimeout(closeTimer); });
    pop.addEventListener('mouseleave', scheduleClose);
    document.body.appendChild(pop);
    return pop;
  }
  function scheduleClose() {
    clearTimeout(closeTimer);
    closeTimer = setTimeout(hidePop, 200);
  }
  function hidePop() {
    clearTimeout(openTimer);
    if (pop) pop.classList.remove('on');
    current = null;
  }
  function positionPop(anchor) {
    if (!anchor || anchor.isConnected === false) { hidePop(); return; }
    if (window.innerWidth < 640) { pop.classList.add('temx-sheet'); return; }
    pop.classList.remove('temx-sheet');
    var rect = anchor.getBoundingClientRect();
    var width = pop.offsetWidth || 390, height = pop.offsetHeight || 360;
    var gap = 12, vw = window.innerWidth, vh = window.innerHeight;
    var spots = [
      { x: rect.right + gap, y: rect.top + rect.height / 2 - height / 2, fits: rect.right + gap + width <= vw - 8 },
      { x: rect.left - width - gap, y: rect.top + rect.height / 2 - height / 2, fits: rect.left - gap - width >= 8 },
      { x: rect.left + rect.width / 2 - width / 2, y: rect.bottom + gap, fits: rect.bottom + gap + height <= vh - 8 },
      { x: rect.left + rect.width / 2 - width / 2, y: rect.top - height - gap, fits: rect.top - gap - height >= 8 }
    ];
    var spot = spots.find(function (item) { return item.fits; }) || spots[2];
    pop.style.left = Math.max(8, Math.min(spot.x, vw - width - 8)) + 'px';
    pop.style.top = Math.max(8, Math.min(spot.y, vh - height - 8)) + 'px';
  }

  function openCard(anchor, item) {
    ensurePop();
    current = { anchor: anchor, item: item };
    pop.classList.add('on');
    var cached = cache[item.region + '|' + item.candidate_en];
    if (cached) {
      renderPop(cached);
      positionPop(anchor);
      return;
    }
    pop.innerHTML = '<div class="temx-load">正在读取候选人资料…</div>';
    positionPop(anchor);
    if (!API) {
      cache[item.region + '|' + item.candidate_en] = item;
      renderPop(item);
      return;
    }
    var query = 'region=' + encodeURIComponent(item.region) +
      '&candidate=' + encodeURIComponent(item.candidate_en) +
      '&region_name=' + encodeURIComponent(item.region_name || '') +
      '&zh=' + encodeURIComponent(item.candidate_zh || '');
    fetch(API + '/api/candidate?' + query)
      .then(function (response) { return response.json(); })
      .then(function (detail) {
        if (!detail || detail.error) throw new Error('详情读取失败');
        detail.event_slug = detail.event_slug || item.event_slug;
        detail.volume = item.volume;
        cache[item.region + '|' + item.candidate_en] = detail;
        if (current && current.anchor === anchor) renderPop(detail);
      })
      .catch(function () {
        cache[item.region + '|' + item.candidate_en] = item;
        if (current && current.anchor === anchor) renderPop(item);
      });
  }

  function renderPop(item) {
    if (!current) return;
    var name = item.candidate_zh || item.candidate_en || '候选人';
    var image = item.image_url
      ? '<div class="temx-av" style="width:44px;height:44px"><span>' + esc(name.charAt(0)) +
        '</span><img src="' + esc(item.image_url) + '" alt="' + esc(name) + '" onerror="this.remove()"></div>'
      : '<div class="temx-av" style="width:44px;height:44px"><span>' + esc(name.charAt(0)) + '</span></div>';
    var market = item.event_slug
      ? '<a href="https://polymarket.com/event/' + esc(item.event_slug) +
        '" target="_blank" rel="noopener">打开 Polymarket 市场 →</a>' : '';
    var fused = item.model && item.model.fused != null ? pct(item.model.fused) : '—';
    pop.innerHTML =
      '<div class="temx-ph">' + image +
        '<div><div class="nm">' + esc(name) + '</div>' +
        '<div class="rg">' + esc(item.region_name || item.region || '') + ' · Polymarket</div></div>' +
        '<div class="temx-winrate"><div class="l">综合胜率</div><b>' + fused + '</b></div>' +
        '<div class="temx-x" title="关闭">✕</div></div>' +
      profileBlock(item.profile) + pollBlock(item.poll) +
      attentionBlock(item.attention) + depthBlock(item.depth) +
      modelBlock(item.model, name) +
      '<div class="temx-foot"><span>累计成交 ' + usd(item.volume) + '</span>' + market + '</div>';
    var close = pop.querySelector('.temx-x');
    if (close) close.onclick = function (event) { event.stopPropagation(); hidePop(); };
    var more = pop.querySelector('.temx-more');
    if (more) more.onclick = function (event) {
      event.stopPropagation();
      var bio = pop.querySelector('.temx-bio');
      var opened = bio.classList.toggle('open');
      more.textContent = opened ? '收起' : '展开全文';
    };
    positionPop(current.anchor);
  }

  function render(root, items) {
    css();
    if (!items.length) {
      root.innerHTML = '<div class="temx-empty">没有可显示的候选人赔率</div>';
      return;
    }
    var html = '<div class="temx-grid">';
    items.forEach(function (item, index) {
      var change = item.chg_1w != null ? item.chg_1w : item.chg_1d;
      html += '<div class="temx-card" data-i="' + index + '">' +
        '<div class="temx-head"><div class="temx-av"><span>' +
          esc((item.candidate_zh || '?').charAt(0)) + '</span>' +
          (item.image_url ? '<img src="' + esc(item.image_url) + '" alt="' +
            esc(item.candidate_zh) + '" loading="lazy" onerror="this.remove()">' : '') +
        '</div><div style="min-width:0"><div class="temx-nm">' + esc(item.candidate_zh) +
          '</div><div class="temx-rg">' + esc(item.region_name) + '</div></div></div>' +
        '<div class="temx-price">' + pct(item.price) + '</div>' +
        '<div class="temx-chg">' + changeHtml(change) + '</div>' +
        '<div class="temx-meta">累计成交 ' + usd(item.volume) + '</div></div>';
    });
    root.innerHTML = html + '</div>';
    var hoverable = window.innerWidth >= 640 &&
      (!window.matchMedia || window.matchMedia('(hover: hover) and (pointer: fine)').matches);
    Array.prototype.forEach.call(root.querySelectorAll('.temx-card'), function (card) {
      var item = items[parseInt(card.getAttribute('data-i'), 10)];
      if (hoverable) {
        card.addEventListener('mouseenter', function () {
          clearTimeout(openTimer);
          clearTimeout(closeTimer);
          openTimer = setTimeout(function () { openCard(card, item); }, 120);
        });
        card.addEventListener('mouseleave', function () {
          clearTimeout(openTimer);
          scheduleClose();
        });
      }
      card.addEventListener('click', function () {
        if (!hoverable || !current || current.anchor !== card) openCard(card, item);
      });
    });
    window.addEventListener('scroll', function () {
      if (current) positionPop(current.anchor);
    }, { passive: true });
  }

  function boot() {
    var root = document.querySelector(TARGET);
    if (!root) return;
    var items = DATA.items || [];
    if (LIMIT > 0) items = items.slice(0, LIMIT);
    if (API) {
      fetch(API + '/api/widget-data')
        .then(function (response) { return response.json(); })
        .then(function (data) {
          render(root, data && data.items && data.items.length ? data.items : items);
        })
        .catch(function () { render(root, items); });
    } else {
      render(root, items);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
"""


def build_widget_js(items: list[dict[str, Any]], *, generated_at: str = "", version: str = "1.0.0") -> str:
    data = {"generated_at": generated_at, "count": len(items), "items": items}
    js = _JS_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    js = js.replace("__VERSION__", version)
    js = js.replace("{generated_at}", generated_at or "—")
    return js.strip() + "\n"


DEMO_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket 候选人赔率卡片 · 嵌入演示</title>
<style>
  body{margin:0;padding:32px;background:#080c15;color:#e9eefb;
       font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif}
  h1{font-size:20px;margin:0 0 6px}
  p{color:#8794ad;font-size:13px;margin:0 0 24px;line-height:1.8}
  code{background:rgba(34,211,238,.1);border:1px solid rgba(34,211,238,.2);
       padding:2px 7px;border-radius:5px;font-size:12px;color:#22d3ee}
  pre{background:#0e1320;border:1px solid rgba(118,160,255,.16);border-radius:10px;
      padding:14px 16px;overflow:auto;color:#9ae6b4;font-size:12px}
  hr{border:0;border-top:1px solid rgba(118,160,255,.16);margin:28px 0}
</style>
</head>
<body>
  <h1>Polymarket 候选人赔率卡片</h1>
  <p>鼠标悬停或点击卡片，可查看候选人当前赔率、民调、新闻舆情与综合胜率说明。<br>
     数据来源：Polymarket Gamma API。</p>
  <div id="tem-widget"></div>
  <hr>
  <h2 style="font-size:15px">嵌入方式</h2>
  <p>把 <code>tem-widget.js</code> 放到你的网站，然后贴上：</p>
  <pre>&lt;div id="tem-widget"&gt;&lt;/div&gt;
&lt;script src="tem-widget.js"&gt;&lt;/script&gt;</pre>
  <p>要改用即时数据，请指定后端地址：</p>
  <pre>&lt;script src="tem-widget.js" data-api="https://你的域名"&gt;&lt;/script&gt;</pre>
  <p>常用参数：<code>data-target</code> 指定容器、<code>data-limit</code> 限制卡片数、
     <code>data-theme="light"</code> 切换浅色主题。</p>
  <script src="tem-widget.js"></script>
</body>
</html>
"""
