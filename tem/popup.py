"""独立候选人资料弹窗模块。

输出一个零依赖浏览器模块，可挂到本系统或其他候选人列表：

  <script src="tem-candidate-popup.js"></script>
  <script>
    TEMPopup.init({ api: 'https://你的域名' });
    TEMPopup.bind(document.querySelector('#someRow'), {
      region: 'NTPC',
      candidate: 'Su Chiao-hui'
    });
  </script>
"""

from __future__ import annotations

import json
from typing import Any

MODULE_JS = r"""/*! TEMPopup — 候选人赔率与资料弹窗
 *  零依赖 · 可单独使用 · 宽度自适应 · 窄屏自动转为底部面板
 */
(function (global) {
  'use strict';
  var P = 'tmpp-';
  var CFG = {
    api: '',
    data: [],
    theme: 'dark',
    gap: 12,
    openDelay: 90,
    closeDelay: 260,
    sheetBreakpoint: 640,
    openOn: 'auto'
  };
  var S = { pop: null, cur: null, closeT: null, openT: null, pinned: false, openedAt: 0, cache: {} };
  var COLORS = {
    dark: { bg:'#0e1320', bg2:'#141b2b', line:'rgba(118,160,255,.18)', txt:'#e9eefb',
            muted:'#8794ad', dim:'#4d5872', accent:'#3b9dff' },
    light: { bg:'#ffffff', bg2:'#f6f8fc', line:'#e4e9f2', txt:'#111827',
             muted:'#6b7280', dim:'#9ca3af', accent:'#2563eb' }
  };
  var C = COLORS.dark;
  var UP = '#10b981', DOWN = '#ef4444';

  function isNarrow() { return global.innerWidth < CFG.sheetBreakpoint; }
  function canHover() {
    if (CFG.openOn === 'click') return false;
    if (CFG.openOn === 'hover') return true;
    if (isNarrow()) return false;
    var media = global.matchMedia && global.matchMedia('(hover: hover) and (pointer: fine)');
    return media ? media.matches : true;
  }
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char];
    });
  }
  function pct(value) { return value == null ? '—' : (Number(value) * 100).toFixed(1) + '%'; }
  function usd(value) { return value == null ? '—' : '$' + Math.round(Number(value)).toLocaleString('en-US'); }
  function signed(value) { return Number(value) >= 0 ? '+' : ''; }
  function changeHtml(value) {
    if (value == null) return '<span style="color:' + C.dim + '">暂无涨跌</span>';
    return '<span style="color:' + (value >= 0 ? UP : DOWN) + '">' +
      (value >= 0 ? '▲' : '▼') + ' ' + Math.abs(value * 100).toFixed(1) + '%</span>';
  }

  function injectCss() {
    if (document.getElementById(P + 'style')) return;
    var style = document.createElement('style');
    style.id = P + 'style';
    style.textContent = [
      '.', P, 'pop{position:fixed;z-index:2147483000;box-sizing:border-box;',
      'width:min(390px,calc(100vw - 20px));max-height:calc(100vh - 20px);overflow:auto;',
      'border-radius:16px;background:', C.bg, ';border:1px solid ', C.line, ';color:', C.txt, ';',
      'box-shadow:0 30px 70px -26px rgba(0,0,0,.95);',
      'font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;',
      'opacity:0;transform:translateY(6px);pointer-events:none;',
      'transition:opacity .16s ease,transform .16s cubic-bezier(.22,1,.36,1)}',
      '.', P, 'pop.on{opacity:1;transform:none;pointer-events:auto}',
      '.', P, 'pop.', P, 'sheet{left:8px;right:8px;width:auto;bottom:8px;top:auto!important;max-height:72vh}',
      '.', P, 'head{display:flex;gap:11px;align-items:center;padding:14px 15px 10px}',
      '.', P, 'av{position:relative;width:44px;height:44px;flex:none}',
      '.', P, 'av span{position:absolute;inset:0;display:grid;place-items:center;border-radius:50%;',
      'background:rgba(118,160,255,.16);color:', C.muted, ';font-size:15px;font-weight:600}',
      '.', P, 'av img{position:absolute;inset:0;width:100%;height:100%;border-radius:50%;object-fit:cover;',
      'border:1.5px solid rgba(118,160,255,.4)}',
      '.', P, 'nm{font-size:15px;font-weight:650;line-height:1.25}',
      '.', P, 'rg{font-size:11.5px;color:', C.muted, ';margin-top:2px}',
      '.', P, 'winrate{margin-left:auto;text-align:right;white-space:nowrap}',
      '.', P, 'winrate .l{font-size:10px;color:', C.muted, '}',
      '.', P, 'winrate b{display:block;margin-top:2px;font:650 21px/1.1 ui-monospace,monospace;color:', C.accent, '}',
      '.', P, 'x{margin-left:4px;color:', C.dim, ';cursor:pointer;font-size:16px;line-height:1;padding:4px 2px}',
      '.', P, 'prof{padding:0 15px 10px}',
      '.', P, 'pchips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}',
      '.', P, 'pchip{font-size:11px;padding:2px 9px;border-radius:99px;border:1px solid ', C.line,
      ';color:', C.muted, ';background:rgba(118,160,255,.07);white-space:nowrap}',
      '.', P, 'pchip.party{color:#fff;border-color:transparent;background:var(--pc,#8794ad);font-weight:600}',
      '.', P, 'bio{font-size:12px;line-height:1.75;color:', C.muted,
      ';display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}',
      '.', P, 'bio.open{-webkit-line-clamp:unset;max-height:168px;overflow-y:auto}',
      '.', P, 'prow{display:flex;align-items:center;gap:10px;margin-top:6px}',
      '.', P, 'more{font-size:11px;color:', C.accent, ';cursor:pointer}',
      '.', P, 'wlink{font-size:11px;margin-left:auto;color:', C.accent, ';text-decoration:none}',
      '.', P, 'row{display:flex;gap:7px;align-items:baseline;flex-wrap:wrap;',
      'padding:0 15px 10px;font-size:11.5px;color:', C.muted, '}',
      '.', P, 'row .t{color:', C.txt, ';font-weight:600}',
      '.', P, 'row b{color:#fbbf24;font-family:ui-monospace,monospace}',
      '.', P, 'note{padding:0 15px 9px;font-size:10.5px;color:', C.dim, ';line-height:1.65}',
      '.', P, 'foot{display:flex;align-items:center;gap:10px;padding:10px 15px 12px;',
      'border-top:1px solid ', C.line, ';font-size:11.5px;color:', C.muted, '}',
      '.', P, 'foot a{margin-left:auto;color:', C.accent, ';text-decoration:none;font-weight:600}',
      '.', P, 'load{padding:40px 15px;text-align:center;color:', C.muted, ';font-size:12.5px}'
    ].join('');
    document.head.appendChild(style);
  }

  function ensurePop() {
    if (S.pop && S.pop.isConnected) return S.pop;
    injectCss();
    var element = document.createElement('div');
    element.className = P + 'pop';
    element.setAttribute('role', 'dialog');
    element.addEventListener('mouseenter', function () { clearTimeout(S.closeT); });
    element.addEventListener('mouseleave', function () { if (!S.pinned) scheduleClose(); });
    document.body.appendChild(element);
    S.pop = element;
    return element;
  }
  function scheduleClose() {
    clearTimeout(S.closeT);
    S.closeT = setTimeout(close, CFG.closeDelay);
  }
  function close() {
    clearTimeout(S.openT);
    clearTimeout(S.closeT);
    S.pinned = false;
    S.cur = null;
    if (S.pop) S.pop.classList.remove('on');
  }
  function place(anchor) {
    if (!S.pop || !anchor || !anchor.isConnected) { close(); return; }
    if (isNarrow()) { S.pop.classList.add(P + 'sheet'); return; }
    S.pop.classList.remove(P + 'sheet');
    var rect = anchor.getBoundingClientRect();
    var vw = global.innerWidth, vh = global.innerHeight;
    var width = S.pop.offsetWidth, height = S.pop.offsetHeight, gap = CFG.gap;
    var spots = [
      { x: rect.right + gap, y: rect.top + rect.height / 2 - height / 2, fits: rect.right + gap + width <= vw - 8 },
      { x: rect.left - width - gap, y: rect.top + rect.height / 2 - height / 2, fits: rect.left - gap - width >= 8 },
      { x: rect.left + rect.width / 2 - width / 2, y: rect.bottom + gap, fits: rect.bottom + gap + height <= vh - 8 },
      { x: rect.left + rect.width / 2 - width / 2, y: rect.top - height - gap, fits: rect.top - gap - height >= 8 }
    ];
    var spot = spots.find(function (item) { return item.fits; }) || spots[2];
    S.pop.style.left = Math.max(8, Math.min(spot.x, vw - width - 8)) + 'px';
    S.pop.style.top = Math.max(8, Math.min(spot.y, vh - height - 8)) + 'px';
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
    if (profile.party) chips.push('<span class="' + P + 'pchip ' + P + 'party" style="--pc:' +
      esc(profile.party_color || '#8794ad') + '">' + esc(profile.party) + '</span>');
    if (profile.birth) chips.push('<span class="' + P + 'pchip">' + esc(profile.birth) + ageFrom(profile.birth) + '</span>');
    if (profile.office) chips.push('<span class="' + P + 'pchip">现任' + esc(profile.office) + '</span>');
    return '<div class="' + P + 'prof"><div class="' + P + 'pchips">' + chips.join('') + '</div>' +
      '<div class="' + P + 'bio">' + esc(profile.bio) + '</div>' +
      '<div class="' + P + 'prow"><span class="' + P + 'more">展开全文</span>' +
      (profile.wiki_url ? '<a class="' + P + 'wlink" href="' + esc(profile.wiki_url) +
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
    return '<div class="' + P + 'row"><span class="t">📊 民调</span><span>' + sentence + '。</span></div>';
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
    var note = '<div class="' + P + 'note"><b>综合胜率说明：</b>' +
      esc(name || '该候选人') + '的综合胜率为 ' + pct(model.fused) +
      '。' + basis +
      (summary ? ' 选区判断：' + esc(summary) : '') + '</div>';
    return '<div class="' + P + 'row"><span class="t">🎯 综合胜率</span>' +
      bits.join('<span>·</span>') + '</div>' + note;
  }

  function lookup(options) {
    for (var i = 0; i < CFG.data.length; i++) {
      var item = CFG.data[i];
      if (item.region === options.region && item.candidate === options.candidate) return item;
    }
    return null;
  }
  function fallbackItem(options) {
    return {
      region: options.region,
      candidate: options.candidate,
      region_name: options.regionName || options.region,
      candidate_zh: options.name || options.candidate,
      image_url: options.image || '',
      price: options.price,
      yes_price: options.price,
      volume: options.volume,
      event_slug: options.eventSlug || '',
      profile: options.profile || null,
      poll: options.poll || null,
      model: options.model || null
    };
  }
  function cacheKey(item) { return item.region + '|' + item.candidate; }
  function open(anchor, options) {
    options = options || {};
    if (!anchor) return;
    S.openedAt = Date.now();
    var item = lookup(options) || fallbackItem(options);
    ensurePop();
    S.cur = { anchor: anchor, item: item };
    S.pop.classList.add('on');
    place(anchor);
    var cached = S.cache[cacheKey(item)];
    if (cached) { render(cached); return; }
    if (!CFG.api) {
      S.cache[cacheKey(item)] = item;
      render(item);
      return;
    }
    S.pop.innerHTML = '<div class="' + P + 'load">正在读取候选人资料…</div>';
    var query = 'region=' + encodeURIComponent(item.region) +
      '&candidate=' + encodeURIComponent(item.candidate_en || item.candidate) +
      '&region_name=' + encodeURIComponent(item.region_name || '') +
      '&zh=' + encodeURIComponent(item.candidate_zh || '');
    fetch(CFG.api + '/api/candidate?' + query)
      .then(function (response) { return response.json(); })
      .then(function (detail) {
        if (!detail || detail.error) throw new Error('详情读取失败');
        detail.event_slug = detail.event_slug || item.event_slug;
        detail.volume = item.volume;
        S.cache[cacheKey(item)] = detail;
        if (S.cur && S.cur.anchor === anchor) render(detail);
      })
      .catch(function () {
        S.cache[cacheKey(item)] = item;
        if (S.cur && S.cur.anchor === anchor) render(item);
      });
  }

  function render(item) {
    if (!S.cur || !S.pop) return;
    var name = item.candidate_zh || item.candidate_en || item.candidate || '候选人';
    var avatar = '<div class="' + P + 'av"><span>' + esc(name.charAt(0)) + '</span>' +
      (item.image_url ? '<img src="' + esc(item.image_url) + '" alt="' + esc(name) +
        '" onerror="this.remove()">' : '') + '</div>';
    var market = item.event_slug
      ? '<a href="https://polymarket.com/event/' + esc(item.event_slug) +
        '" target="_blank" rel="noopener">打开 Polymarket 市场 →</a>' : '';
    var fused = item.model && item.model.fused != null ? pct(item.model.fused) : '—';
    S.pop.innerHTML =
      '<div class="' + P + 'head">' + avatar +
        '<div><div class="' + P + 'nm">' + esc(name) + '</div>' +
        '<div class="' + P + 'rg">' + esc(item.region_name || item.region || '') +
        ' · Polymarket</div></div>' +
        '<div class="' + P + 'winrate"><div class="l">综合胜率</div><b>' + fused + '</b></div>' +
        '<div class="' + P + 'x" title="关闭">✕</div></div>' +
      profileBlock(item.profile) + pollBlock(item.poll) + modelBlock(item.model, name) +
      '<div class="' + P + 'foot"><span>累计成交 ' + usd(item.volume) + '</span>' + market + '</div>';
    var closeButton = S.pop.querySelector('.' + P + 'x');
    if (closeButton) closeButton.onclick = function (event) { event.stopPropagation(); close(); };
    var more = S.pop.querySelector('.' + P + 'prof .' + P + 'more');
    if (more) more.onclick = function (event) {
      event.stopPropagation();
      var bio = S.pop.querySelector('.' + P + 'prof .' + P + 'bio');
      var opened = bio.classList.toggle('open');
      more.textContent = opened ? '收起' : '展开全文';
    };
    place(S.cur.anchor);
  }

  function bind(element, options) {
    if (!element) return element;
    options = options || {};
    element.setAttribute('data-tmpp-bound', '1');
    if (canHover()) {
      element.addEventListener('mouseenter', function () {
        clearTimeout(S.closeT);
        clearTimeout(S.openT);
        S.openT = setTimeout(function () { open(element, options); }, CFG.openDelay);
      });
      element.addEventListener('mouseleave', function () {
        clearTimeout(S.openT);
        scheduleClose();
      });
    }
    element.addEventListener('click', function (event) {
      event.preventDefault();
      var justOpened = Date.now() - S.openedAt < 350;
      if (!justOpened && S.cur && S.cur.anchor === element && S.pop.classList.contains('on')) {
        close();
        return;
      }
      clearTimeout(S.openT);
      S.pinned = !isNarrow();
      open(element, options);
      if (isNarrow()) S.pinned = true;
    });
    if (element.tabIndex < 0) element.tabIndex = 0;
    element.addEventListener('focus', function () {
      if (element.matches && element.matches(':focus-visible')) open(element, options);
    });
    return element;
  }

  function autoBind(root) {
    var scope = root || document;
    Array.prototype.forEach.call(scope.querySelectorAll('[data-tem-region][data-tem-candidate]'), function (element) {
      if (element.getAttribute('data-tmpp-bound')) return;
      bind(element, {
        region: element.getAttribute('data-tem-region'),
        candidate: element.getAttribute('data-tem-candidate'),
        name: element.getAttribute('data-tem-name') || undefined,
        image: element.getAttribute('data-tem-image') || undefined
      });
    });
  }

  function init(options) {
    Object.keys(options || {}).forEach(function (key) {
      if (key in CFG) CFG[key] = options[key];
    });
    C = COLORS[CFG.theme] || COLORS.dark;
    var old = document.getElementById(P + 'style');
    if (old) old.remove();
    S.pop = null;
    injectCss();
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape') close(); });
    document.addEventListener('click', function (event) {
      if (!S.pop || !S.pop.classList.contains('on')) return;
      if (S.pop.contains(event.target)) return;
      if (S.cur && S.cur.anchor.contains(event.target)) return;
      close();
    });
    var follow = function () {
      if (!S.cur || !S.cur.anchor) return;
      if (S.cur.anchor.isConnected === false) { close(); return; }
      var rect = S.cur.anchor.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > global.innerHeight) { close(); return; }
      place(S.cur.anchor);
    };
    global.addEventListener('resize', follow);
    global.addEventListener('scroll', follow, { passive: true });
    autoBind();
    return API;
  }

  var API = {
    init: init,
    bind: bind,
    autoBind: autoBind,
    open: open,
    close: close,
    version: '2.0.0',
    config: CFG
  };
  global.TEMPopup = API;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { if (S.pop === null) injectCss(); });
  }
})(window);
"""


