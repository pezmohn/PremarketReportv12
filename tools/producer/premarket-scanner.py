#!/usr/bin/env python3
"""
Pre-Market Momentum Scanner V6 — Final Strategy + GapFade1 + optional GEX overlay
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PRODUCER_DIR = Path(__file__).resolve().parent
for path in (WORKSPACE_ROOT, PRODUCER_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from barchart_gex_fastlane import BarchartLevelsHistoryStore, build_enriched_rows, collect_fastlane_batch

API_KEY_PATH = WORKSPACE_ROOT / 'sync/polygon-api-key.txt'
MCAP_CACHE_PATH = WORKSPACE_ROOT / 'scripts/data/mcap_cache.json'
BASE_URL = 'https://api.polygon.io'

DEFAULT_MIN_PRICE = 10.0
DEFAULT_MIN_PM_CHG = 3.0
DEFAULT_MIN_MCAP = 2_000_000_000
DEFAULT_MIN_PM_VOL = 500_000
DEFAULT_MAX_RESULTS = 10
DEFAULT_GEX_TOP_N = 10
DEFAULT_DIRECTION_POLICY = "gapfade"
DEFAULT_TRIGGER_POLICY = "fib"
DEFAULT_ENTRY_POLICY = "close_through"

RATE_LIMIT = 4.5
_last_req = 0

LEVEL_FLIP_EXPECTANCY = {
    'Ext_-27.2': {'r': 0.88, 'n': 404, 'hit_1r': 51.5, 'tier': 1, 'rank': 1},
    'Fib_78.6': {'r': 0.81, 'n': 617, 'hit_1r': 53.5, 'tier': 1, 'rank': 2},
    'Fib_50.0': {'r': 0.75, 'n': 770, 'hit_1r': 51.3, 'tier': 1, 'rank': 3},
    'Fib_23.6': {'r': 0.69, 'n': 893, 'hit_1r': 54.2, 'tier': 1, 'rank': 4},
}


def _api_key():
    return Path(API_KEY_PATH).read_text().strip()


def _get(url, params):
    global _last_req
    wait = 1.0 / RATE_LIMIT - (time.time() - _last_req)
    if wait > 0:
        time.sleep(wait)
    resp = requests.get(url, params=params, timeout=15)
    _last_req = time.time()
    resp.raise_for_status()
    return resp.json()


def load_mcap_cache():
    if MCAP_CACHE_PATH.exists():
        with MCAP_CACHE_PATH.open() as f:
            data = json.load(f)
            now = time.time()
            return {k: v['mcap'] for k, v in data.items() if now - v.get('ts', 0) < 7 * 86400}
    return {}


def save_mcap_cache(cache):
    MCAP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {k: {'mcap': v, 'ts': time.time()} for k, v in cache.items()}
    if MCAP_CACHE_PATH.exists():
        with MCAP_CACHE_PATH.open() as f:
            existing = json.load(f)
        existing.update(data)
        data = existing
    with MCAP_CACHE_PATH.open('w') as f:
        json.dump(data, f)


def fetch_market_cap(ticker, api_key):
    try:
        data = _get(f'{BASE_URL}/v3/reference/tickers/{ticker}', {'apiKey': api_key})
        return data.get('results', {}).get('market_cap')
    except Exception:
        return None


def fetch_market_caps(tickers, api_key, cache):
    result = {}
    for ticker in tickers:
        if ticker in cache:
            result[ticker] = cache[ticker]
            continue
        mcap = fetch_market_cap(ticker, api_key)
        if mcap is not None:
            cache[ticker] = mcap
            result[ticker] = mcap
    return result


def get_snapshot(api_key):
    data = _get(f'{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers', {'apiKey': api_key})
    return data.get('tickers', [])


def calc_levels(pm_high, pm_low, prev_high, prev_low, prev_close):
    rng = max(pm_high - pm_low, 0.01)
    return {
        'PM_High': {'price': pm_high, 'type': 'pm'},
        'PM_Low': {'price': pm_low, 'type': 'pm'},
        'PDH': {'price': prev_high, 'type': 'daily'},
        'PDL': {'price': prev_low, 'type': 'daily'},
        'PDC': {'price': prev_close, 'type': 'daily'},
        'Ext_-27.2': {'price': pm_low - 0.272 * rng, **LEVEL_FLIP_EXPECTANCY['Ext_-27.2'], 'type': 'ext'},
        'Ext_127.2': {'price': pm_high + 0.272 * rng, **LEVEL_FLIP_EXPECTANCY['Ext_-27.2'], 'type': 'ext'},
        'Fib_78.6': {'price': pm_low + 0.786 * rng, **LEVEL_FLIP_EXPECTANCY['Fib_78.6'], 'type': 'fib'},
        'Fib_50.0': {'price': pm_low + 0.500 * rng, **LEVEL_FLIP_EXPECTANCY['Fib_50.0'], 'type': 'fib'},
        'Fib_23.6': {'price': pm_low + 0.236 * rng, **LEVEL_FLIP_EXPECTANCY['Fib_23.6'], 'type': 'fib'},
    }


def calc_range_position(price, pm_high, pm_low):
    rng = max(pm_high - pm_low, 0.01)
    pos = (price - pm_low) / rng * 100
    if pos >= 70:
        return pos, 'long'
    if pos <= 30:
        return pos, 'short'
    return pos, 'neutral'


def score_setup(pm_chg_pct, pm_vol, levels, range_bias):
    score = 50
    score += min(25, abs(pm_chg_pct) * 3)
    if pm_vol >= 1_000_000:
        score += 10
    if range_bias != 'neutral':
        score += 10
    return min(100, round(score))


def calc_gapfade1(pm_chg_pct, rel_vol, pm_high, pm_low, pm_price, pm_vol, prev_close, prev_high, prev_low, adr14=None):
    if pm_chg_pct < 8.0 or (rel_vol >= 1.5 and rel_vol > 0):
        return None
    score = 0
    flags = []
    gap_pct = pm_chg_pct
    if adr14 is None:
        adr14 = (prev_high - prev_low) * 1.2 if prev_high > prev_low else 1.0
    gap_dollars = abs(pm_price - prev_close)
    gap_adr_ratio = gap_dollars / adr14 if adr14 > 0 else 0
    if 1.0 <= gap_adr_ratio <= 3.0:
        score += 25
        flags.append(f"Gap/ADR {gap_adr_ratio:.1f}x ⭐")
    elif 0.5 <= gap_adr_ratio < 1.0:
        score += 10
        flags.append(f"Gap/ADR {gap_adr_ratio:.1f}x")
    elif gap_adr_ratio > 5.0:
        score -= 10
        flags.append(f"Gap/ADR {gap_adr_ratio:.1f}x ⚠️ extreme")
    else:
        flags.append(f"Gap/ADR {gap_adr_ratio:.1f}x")
    pm_ext_pct = (pm_high - pm_price) / gap_dollars * 100 if gap_dollars > 0 else 0
    if pm_price < prev_close + gap_dollars * 0.95:
        score += 20
        flags.append('PM fading ⭐')
    elif pm_ext_pct > 5:
        score += 15
        flags.append(f"PM pullback {pm_ext_pct:.0f}%")
    else:
        flags.append('PM near highs')
    if pm_vol >= 1_000_000:
        score += 20
        flags.append('PM Vol >1M ⭐')
    elif pm_vol >= 500_000:
        score += 10
        flags.append('PM Vol >500k')
    dow = datetime.now().weekday()
    if dow == 3:
        score += 15
        flags.append('Thursday ⭐')
    elif dow in (1, 2):
        score += 5
        flags.append('Tue' if dow == 1 else 'Wed')
    if rel_vol < 0.7:
        score += 15
        flags.append(f"RVOL {rel_vol:.1f}x (very low) ⭐")
    elif rel_vol < 1.0:
        score += 10
        flags.append(f"RVOL {rel_vol:.1f}x (low)")
    else:
        flags.append(f"RVOL {rel_vol:.1f}x")
    if 8 <= gap_pct <= 15:
        score += 10
        flags.append(f"Gap {gap_pct:.1f}% (sweetspot)")
    elif gap_pct > 20:
        score -= 5
        flags.append(f"Gap {gap_pct:.1f}% (very large ⚠️)")
    else:
        flags.append(f"Gap {gap_pct:.1f}%")
    score = max(0, min(100, score))
    confidence = 'HIGH' if score >= 70 else 'MEDIUM' if score >= 45 else 'LOW'
    return {'score': score, 'confidence': confidence, 'flags': flags, 'gap_adr_ratio': round(gap_adr_ratio, 2), 'rvol': round(rel_vol, 1), 'gap_pct': round(gap_pct, 1)}


def scan_premarket(min_price=DEFAULT_MIN_PRICE, min_pm_chg=DEFAULT_MIN_PM_CHG, min_mcap=DEFAULT_MIN_MCAP, min_pm_vol=DEFAULT_MIN_PM_VOL, max_results=DEFAULT_MAX_RESULTS):
    api_key = _api_key()
    mcap_cache = load_mcap_cache()
    all_tickers = get_snapshot(api_key)
    candidates = []
    for t in all_tickers:
        mn = t.get('min', {})
        prev = t.get('prevDay', {})
        prev_close = prev.get('c', 0)
        pm_price = mn.get('c', 0)
        pm_vol = mn.get('av', 0)
        pm_high = mn.get('h', 0)
        pm_low = mn.get('l', 0)
        if not prev_close or prev_close < min_price or not pm_price or pm_price < min_price or not pm_vol or pm_vol < min_pm_vol:
            continue
        pm_chg = (pm_price - prev_close) / prev_close * 100
        if abs(pm_chg) < min_pm_chg:
            continue
        prev_vol = prev.get('v', 0)
        rel_vol = pm_vol / (prev_vol / 6.5) if prev_vol > 0 else 0
        candidates.append({'ticker': t['ticker'], 'prev_close': prev_close, 'prev_high': prev.get('h', 0), 'prev_low': prev.get('l', 0), 'pm_price': pm_price, 'pm_chg_pct': round(pm_chg, 2), 'pm_vol': pm_vol, 'pm_high': pm_high, 'pm_low': pm_low, 'rel_vol': round(rel_vol, 1)})
    ticker_list = [c['ticker'] for c in candidates]
    mcaps = fetch_market_caps(ticker_list, api_key, mcap_cache)
    save_mcap_cache(mcap_cache)
    filtered = []
    for c in candidates:
        mcap = mcaps.get(c['ticker'])
        if mcap is None or mcap < min_mcap:
            continue
        c['market_cap_b'] = round(mcap / 1e9, 1)
        pm_range_pct = (c['pm_high'] - c['pm_low']) / c['pm_low'] * 100 if c['pm_low'] > 0 else 0
        c['pm_range_pct'] = round(pm_range_pct, 2)
        c['narrow_pm'] = pm_range_pct < 0.5
        c['levels'] = calc_levels(c['pm_high'], c['pm_low'], c['prev_high'], c['prev_low'], c['prev_close'])
        rpos, rbias = calc_range_position(c['pm_price'], c['pm_high'], c['pm_low'])
        c['range_position'] = round(rpos, 1)
        c['range_bias'] = rbias
        c['score'] = score_setup(c['pm_chg_pct'], c['pm_vol'], c['levels'], rbias)
        c['gap_up'] = c['pm_chg_pct'] > 0
        c['gapfade1'] = calc_gapfade1(c['pm_chg_pct'], c['rel_vol'], c['pm_high'], c['pm_low'], c['pm_price'], c['pm_vol'], c['prev_close'], c['prev_high'], c['prev_low'])
        filtered.append(c)
    filtered.sort(key=lambda x: x['score'], reverse=True)
    return filtered[:max_results]


def attach_gex_overlay(candidates, top_n=DEFAULT_GEX_TOP_N):
    tickers = [c['ticker'] for c in candidates[:top_n]]
    if not tickers:
        return None
    result = collect_fastlane_batch(tickers=tickers)
    baseline_store = BarchartLevelsHistoryStore(WORKSPACE_ROOT / 'state/barchart_levels.sqlite')
    try:
        enriched_rows = build_enriched_rows(result, baseline_store=baseline_store, compare_phase='open')
    finally:
        baseline_store.close()
    overlay = {}
    for snapshot, actionability, comparison, structure, decision, ranking in enriched_rows:
        overlay[snapshot.ticker] = {
            'flip': snapshot.gamma_flip,
            'call_wall': snapshot.call_wall,
            'put_wall': snapshot.put_wall,
            'state_label': actionability.state_label,
            'bias': actionability.bias,
            'index_regime_summary': actionability.index_regime_summary,
            'gamma_box': structure.gamma_box,
            'trigger': decision.trigger,
            'decision_summary': decision.summary,
        }
    return overlay


def _compact_trigger(trigger: Optional[str]) -> str:
    if not trigger:
        return '-'
    text = trigger.replace('Only treat upside as cleaner if price reclaims ', '')
    text = text.replace('Need clean acceptance above ', '')
    text = text.replace(' to escape the gamma box.', '')
    return text.strip()


def derive_tradeability(candidate, gex):
    if not gex:
        return ('B', 'scanner only')
    state = gex.get('state_label') or ''
    bias = gex.get('bias') or 'neutral'
    trigger = _compact_trigger(gex.get('trigger'))
    narrow_pm = candidate.get('narrow_pm', False)
    pm_range = candidate.get('pm_range_pct', 0)

    if 'below_flip' in state or bias == 'neutral':
        return ('C', 'below flip / weak context')
    if 'at_flip' in state:
        return ('B-', 'at flip, needs decision')
    if 'pressed_into_call_wall' in state:
        return ('B', 'directly into wall, decision-zone')
    if gex.get('gamma_box'):
        if narrow_pm and pm_range < 0.35:
            return ('B', 'boxed and PM too tight')
        if trigger and trigger != '-':
            return ('A-' if bias == 'mean_reversion' else 'B+', 'boxed but clear trigger nearby')
        return ('B+', 'boxed but structurally okay')
    if 'above_flip' in state and bias == 'mean_reversion':
        return ('A', 'above flip with cleaner air')
    return ('B', 'acceptable but not clean')


def _level_price(candidate, name):
    level = (candidate.get('levels') or {}).get(name)
    if isinstance(level, dict):
        return level.get('price')
    return level


def _fmt_price(value):
    return f"{value:.2f}" if value is not None else "-"


def _fmt_gex_line(ticker, gex_overlay):
    if not gex_overlay or ticker not in gex_overlay:
        return None
    gex = gex_overlay.get(ticker) or {}
    parts = [
        f"Flip {_fmt_price(gex.get('flip'))}",
        f"CallW {_fmt_price(gex.get('call_wall'))}",
        f"PutW {_fmt_price(gex.get('put_wall'))}",
    ]
    if gex.get('gamma_box'):
        parts.append(f"Box {gex['gamma_box']}")
    state = gex.get('state_label')
    if state:
        parts.append(state)
    return "GEX: " + " | ".join(parts)


def direction_for_candidate(candidate):
    if candidate.get('gapfade1'):
        return 'short'
    return 'long' if candidate.get('pm_chg_pct', 0) > 0 else 'short'


def choose_fib_trigger(candidate, expected_side):
    pm_price = float(candidate.get('pm_price') or 0)
    if pm_price <= 0:
        return None, None
    if expected_side == 'long':
        preferred = ['Fib_78.6', 'Fib_50.0', 'Fib_23.6']
        candidates = [
            (name, float(price), abs(float(price) - pm_price) / pm_price)
            for name in preferred
            if (price := _level_price(candidate, name)) is not None and float(price) >= pm_price * 0.995
        ]
        if candidates:
            name, price, _ = min(candidates, key=lambda item: item[2])
            return name, round(price, 4)
        price = _level_price(candidate, 'PM_High') or candidate.get('pm_high')
        return ('PM_High', round(float(price), 4)) if price is not None else (None, None)
    if expected_side == 'short':
        preferred = ['Fib_23.6', 'Fib_50.0', 'Fib_78.6']
        candidates = [
            (name, float(price), abs(float(price) - pm_price) / pm_price)
            for name in preferred
            if (price := _level_price(candidate, name)) is not None and float(price) <= pm_price * 1.005
        ]
        if candidates:
            name, price, _ = min(candidates, key=lambda item: item[2])
            return name, round(price, 4)
        price = _level_price(candidate, 'PM_Low') or candidate.get('pm_low')
        return ('PM_Low', round(float(price), 4)) if price is not None else (None, None)
    return None, None


def tradeability_grade(candidate):
    score = int(candidate.get('score') or 0)
    if candidate.get('gapfade1'):
        score += 8
    if candidate.get('narrow_pm'):
        score -= 15
    if candidate.get('rel_vol', 0) < 0.25:
        score -= 5
    score = max(0, min(100, score))
    if score >= 82:
        return 'A'
    if score >= 74:
        return 'B+'
    if score >= 64:
        return 'B'
    return 'C'


def setup_label(candidate, expected_side):
    if candidate.get('gapfade1'):
        return 'GapFade Short'
    return 'Long Continuation' if expected_side == 'long' else 'Short Continuation'


def build_claims(candidates, gex_overlay=None):
    claims = []
    for candidate in candidates:
        expected_side = direction_for_candidate(candidate)
        trigger_name, trigger_price = choose_fib_trigger(candidate, expected_side)
        warnings = []
        if candidate.get('narrow_pm'):
            warnings.append('narrow_pm')
        if candidate.get('rel_vol', 0) < 0.25:
            warnings.append('low_rel_vol')
        if gex_overlay and candidate['ticker'] in gex_overlay and gex_overlay[candidate['ticker']].get('gamma_box'):
            warnings.append('gamma_box')
        claims.append({
            'ticker': candidate['ticker'],
            'pm_price': candidate.get('pm_price'),
            'scanner_score': candidate.get('score'),
            'pm_change_pct': candidate.get('pm_chg_pct'),
            'expected_side': expected_side,
            'tradeability_grade': tradeability_grade(candidate),
            'tradeability_read': setup_label(candidate, expected_side),
            'trigger_price': trigger_price,
            'trigger_name': trigger_name,
            'entry_policy': DEFAULT_ENTRY_POLICY,
            'setup_type': 'GapFade1' if candidate.get('gapfade1') else 'S1/S2',
            'warnings': warnings,
        })
    return claims


def rank_report_candidates(candidates, gex_overlay=None):
    claims_by_ticker = {claim['ticker']: claim for claim in build_claims(candidates, gex_overlay)}
    ranked = []
    for candidate in candidates:
        claim = claims_by_ticker[candidate['ticker']]
        grade_rank = {'A': 4, 'B+': 3, 'B': 2, 'C': 1}.get(claim['tradeability_grade'], 0)
        warning_penalty = len(claim['warnings'])
        ranked.append((grade_rank, int(candidate.get('score') or 0), -warning_penalty, candidate, claim))
    ranked.sort(reverse=True, key=lambda item: item[:3])
    a_setups = [(c, claim) for _, _, _, c, claim in ranked if claim['tradeability_grade'] in {'A', 'B+'} and claim.get('trigger_price')][:4]
    used = {claim['ticker'] for _, claim in a_setups}
    watchlist = [(c, claim) for _, _, _, c, claim in ranked if claim['ticker'] not in used and claim['tradeability_grade'] == 'B'][:4]
    used.update(claim['ticker'] for _, claim in watchlist)
    avoid = [(c, claim) for _, _, _, c, claim in ranked if claim['ticker'] not in used][:4]
    return a_setups, watchlist, avoid


def format_telegram(candidates, gex_overlay=None):
    ts = datetime.now(timezone.utc).strftime('%H:%M')
    n = len(candidates)
    lines = [
        f"🔍 **Premarket Report V1.2** ({ts} UTC)",
        "Engine: **Gapfade Direction + Fib Trigger + Close-through Entry**",
        f"📊 {n} Kandidaten | EOD-validiert | Entry: erste 1m-Close durch Trigger",
    ]
    if gex_overlay:
        first = next(iter(gex_overlay.values()), None)
        if first and first.get('index_regime_summary'):
            lines.append(f"🧠 Market Read: {first['index_regime_summary']}")
    lines.append('')
    if not candidates:
        lines.append('Keine Momentum-Kandidaten gefunden.')
        return '\n'.join(lines)

    a_setups, watchlist, avoid = rank_report_candidates(candidates, gex_overlay)
    claims_by_ticker = {claim['ticker']: claim for claim in build_claims(candidates, gex_overlay)}

    lines.append('**A-Setups / Tradeable nur nach Trigger**')
    if not a_setups:
        lines.append('- Keine A-Setups. Heute nur Watchlist/No-Trade-Disziplin.')
    for i, (c, claim) in enumerate(a_setups, 1):
        arrow = '🟢' if c['pm_chg_pct'] > 0 else '🔴'
        vol_str = f"{c['pm_vol']/1e6:.1f}M" if c['pm_vol'] >= 1_000_000 else f"{c['pm_vol']:,.0f}"
        side = 'LONG' if claim['expected_side'] == 'long' else 'SHORT'
        lines.append(f"{i}. {arrow} **{c['ticker']} — {claim['tradeability_grade']} {claim['tradeability_read']}**")
        lines.append(
            f"   PM {c['pm_chg_pct']:+.1f}% @ ${c['pm_price']:.2f} | Score {c['score']}/100 | "
            f"Vol {vol_str} / RV {c['rel_vol']}x | Cap ${c['market_cap_b']}B"
        )
        lines.append(
            f"   Bias: **{side}** | Trigger: **{claim['trigger_name']} {_fmt_price(claim['trigger_price'])}** | Entry: Close-through"
        )
        gex_line = _fmt_gex_line(c['ticker'], gex_overlay)
        if gex_line:
            lines.append(f"   {gex_line}")
        if claim['warnings']:
            lines.append(f"   Warnung: {', '.join(claim['warnings'])}")
        lines.append('')

    lines.append('**Watchlist / Nur bei sauberem Trigger**')
    if not watchlist:
        lines.append('- Keine separaten Watchlist-Kandidaten.')
    for c, claim in watchlist:
        side = 'LONG' if claim['expected_side'] == 'long' else 'SHORT'
        warn = f" | Warnung: {', '.join(claim['warnings'])}" if claim['warnings'] else ''
        gex = f" | {_fmt_gex_line(c['ticker'], gex_overlay)}" if _fmt_gex_line(c['ticker'], gex_overlay) else ''
        lines.append(
            f"- **{c['ticker']}** {c['pm_chg_pct']:+.1f}% @ ${c['pm_price']:.2f} | "
            f"{claim['tradeability_grade']} | {side} | Trigger {claim['trigger_name']} {_fmt_price(claim['trigger_price'])}{gex}{warn}"
        )
    lines.append('')

    lines.append('**Avoid / Kein First-Choice-Trade**')
    if not avoid:
        lines.append('- Keine Avoid-Kandidaten im aktuellen Set.')
    for c, claim in avoid:
        reasons = list(claim['warnings'])
        if claim['tradeability_grade'] == 'C':
            reasons.append('low_tradeability')
        if not reasons:
            reasons.append('weaker_than_top_setups')
        gex = f" | {_fmt_gex_line(c['ticker'], gex_overlay)}" if _fmt_gex_line(c['ticker'], gex_overlay) else ''
        lines.append(
            f"- **{c['ticker']}** {c['pm_chg_pct']:+.1f}% @ ${c['pm_price']:.2f} | "
            f"{claim['tradeability_grade']} | {', '.join(reasons)}{gex}"
        )
    lines.append('')

    lines.append('**Alle Scanner-Kandidaten / Audit Trail**')
    for c in candidates:
        claim = claims_by_ticker[c['ticker']]
        side = 'LONG' if claim['expected_side'] == 'long' else 'SHORT'
        lines.append(
            f"- **{c['ticker']}** | {claim['tradeability_grade']} | {side} | "
            f"Score {c['score']} | Trigger {claim['trigger_name']} {_fmt_price(claim['trigger_price'])}"
        )

    gf1_candidates = [c for c in candidates if c.get('gapfade1')]
    if gf1_candidates:
        lines.append('─' * 40)
        lines.append('')
        lines.append('🔻 **GapFade1 — Gap Up Fade Candidates**')
        lines.append('📊 60% Win | +3.0R avg | PF 2.0+ (83k events, 5yr)')
        lines.append('')
        gf1_sorted = sorted(gf1_candidates, key=lambda x: x['gapfade1']['score'], reverse=True)
        for c in gf1_sorted:
            gf = c['gapfade1']
            conf_emoji = '🟢' if gf['confidence'] == 'HIGH' else '🟡' if gf['confidence'] == 'MEDIUM' else '🔴'
            lines.append(f"{conf_emoji} **{c['ticker']}** Gap +{gf['gap_pct']:.1f}% | RVOL {gf['rvol']}x | Fade Score: {gf['score']}/100 ({gf['confidence']})")
            lines.append(f"  {' | '.join(gf['flags'])}")
            lines.append('')
        lines.append('📋 **GF1 Bias:** Gap-up Fades bevorzugt SHORT; Entry erst nach Close-through Trigger')
        lines.append('🛡 **GF1 Exit:** Stop: ORB/Trigger invalidation | Trail BE@0.5R, 0.25R step')
        lines.append('')
    lines.append('─' * 40)
    lines.append('')
    lines.append('📋 **Entry-Regel:** Trigger muss per 1m-Close gebrochen werden; kein Touch-Only Entry')
    lines.append('🛡 **Exit:** Stop 0.8% | Trail BE@1R, 0.5R step | EOD close')
    lines.append('⚠️ Max 4 trades/day | $100 risk each | Stop nach -$400')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Pre-Market Momentum Scanner V6')
    parser.add_argument('--min-price', type=float, default=DEFAULT_MIN_PRICE)
    parser.add_argument('--min-pm-chg', type=float, default=DEFAULT_MIN_PM_CHG)
    parser.add_argument('--min-mcap', type=float, default=DEFAULT_MIN_MCAP)
    parser.add_argument('--min-vol', type=int, default=DEFAULT_MIN_PM_VOL)
    parser.add_argument('--max-results', type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--meta-json', action='store_true', help='Emit structured scanner output with message + metadata')
    parser.add_argument('--with-gex', action='store_true', help='Overlay Fastlane GEX context for top candidates')
    args = parser.parse_args()
    run_started_at = datetime.now(timezone.utc).isoformat()
    fetch_errors = []
    try:
        candidates = scan_premarket(min_price=args.min_price, min_pm_chg=args.min_pm_chg, min_mcap=args.min_mcap, min_pm_vol=args.min_vol, max_results=args.max_results)
    except Exception as exc:
        if args.meta_json:
            payload = {
                'ok': False,
                'error': str(exc),
                'has_fetch_errors': True,
                'has_stale_flags': False,
                'candidate_count': 0,
                'run_started_at': run_started_at,
                'run_finished_at': datetime.now(timezone.utc).isoformat(),
                'message': '',
                'candidates': [],
                'gex_overlay': None,
            }
            print(json.dumps(payload, indent=2, default=str))
            return
        raise
    gex_overlay = attach_gex_overlay(candidates) if args.with_gex and not args.json else None
    message = format_telegram(candidates, gex_overlay=gex_overlay)
    has_stale_flags = ('stale' in message.lower()) or ('unreliable' in message.lower()) or ('skip' in message.lower())
    if args.meta_json:
        payload = {
            'ok': True,
            'error': None,
            'has_fetch_errors': bool(fetch_errors),
            'has_stale_flags': has_stale_flags,
            'candidate_count': len(candidates),
            'run_started_at': run_started_at,
            'run_finished_at': datetime.now(timezone.utc).isoformat(),
            'message': message,
            'candidates': candidates,
            'claims': build_claims(candidates, gex_overlay),
            'report_template': 'premarket_v1_2_a_watchlist_avoid',
            'direction_policy': DEFAULT_DIRECTION_POLICY,
            'trigger_policy': DEFAULT_TRIGGER_POLICY,
            'entry_policy': DEFAULT_ENTRY_POLICY,
            'gex_overlay': gex_overlay,
        }
        print(json.dumps(payload, indent=2, default=str))
    elif args.json:
        print(json.dumps(candidates, indent=2, default=str))
    else:
        print(message)


if __name__ == '__main__':
    main()
