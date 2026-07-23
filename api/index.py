import os
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify

import engine_core as ec
import binance_live as bl
import sheets_live as sh

app = Flask(__name__)

SYMBOL = os.environ.get("SYMBOL", "ETHUSDT")
RISK_PCT = float(os.environ.get("RISK_PCT", "1"))
MAX_LEVERAGE = float(os.environ.get("MAX_LEVERAGE", "10"))
DEFAULT_BALANCE = float(os.environ.get("INITIAL_BALANCE", "1000"))
KLINES_LIMIT = int(os.environ.get("KLINES_LIMIT", "500"))
ENTRY_LOOKBACK_BARS = int(os.environ.get("ENTRY_LOOKBACK_BARS", "50"))
COOLDOWN_MINUTES = float(os.environ.get("COOLDOWN_MINUTES", "60"))
# отсекает сетапы с шумовым (слишком узким) стопом — частая проблема на 1m,
# где гэп между свечами иногда получается почти нулевым; такие входы
# статистически почти всегда сразу вылетают по стопу
MIN_SL_DISTANCE_PCT = float(os.environ.get("MIN_SL_DISTANCE_PCT", "0.15"))

# ==============================================================================
# 4 независимые конфигурации, отобранные по итогам RR-sweep на ETHUSDT/30 дней.
# Каждая работает как отдельный "джоб" со своим ТФ/HTF/схемой выхода и своим
# независимым состоянием в Google Sheets (ключ — "id"). Одна и та же стратегия
# может быть запущена на разных ТФ одновременно (см. QUASIMODO_POI 5m и 15m) —
# именно поэтому ключ состояния "id", а не голое имя стратегии.
#
#   fixed_tp_r=None + partial_r=None -> "no_partial": просто один финальный
#     выход на fixed_tp_r без частичных тейков (cfg.USE_PARTIAL_TP=False)
# ==============================================================================
JOBS = [
    {
        "id": "FVG_REBALANCE_5m", "strategy": "FVG_REBALANCE",
        "interval": "5m", "htf_interval": "1h",
        "fixed_tp_r": 4.0, "partial_r": None, "partial_pct": 0.0,  # fixed_4R_no_partial
    },
    {
        "id": "QUASIMODO_POI_5m", "strategy": "QUASIMODO_POI",
        "interval": "5m", "htf_interval": "1h",
        "fixed_tp_r": 3.0, "partial_r": 1.0, "partial_pct": 0.5,  # fixed_3R_partial_50@1R
    },
    {
        "id": "FVG_REBALANCE_1m", "strategy": "FVG_REBALANCE",
        "interval": "1m", "htf_interval": "15m",
        "fixed_tp_r": 4.0, "partial_r": None, "partial_pct": 0.0,  # fixed_4R_no_partial
    },
    {
        "id": "QUASIMODO_POI_15m", "strategy": "QUASIMODO_POI",
        "interval": "15m", "htf_interval": "4h",
        "fixed_tp_r": 2.0, "partial_r": 1.0, "partial_pct": 0.5,  # fixed_2R_partial_50@1R
    },
]


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


def find_live_entry(setups, current_price: float, last_bar_index: int):
    for s in sorted(setups, key=lambda s: s.formed_index, reverse=True):
        if last_bar_index - s.formed_index > ENTRY_LOOKBACK_BARS:
            continue
        lo, hi = sorted([s.entry_zone_bottom, s.entry_zone_top])
        if not (lo <= current_price <= hi):
            continue
        risk_pct = abs(s.entry_ref_price - s.sl_price) / s.entry_ref_price * 100.0
        if risk_pct < MIN_SL_DISTANCE_PCT:
            continue  # стоп слишком узкий (шум) — пропускаем этот сетап, ищем следующий
        return s
    return None


