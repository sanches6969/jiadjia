import os
import sys
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify

# Корень репо (рядом с api/) — чтобы engine_core / binance_live / sheets_live импортировались на Vercel
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine_core as ec
import binance_live as bl
import sheets_live as sh

app = Flask(__name__)

# Несколько символов через запятую (по умолчанию BTC + ETH)
_SYMBOLS_RAW = os.environ.get("SYMBOLS", os.environ.get("SYMBOL", "BTCUSDT,ETHUSDT"))
SYMBOLS = [s.strip().upper() for s in _SYMBOLS_RAW.split(",") if s.strip()]
if not SYMBOLS:
    SYMBOLS = ["BTCUSDT", "ETHUSDT"]

RISK_PCT = float(os.environ.get("RISK_PCT", "1"))
MAX_LEVERAGE = float(os.environ.get("MAX_LEVERAGE", "10"))
DEFAULT_BALANCE = float(os.environ.get("INITIAL_BALANCE", "1000"))
KLINES_LIMIT = int(os.environ.get("KLINES_LIMIT", "500"))
ENTRY_LOOKBACK_BARS = int(os.environ.get("ENTRY_LOOKBACK_BARS", "45"))
# После SL не долбим тот же сетап сразу (у тебя это жрало депозит на QM)
COOLDOWN_MINUTES = float(os.environ.get("COOLDOWN_MINUTES", "90"))
# Шумовой стоп — только совсем микро-гэпы; 0.10% не режет нормальные FVG
MIN_SL_DISTANCE_PCT = float(os.environ.get("MIN_SL_DISTANCE_PCT", "0.10"))

# ==============================================================================
# Возврат к ТВОЕЙ прибыльной схеме: низкий WR + высокий RR.
# FVG: 4R без partial (как в исходном live).
# QM 5m: 3R + partial 50%@1R (BE после первого тейка).
# QM 15m выключен — единственный стабильно минусовой джоб в статистике.
# FVG_REBALANCE_1m ВЫКЛЮЧЕН 2026-09-23: по статистике 7/7 сделок закрыты по
# SL (0% winrate, -67.36 суммарно на обоих символах). На 1-минутном графике
# BTC/ETH обычный шум одной свечи регулярно превышает MIN_SL_DISTANCE_PCT
# (0.10%), поэтому стоп выбивает почти всегда, ещё до того как сетап успевает
# отработать. Если захочешь вернуть — либо подними для этого job'а стоп
# отдельно (напр. свой MIN_SL_DISTANCE_PCT ~0.20-0.25%), либо просто убери
# комментарии ниже.
# ==============================================================================
_JOB_TEMPLATES = [
    {
        "strategy": "FVG_REBALANCE",
        "interval": "5m", "htf_interval": "1h",
        "fixed_tp_r": 4.0, "partial_r": None, "partial_pct": 0.0,  # 4R no partial
        "suffix": "FVG_REBALANCE_5m",
    },
    {
        "strategy": "QUASIMODO_POI",
        "interval": "5m", "htf_interval": "1h",
        "fixed_tp_r": 3.0, "partial_r": 1.0, "partial_pct": 0.5,  # 3R + 50%@1R
        "suffix": "QUASIMODO_POI_5m",
    },
    # {
    #     "strategy": "FVG_REBALANCE",
    #     "interval": "1m", "htf_interval": "15m",
    #     "fixed_tp_r": 4.0, "partial_r": None, "partial_pct": 0.0,  # 4R no partial
    #     "suffix": "FVG_REBALANCE_1m",
    # },
]

JOBS = []
for _sym in SYMBOLS:
    for _tpl in _JOB_TEMPLATES:
        JOBS.append({
            "id": f"{_sym}_{_tpl['suffix']}",
            "symbol": _sym,
            "strategy": _tpl["strategy"],
            "interval": _tpl["interval"],
            "htf_interval": _tpl["htf_interval"],
            "fixed_tp_r": _tpl["fixed_tp_r"],
            "partial_r": _tpl["partial_r"],
            "partial_pct": _tpl["partial_pct"],
        })