DEMO_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>候选人赔率与资料弹窗 · 独立模块演示</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; padding:26px; background:#070b14; color:#e9eefb;
         font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif; }
  h1 { font-size:19px; margin:0 0 6px; }
  p.lead { color:#8794ad; font-size:13px; line-height:1.8; margin:0 0 22px; }
  .tip { display:inline-block; padding:2px 9px; border-radius:99px; font-size:11.5px;
         background:rgba(34,211,238,.12); border:1px solid rgba(34,211,238,.3); color:#22d3ee; }
  .grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); }
  .row { display:flex; align-items:center; gap:10px; padding:10px 12px; cursor:pointer;
         border-radius:12px; background:#121a2a; border:1px solid rgba(118,160,255,.16);
         transition:transform .16s, border-color .16s; }
  .row:hover { transform:translateY(-2px); border-color:rgba(76,141,255,.55); }
  .row:focus-visible { outline:2px solid #22d3ee; outline-offset:2px; }
  .av { position:relative; width:38px; height:38px; flex:none; }
  .av span { position:absolute; inset:0; display:grid; place-items:center; border-radius:50%;
             background:rgba(118,160,255,.16); color:#8794ad; font-weight:600; }
  .av img { position:absolute; inset:0; width:100%; height:100%; border-radius:50%; object-fit:cover;
            border:1.5px solid rgba(118,160,255,.35); }
  .nm { font-size:13.5px; font-weight:600; line-height:1.3; }
  .rg { font-size:11px; color:#8794ad; margin-top:1px; }
  .pc { margin-left:auto; font-family:ui-monospace,monospace; font-size:15px; color:#3b9dff; }
  hr { border:0; border-top:1px solid rgba(118,160,255,.16); margin:28px 0; }
  h2 { font-size:15px; margin:0 0 10px; }
  pre { background:#0e1320; border:1px solid rgba(118,160,255,.16); border-radius:10px;
        padding:13px 15px; overflow:auto; color:#9ae6b4; font-size:12px; }
  code { background:rgba(34,211,238,.1); border:1px solid rgba(34,211,238,.2);
         padding:1px 6px; border-radius:5px; font-size:12px; color:#22d3ee; }
</style>
</head>
<body>
  <h1>候选人赔率与资料弹窗</h1>
  <p class="lead">独立模块，零依赖。<span class="tip">宽屏：鼠标悬停</span>
     <span class="tip">窄屏：点击</span><br>
     展示当前赔率、民调、个人资料和综合胜率的形成说明。</p>
  <div class="grid">
"""

DEMO_TAIL = """  </div>
  <hr>
  <h2>用法</h2>
  <pre>&lt;script src="tem-candidate-popup.js"&gt;&lt;/script&gt;
&lt;script&gt;
  TEMPopup.init({ api: 'https://你的域名' });   // 或 data: [...]

  TEMPopup.bind(document.querySelector('#myRow'), {
    region: 'NTPC', candidate: 'Su Chiao-hui'
  });

  TEMPopup.autoBind();
&lt;/script&gt;</pre>
  <p class="lead">参数：<code>api</code> 指定后端地址；
     <code>data</code> 直接传入本地数据；<code>theme</code> 填 <code>light</code> 切换浅色；
     <code>openOn</code> 可设 <code>hover</code>／<code>click</code>／<code>auto</code>。</p>
  <script src="tem-candidate-popup.js"></script>
  <script>
    TEMPopup.init({ data: __DATA__ });
    TEMPopup.autoBind();
  </script>
</body>
</html>
"""


def _demo_row(item: dict[str, Any]) -> str:
    name = item.get("candidate_zh") or item.get("candidate") or ""
    initial = name[:1] if name else "?"
    image = item.get("image_url") or ""
    img = (
        f'<img src="{image}" alt="{name}" loading="lazy" onerror="this.remove()">'
        if image
        else ""
    )
    return (
        f'    <div class="row" data-tem-region="{item["region"]}" '
        f'data-tem-candidate="{item["candidate"]}" data-tem-name="{name}" '
        f'data-tem-image="{image}" tabindex="0">'
        f'<div class="av"><span>{initial}</span>{img}</div>'
        f'<div><div class="nm">{name}</div><div class="rg">{item.get("region_name","")}</div></div>'
        f'<div class="pc">{(item.get("price") or 0) * 100:.1f}%</div></div>\n'
    )


def normalise_for_popup(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把卡片字段转换成独立弹窗使用的候选人与县市键。"""
    out: list[dict[str, Any]] = []
    for item in items:
        entry = dict(item)
        entry.setdefault("candidate", item.get("candidate_en") or item.get("candidate"))
        out.append(entry)
    return out


def build_popup_demo(items: list[dict[str, Any]], *, limit: int = 60) -> str:
    data = normalise_for_popup(items)
    rows = "".join(_demo_row(item) for item in data[:limit])
    return DEMO_HEAD + rows + DEMO_TAIL.replace(
        "__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    )