def process_job(job: dict, state, current_price: float, base_cfg: ec.Config, context_cache: dict) -> dict:
    """Обрабатывает одну джобу (проверка открытой позиции ИЛИ поиск нового
    входа). Вынесено в отдельную функцию, чтобы вызывающий код мог обернуть
    её в try/except на уровне ОДНОЙ джобы — падение одной не должно мешать
    проверке остальных (иначе, например, зависшая FVG_REBALANCE_1m могла бы
    не давать проверяться QUASIMODO_POI_15m в тот же тик)."""
    job_id = job["id"]
    cfg = build_job_cfg(job, base_cfg)

    if state and state["symbol"] == SYMBOL:
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
            return {"status": "partial_tp", "pnl_part": round(pnl_part, 2)}

        if result["action"] == "close":
            pnl = (result["exit_price"] - state["entry"]) * state["qty"] if state["direction"] == "long" \
                else (state["entry"] - result["exit_price"]) * state["qty"]
            pnl_pct = (pnl / (state["entry"] * state["qty"])) * 100 if state["qty"] else 0.0
            sh.update_trade_close(state["trade_row"], result["exit_price"], pnl, pnl_pct, result["reason"])
            new_balance = sh.update_summary(pnl)
            sh.clear_state(job_id)
            sh.set_cooldown_until(job_id, datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES))
            return {"status": "closed", "reason": result["reason"], "pnl": round(pnl, 2), "balance": round(new_balance, 2)}

        return {"status": "holding"}

    cooldown_until = sh.get_cooldown_until(job_id)
    if cooldown_until is not None and cooldown_until > datetime.now(timezone.utc):
        return {"status": "cooldown", "until": cooldown_until.isoformat()}

    cache_key = (job["interval"], job["htf_interval"])
    if cache_key not in context_cache:
        ltf_df = bl.get_klines(SYMBOL, job["interval"], KLINES_LIMIT)
        ltf_ctx = ec.build_smc_context(ltf_df, base_cfg)
        htf_df = ec.resample_ohlcv(ltf_df, job["htf_interval"])
        htf_ctx = ec.build_smc_context(htf_df, base_cfg)
        context_cache[cache_key] = (ltf_df, ltf_ctx, htf_ctx)
    ltf_df, ltf_ctx, htf_ctx = context_cache[cache_key]

    setups = ec.generate_setups(job["strategy"], ltf_ctx, htf_ctx, base_cfg)
    last_idx = len(ltf_df) - 1
    setup = find_live_entry(setups, current_price, last_idx)

    if not setup:
        return {"status": "no_signal"}

    balance = sh.get_balance(DEFAULT_BALANCE)
    risk_amount = balance * (RISK_PCT / 100.0)
    risk_per_unit = abs(setup.entry_ref_price - setup.sl_price)
    qty = risk_amount / risk_per_unit if risk_per_unit > 0 else 0

    if qty <= 0:
        return {"status": "invalid_qty"}

    liq_price = liq_price_for(setup.entry_ref_price, setup.direction, cfg)
    tp_price = (
        fixed_tp_price(setup.entry_ref_price, setup.direction, setup.sl_price, job["fixed_tp_r"])
        if job["fixed_tp_r"] is not None else setup.tp_price
    )
    trade_row = sh.append_trade_open(
        job_id, SYMBOL, setup.direction, setup.reason,
        setup.entry_ref_price, setup.sl_price, tp_price, qty,
    )
    sh.set_state(job_id, {
        "symbol": SYMBOL, "direction": setup.direction,
        "entry": setup.entry_ref_price, "initial_sl": setup.sl_price,
        "sl": setup.sl_price, "tp": tp_price, "qty": qty,
        "partials_taken": 0, "trade_row": trade_row, "liq_price": liq_price,
    })
    return {"status": "opened", "direction": setup.direction, "reason": setup.reason, "qty": round(qty, 6)}


@app.route("/api/scan", methods=["GET"])
def scan():
    secret = request.args.get("secret") or (request.headers.get("Authorization", "").replace("Bearer ", ""))
    if secret != os.environ.get("CRON_SECRET"):
        return jsonify({"error": "unauthorized"}), 401

    base_cfg = build_base_cfg()
    results = {}
    context_cache = {}

    try:
        current_price = bl.get_current_price(SYMBOL)
        all_states = sh.get_all_states()
    except Exception as e:
        # это критично для ВСЕХ джоб сразу (нет цены/состояния — нечего проверять)
        return jsonify({"error": f"critical: {e}"}), 500

    for job in JOBS:
        job_id = job["id"]
        try:
            state = all_states.get(job_id)
            results[job_id] = process_job(job, state, current_price, base_cfg, context_cache)
        except Exception as e:
            # падение ОДНОЙ джобы не должно мешать проверке остальных —
            # именно это раньше могло "подвешивать" другие открытые позиции
            results[job_id] = {"status": "error", "error": str(e)}

    return jsonify({"price": current_price, "results": results})


if __name__ == "__main__":
    app.run(port=3000, debug=True)