def build_base_cfg():
    cfg = ec.Config()
    cfg.RISK_PER_TRADE_PCT = RISK_PCT
    cfg.MAX_LEVERAGE = MAX_LEVERAGE
    cfg.INITIAL_BALANCE = DEFAULT_BALANCE
    return cfg


def build_job_cfg(job: dict, base_cfg: ec.Config) -> ec.Config:
    """Клонирует базовый конфиг и подставляет схему выхода конкретного джоба."""
    if hasattr(ec, "clone_config"):
        cfg = ec.clone_config(base_cfg)
    else:
        cfg = ec.Config()
        cfg.RISK_PER_TRADE_PCT = base_cfg.RISK_PER_TRADE_PCT
        cfg.MAX_LEVERAGE = base_cfg.MAX_LEVERAGE
        cfg.INITIAL_BALANCE = base_cfg.INITIAL_BALANCE
    if job["partial_r"] is not None and job["partial_pct"] > 0:
        cfg.USE_PARTIAL_TP = True
        cfg.PARTIAL_TP_LEVELS = [(job["partial_r"], job["partial_pct"])]
        cfg.MOVE_SL_TO_BE_AFTER_FIRST_TP = True
    else:
        cfg.USE_PARTIAL_TP = False
        cfg.PARTIAL_TP_LEVELS = []
    return cfg


def fixed_tp_price(entry: float, direction: str, sl: float, r: float) -> float:
    risk = abs(entry - sl)
    sign = 1 if direction == "long" else -1
    return entry + sign * r * risk


def liq_price_for(entry, direction, cfg):
    if not cfg.MODEL_LIQUIDATION or cfg.MAX_LEVERAGE <= 0:
        return None
    liq_dist_pct = max(1.0 / cfg.MAX_LEVERAGE - cfg.MAINTENANCE_MARGIN_RATE, 0.001)
    return entry * (1 - liq_dist_pct) if direction == "long" else entry * (1 + liq_dist_pct)


def check_open_position(state: dict, current_price: float, cfg: ec.Config):
    """Ликвидация -> стоп/БУ -> финальный TP -> частичный тейк (если задан)."""
    direction = state["direction"]
    entry = state["entry"]
    risk = abs(entry - state["initial_sl"])
    sign = 1 if direction == "long" else -1
    is_long = direction == "long"

    if state.get("liq_price") is not None:
        hit_liq = (current_price <= state["liq_price"]) if is_long else (current_price >= state["liq_price"])
        liq_closer = abs(state["liq_price"] - entry) <= abs(state["sl"] - entry)
        if hit_liq and liq_closer:
            return {"action": "close", "exit_price": state["liq_price"], "reason": "LIQUIDATION"}

    hit_sl = (current_price <= state["sl"]) if is_long else (current_price >= state["sl"])
    if hit_sl:
        reason = "BE" if state["partials_taken"] > 0 else "SL"
        return {"action": "close", "exit_price": state["sl"], "reason": reason}

    hit_tp = (current_price >= state["tp"]) if is_long else (current_price <= state["tp"])
    if hit_tp:
        return {"action": "close", "exit_price": state["tp"], "reason": "TP"}

    levels = cfg.PARTIAL_TP_LEVELS
    idx = state["partials_taken"]
    if idx < len(levels) and risk > 0:
        r, pct = levels[idx]
        level_price = entry + sign * r * risk
        hit_level = (current_price >= level_price) if is_long else (current_price <= level_price)
        if hit_level:
            close_qty = min(state["qty"] * pct, state["qty"])
            return {
                "action": "partial", "exit_price": level_price, "close_qty": close_qty,
                "level_r": r, "new_partials_taken": idx + 1,
                "move_be": cfg.MOVE_SL_TO_BE_AFTER_FIRST_TP and idx == 0,
            }

    return {"action": "none"}


