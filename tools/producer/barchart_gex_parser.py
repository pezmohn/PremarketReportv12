import json
import math
import re
import urllib.parse
import urllib.request
from typing import Any, Iterable

INLINE_DATA_ID = "barchart-www-inline-gamma-levels"
BARCHART_GEX_URL = "https://www.barchart.com/stocks/quotes/{ticker}/gamma-exposure"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
DEFAULT_DECISION_WINDOW_PCT = 3.0
DEFAULT_MAX_DECISION_LEVELS = 3


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value or value.upper() == "N/A":
            return None
        if value.endswith("%"):
            try:
                return float(value[:-1]) / 100.0
            except ValueError:
                return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_inline_payload(html: str, element_id: str = INLINE_DATA_ID) -> dict:
    pattern = re.compile(
        rf'<script[^>]*id=["\']{re.escape(element_id)}["\'][^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html)
    if not match:
        raise ValueError(f"Inline JSON script not found: {element_id}")

    raw = match.group(1).strip()
    if not raw:
        raise ValueError(f"Inline JSON script is empty: {element_id}")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid inline JSON in {element_id}: {exc}") from exc


def _extract_page_last_price(html: str) -> float | None:
    """Best-effort current underlying spot from page-level symbol metadata.

    Barchart's inline gamma payload often carries EOD-ish base prices like
    `baseDailyLastPrice`, while the page shell can expose a more current
    `lastPrice` for the underlying symbol. For trading interpretation we want
    the current page-level symbol spot when available.
    """
    patterns = [
        r'"lastPrice"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
        r'&quot;lastPrice&quot;:\s*&quot;([0-9]+(?:\.[0-9]+)?)&quot;',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return _coerce_float(match.group(1))
    return None


def _iter_records(payload: Any) -> Iterable[dict]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return

    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            for item in payload["data"]:
                if isinstance(item, dict):
                    yield item
            return

        for key in ("calls", "puts", "rows", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
                return

        yielded = False
        for value in payload.values():
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                for item in value:
                    yield item
                yielded = True
        if yielded:
            return


def _norm_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _bs_gamma(spot: float, strike: float, time_to_expiry: float, volatility: float) -> float:
    if spot <= 0 or strike <= 0 or time_to_expiry <= 0 or volatility <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * volatility * volatility * time_to_expiry) / (
        volatility * math.sqrt(time_to_expiry)
    )
    return _norm_pdf(d1) / (spot * volatility * math.sqrt(time_to_expiry))


def _record_value(record: dict, *keys: str) -> Any:
    raw = record.get("raw")
    if isinstance(raw, dict):
        for key in keys:
            if key in raw and raw[key] not in (None, ""):
                return raw[key]
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _find_local_peak_levels(
    signed_exposure_by_strike: dict[float, float],
    spot: float,
    *,
    side: str,
    window_pct: float,
    max_levels: int,
) -> list[float]:
    if spot <= 0 or not signed_exposure_by_strike:
        return []

    lower = spot * (1.0 - window_pct / 100.0)
    upper = spot * (1.0 + window_pct / 100.0)
    strikes = [
        strike
        for strike in sorted(signed_exposure_by_strike)
        if lower <= strike <= upper
        and ((side == "below" and strike < spot) or (side == "above" and strike > spot))
    ]
    peaks: list[tuple[float, float, float]] = []

    for index, strike in enumerate(strikes):
        strength = abs(signed_exposure_by_strike[strike])
        if strength <= 0:
            continue

        prev_strength = abs(signed_exposure_by_strike[strikes[index - 1]]) if index > 0 else -1.0
        next_strength = abs(signed_exposure_by_strike[strikes[index + 1]]) if index < len(strikes) - 1 else -1.0
        if strength >= prev_strength and strength >= next_strength:
            peaks.append((strike, strength, abs(strike - spot)))

    peaks.sort(key=lambda item: (-item[1], item[2], item[0]))
    return [strike for strike, _, _ in peaks[:max_levels]]


def _normalize_option_type(record: dict) -> str | None:
    for key in ("optionType", "type", "callPut", "side"):
        value = record.get(key)
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in {"call", "c"}:
            return "call"
        if text in {"put", "p"}:
            return "put"
    return None


def extract_barchart_levels(
    html: str,
    include_debug: bool = True,
    decision_window_pct: float = DEFAULT_DECISION_WINDOW_PCT,
    max_decision_levels: int = DEFAULT_MAX_DECISION_LEVELS,
) -> dict:
    """
    Parse Barchart's inline gamma JSON and compute spot, call wall, put wall, gamma flip.

    Uses Barchart UI-compatible field priority where possible:
      - current page-level `lastPrice` for trading interpretation
      - `baseLastPrice` before `baseDailyLastPrice` for payload fallback
      - `openInterest` before `dailyOpenInterest`
      - `gamma` before `dailyGamma`

    This better matches the visible Barchart gamma page for intraday reads,
    while keeping EOD-style fields as fallback when live-ish fields are absent.

    Returns a dict with the computed levels plus some debug context.
    """
    payload = _extract_inline_payload(html)
    records = list(_iter_records(payload))
    if not records:
        raise ValueError("No option records found in inline gamma payload")

    payload_spot = None
    current_spot = _extract_page_last_price(html)
    strike_points: set[float] = set()
    call_wall_exposure: dict[float, float] = {}
    put_wall_exposure: dict[float, float] = {}
    call_open_interest_by_strike: dict[float, float] = {}
    put_open_interest_by_strike: dict[float, float] = {}
    used_rows = 0
    option_rows: list[dict[str, float | str]] = []

    for record in records:
        strike = _coerce_float(_record_value(record, "strikePrice", "strike"))
        option_type = _normalize_option_type(record)
        gamma = _coerce_float(_record_value(record, "gamma", "dailyGamma"))
        oi = _coerce_float(_record_value(record, "openInterest", "dailyOpenInterest"))
        dte_days = _coerce_float(_record_value(record, "daysToExpiration"))
        volatility = _coerce_float(_record_value(record, "averageVolatility"))
        record_spot = _coerce_float(_record_value(record, "baseLastPrice", "baseDailyLastPrice"))
        if payload_spot is None and record_spot is not None:
            payload_spot = record_spot

        if strike is None or option_type is None or gamma is None or oi is None or record_spot is None:
            continue
        if oi <= 0 or gamma == 0:
            continue

        strike_points.add(strike)
        raw_gex = gamma * oi * 100.0 * (record_spot**2) * 0.01
        if option_type == "call":
            call_wall_exposure[strike] = call_wall_exposure.get(strike, 0.0) + abs(raw_gex)
            call_open_interest_by_strike[strike] = call_open_interest_by_strike.get(strike, 0.0) + oi
        else:
            put_wall_exposure[strike] = put_wall_exposure.get(strike, 0.0) - abs(raw_gex)
            put_open_interest_by_strike[strike] = put_open_interest_by_strike.get(strike, 0.0) + oi

        option_rows.append(
            {
                "strike": strike,
                "option_type": option_type,
                "open_interest": oi,
                "time_to_expiry": max((dte_days or 0.0) / 365.0, 1.0 / 262.0 if dte_days == 0 else 0.0),
                "volatility": volatility or 0.0,
            }
        )
        used_rows += 1

    if payload_spot is None and current_spot is None:
        raise ValueError("Could not infer underlying spot from Barchart page or inline gamma payload")
    if not option_rows:
        raise ValueError("No usable option rows after EOD field filtering")

    strikes = sorted(strike_points)
    net_gex_curve: list[tuple[float, float]] = []
    for scenario in strikes:
        total_exposure = 0.0
        for option in option_rows:
            gamma = _bs_gamma(
                scenario,
                float(option["strike"]),
                float(option["time_to_expiry"]),
                float(option["volatility"]),
            )
            if gamma == 0:
                continue
            distance = abs(scenario - float(option["strike"])) / scenario
            smoothed_gamma = gamma * math.exp(-(distance * distance) * 25.0)
            exposure = smoothed_gamma * float(option["open_interest"]) * 100.0 * (scenario**2) * 0.01
            if option["option_type"] == "call":
                total_exposure += exposure
            else:
                total_exposure -= exposure
        net_gex_curve.append((scenario, total_exposure))

    call_wall = max(call_wall_exposure.items(), key=lambda item: item[1])[0] if call_wall_exposure else None
    put_wall = min(put_wall_exposure.items(), key=lambda item: item[1])[0] if put_wall_exposure else None
    signed_gex_by_strike = {
        strike: call_wall_exposure.get(strike, 0.0) + put_wall_exposure.get(strike, 0.0)
        for strike in strikes
    }
    interpretation_spot = current_spot if current_spot is not None else payload_spot

    near_support_levels = _find_local_peak_levels(
        signed_exposure_by_strike=signed_gex_by_strike,
        spot=interpretation_spot,
        side="below",
        window_pct=decision_window_pct,
        max_levels=max_decision_levels,
    )
    near_resistance_levels = _find_local_peak_levels(
        signed_exposure_by_strike=signed_gex_by_strike,
        spot=interpretation_spot,
        side="above",
        window_pct=decision_window_pct,
        max_levels=max_decision_levels,
    )

    flips: list[float] = []
    flip_interval = None
    for i in range(1, len(net_gex_curve)):
        s0, g0 = net_gex_curve[i - 1]
        s1, g1 = net_gex_curve[i]
        if g0 < 0 < g1 or g0 > 0 > g1:
            flip = s0 - g0 * (s1 - s0) / (g1 - g0)
            flips.append(flip)
            if flip_interval is None:
                flip_interval = (s0, s1)

    gamma_flip = min(flips, key=lambda value: abs(value - interpretation_spot)) if flips else None

    result = {
        "spot": interpretation_spot,
        "current_spot": current_spot,
        "payload_spot": payload_spot,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_flip": gamma_flip,
        "near_support_levels": near_support_levels,
        "near_resistance_levels": near_resistance_levels,
    }

    if include_debug:
        result.update({
            "flip_interval": flip_interval,
            "all_flips": flips,
            "total_net_gex": sum(value for _, value in net_gex_curve),
            "interpretation_spot": interpretation_spot,
            "strikes": strikes,
            "net_gex_by_strike": dict(net_gex_curve),
            "rows_used": used_rows,
            "signed_gex_by_strike": signed_gex_by_strike,
            "call_wall_exposure_by_strike": call_wall_exposure,
            "put_wall_exposure_by_strike": put_wall_exposure,
            "call_open_interest_by_strike": call_open_interest_by_strike,
            "put_open_interest_by_strike": put_open_interest_by_strike,
        })

    return result


def fetch_barchart_html(ticker: str, timeout: float = 20.0) -> str:
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("Ticker is required")

    safe_ticker = urllib.parse.quote(ticker, safe="$")
    url = BARCHART_GEX_URL.format(ticker=safe_ticker)
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def fetch_barchart_levels(ticker: str, include_debug: bool = False, timeout: float = 20.0) -> dict:
    html = fetch_barchart_html(ticker=ticker, timeout=timeout)
    result = extract_barchart_levels(html, include_debug=include_debug)
    result["ticker"] = ticker.strip().upper()
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("Usage: python barchart_gex_parser.py <html-file-or-ticker>")

    arg = sys.argv[1]
    if arg.lower().endswith(".html"):
        with open(arg, "r", encoding="utf-8") as f:
            html = f.read()
        result = extract_barchart_levels(html)
    else:
        result = fetch_barchart_levels(arg, include_debug=True)

    print(json.dumps(result, indent=2, sort_keys=True))