# Ширина зоны входа не должна съедать риск: если зона, где мы ещё согласны
# исполниться, шире, чем доля от дистанции до SL, реальная цена филла может
# оказаться намного ближе к стопу, чем думает сайзинг позиции — отсюда
# мгновенные SL через 2-10 минут после входа при формально нормальном RR.
MAX_ZONE_TO_RISK_FRAC = float(os.environ.get("MAX_ZONE_TO_RISK_FRAC", "0.3"))


def find_live_entry(setups, current_price: float, last_bar_index: int):
    """Берём самый свежий валидный сетап. Доп. фильтр: цена должна быть
    внутри зоны входа, реальный риск (от ТЕКУЩЕЙ цены до SL, а не от
    исторического entry_ref_price сетапа) не шумовой, и ширина зоны входа
    ограничена долей от этого риска (MAX_ZONE_TO_RISK_FRAC) — иначе
    фактический филл может оказаться намного ближе к стопу, чем думает
    сайзинг позиции."""
    for s in sorted(setups, key=lambda s: s.formed_index, reverse=True):
        if last_bar_index - s.formed_index > ENTRY_LOOKBACK_BARS:
            continue
        lo, hi = sorted([s.entry_zone_bottom, s.entry_zone_top])
        if not (lo <= current_price <= hi):
            continue
        risk_to_sl = abs(current_price - s.sl_price)
        if risk_to_sl <= 0:
            continue
        risk_pct = risk_to_sl / current_price * 100.0
        if risk_pct < MIN_SL_DISTANCE_PCT:
            continue  # стоп слишком узкий (шум)
        zone_width = hi - lo
        if zone_width > risk_to_sl * MAX_ZONE_TO_RISK_FRAC:
            continue  # зона входа слишком широкая относительно дистанции до стопа
        return s
    return None


def process_job(job: dict, state, current_price: float, base_cfg: ec.Config, context_cache: dict) -> dict:
    """Обрабатывает одну джобу (проверка открытой позиции ИЛИ поиск нового
    входа). Вынесено в отдельную функцию, чтобы вызывающий код мог обернуть
    её в try/except на уровне ОДНОЙ джобы — падение одной не должно мешать
    проверке остальных.
    job["symbol"] — конкретная пара (BTCUSDT / ETHUSDT).
    """
    job_id = job["id"]
    symbol = job["symbol"]
    cfg = build_job_cfg(job, base_cfg)

    if state and state.get("symbol") == symbol:
        result = check_open_position(state, current_price, cfg)

        if result["action"] == "partial":
            pnl_part = (result["exit_price"] - state["entry"]) * result["close_qty"] if state["direction"] == "long" \
                else (state["entry"] - result["exit_price"]) * result["close_qty"]
            sign_str = "+" if pnl_part >= 0 else ""
            sh.append_partial_note(
                state["trade_row"],
                f"Частичный тейк {sign_str}{pnl_part:.2f}$ на {result['level_r']}R"
            )
            new_state = dict(state)
            new_state["qty"] = state["qty"] - result["close_qty"]
            new_state["partials_taken"] = result["new_partials_taken"]
            if result["move_be"]:
                new_state["sl"] = state["entry"]
            sh.set_state(job_id, new_state)
            return {"status": "partial_tp", "pnl_part": round(pnl_part, 2), "symbol": symbol}

        if result["action"] == "close":
            pnl = (result["exit_price"] - state["entry"]) * state["qty"] if state["direction"] == "long" \
                else (state["entry"] - result["exit_price"]) * state["qty"]
            pnl_pct = (pnl / (state["entry"] * state["qty"])) * 100 if state["qty"] else 0.0
            sh.update_trade_close(state["trade_row"], result["exit_price"], pnl, pnl_pct, result["reason"])
            new_balance = sh.update_summary(pnl)
            sh.clear_state(job_id)
            sh.set_cooldown_until(job_id, datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES))
            return {
                "status": "closed", "reason": result["reason"],
                "pnl": round(pnl, 2), "balance": round(new_balance, 2), "symbol": symbol,
            }

        return {"status": "holding", "symbol": symbol}

    cooldown_until = sh.get_cooldown_until(job_id)
    if cooldown_until is not None and cooldown_until > datetime.now(timezone.utc):
        return {"status": "cooldown", "until": cooldown_until.isoformat(), "symbol": symbol}

    # кэш по (symbol, interval, htf) — BTC и ETH не смешиваем
    cache_key = (symbol, job["interval"], job["htf_interval"])
    if cache_key not in context_cache:
        ltf_df = bl.get_klines(symbol, job["interval"], KLINES_LIMIT)
        ltf_ctx = ec.build_smc_context(ltf_df, base_cfg)
        htf_df = ec.resample_ohlcv(ltf_df, job["htf_interval"])
        htf_ctx = ec.build_smc_context(htf_df, base_cfg)
        context_cache[cache_key] = (ltf_df, ltf_ctx, htf_ctx)
    ltf_df, ltf_ctx, htf_ctx = context_cache[cache_key]

    setups = ec.generate_setups(job["strategy"], ltf_ctx, htf_ctx, base_cfg)
    last_idx = len(ltf_df) - 1
    setup = find_live_entry(setups, current_price, last_idx)

    if not setup:
        return {"status": "no_signal", "symbol": symbol}

    # Реальный вход — по факту исполнения (текущая цена тика), а не
    # исторический entry_ref_price сетапа. SL остаётся на структурном
    # уровне (там сетап реально инвалидируется), но риск/сайзинг/TP
    # считаем от настоящей цены входа — иначе R-мультипл строится на
    # дистанции, которой к моменту факта входа уже могло не быть.
    entry_price = current_price
    sl_price = setup.sl_price
    risk_per_unit = abs(entry_price - sl_price)

    if risk_per_unit <= 0:
        return {"status": "invalid_risk", "symbol": symbol}

    balance = sh.get_balance(DEFAULT_BALANCE)
    risk_amount = balance * (RISK_PCT / 100.0)
    qty = risk_amount / risk_per_unit

    if qty <= 0:
        return {"status": "invalid_qty", "symbol": symbol}

    liq_price = liq_price_for(entry_price, setup.direction, cfg)
    tp_price = (
        fixed_tp_price(entry_price, setup.direction, sl_price, job["fixed_tp_r"])
        if job["fixed_tp_r"] is not None else setup.tp_price
    )
    trade_row = sh.append_trade_open(
        job_id, symbol, setup.direction, setup.reason,
        entry_price, sl_price, tp_price, qty,
    )
    sh.set_state(job_id, {
        "symbol": symbol, "direction": setup.direction,
        "entry": entry_price, "initial_sl": sl_price,
        "sl": sl_price, "tp": tp_price, "qty": qty,
        "partials_taken": 0, "trade_row": trade_row, "liq_price": liq_price,
    })
    return {
        "status": "opened", "direction": setup.direction,
        "reason": setup.reason, "qty": round(qty, 6), "symbol": symbol,
    }


@app.route("/api/scan", methods=["GET"])
def scan():
    secret = request.args.get("secret") or (request.headers.get("Authorization", "").replace("Bearer ", ""))
    if secret != os.environ.get("CRON_SECRET"):
        return jsonify({"error": "unauthorized"}), 401

    base_cfg = build_base_cfg()
    results = {}
    context_cache = {}
    prices = {}

    try:
        all_states = sh.get_all_states()
        for sym in SYMBOLS:
            prices[sym] = bl.get_current_price(sym)
    except Exception as e:
        return jsonify({"error": f"critical: {e}"}), 500

    for job in JOBS:
        job_id = job["id"]
        symbol = job["symbol"]
        try:
            state = all_states.get(job_id)
            current_price = prices[symbol]
            results[job_id] = process_job(job, state, current_price, base_cfg, context_cache)
        except Exception as e:
            results[job_id] = {"status": "error", "error": str(e), "symbol": symbol}

    return jsonify({"prices": prices, "symbols": SYMBOLS, "results": results})


if __name__ == "__main__":
    app.run(port=3000, debug=True)
