import os
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify

import engine_core as ec
import binance_live as bl
import sheets_live as sh

app = Flask(__name__)

SYMBOL = os.environ.get("SYMBOL", "ETHUSDT")
INTERVAL = os.environ.get("INTERVAL", "15m")
HTF_INTERVAL = os.environ.get("HTF_INTERVAL", "4h")
RISK_PCT = float(os.environ.get("RISK_PCT", "1"))
MAX_LEVERAGE = float(os.environ.get("MAX_LEVERAGE", "10"))
DEFAULT_BALANCE = float(os.environ.get("INITIAL_BALANCE", "1000"))
KLINES_LIMIT = int(os.environ.get("KLINES_LIMIT", "500"))
ENTRY_LOOKBACK_BARS = int(os.environ.get("ENTRY_LOOKBACK_BARS", "50"))  # насколько "свежим" должен быть сетап
# после закрытия любой сделки по стратегии не открывать новую это же
# кол-во минут — защита от мгновенного повторного входа в ту же зону
COOLDOWN_MINUTES = float(os.environ.get("COOLDOWN_MINUTES", "60"))

# Зафиксированная по итогам RR-sweep комбинация: QUASIMODO_POI, 30% на +1.5R
# -> перенос SL в БУ -> полный выход оставшихся 70% на +4R (не структурный TP).
# Именно эта схема дала лучший PnL в бэктестах на ETHUSDT 15m.
STRATEGIES_TO_RUN = os.environ.get("STRATEGIES_TO_RUN", "QUASIMODO_POI").split(",")
FIXED_TP_R = float(os.environ.get("FIXED_TP_R", "4.0"))
PARTIAL_R = float(os.environ.get("PARTIAL_R", "1.5"))
PARTIAL_PCT = float(os.environ.get("PARTIAL_PCT", "0.30"))


def build_cfg():
    cfg = ec.Config()
    cfg.RISK_PER_TRADE_PCT = RISK_PCT
    cfg.MAX_LEVERAGE = MAX_LEVERAGE
    cfg.INITIAL_BALANCE = DEFAULT_BALANCE
    # переопределяем дефолтную 3-уровневую схему партиалов на конкретную
    # выигрышную комбинацию, найденную в RR-sweep (см. PARTIAL_R/PARTIAL_PCT выше)
    cfg.USE_PARTIAL_TP = True
    cfg.PARTIAL_TP_LEVELS = [(PARTIAL_R, PARTIAL_PCT)]
    cfg.MOVE_SL_TO_BE_AFTER_FIRST_TP = True
    return cfg


def fixed_tp_price(entry: float, direction: str, sl: float) -> float:
    """Финальный TP как фиксированный R-мультипликатор риска (FIXED_TP_R),
    а не структурная цель стратегии — воспроизводит комбинацию
    fixed_4R_partial_30@1.5R из твоего RR-sweep."""
    risk = abs(entry - sl)
    sign = 1 if direction == "long" else -1
    return entry + sign * FIXED_TP_R * risk


def liq_price_for(entry, direction, cfg):
    if not cfg.MODEL_LIQUIDATION or cfg.MAX_LEVERAGE <= 0:
        return None
    liq_dist_pct = max(1.0 / cfg.MAX_LEVERAGE - cfg.MAINTENANCE_MARGIN_RATE, 0.001)
    return entry * (1 - liq_dist_pct) if direction == "long" else entry * (1 + liq_dist_pct)


def check_open_position(state: dict, current_price: float, cfg: ec.Config):
    """
    Проверяет одну открытую позицию против текущей цены: ликвидация -> стоп/БУ ->
    структурный TP -> частичные тейки (по R от ПЕРВОНАЧАЛЬНОГО риска, как в
    backtest-движке: cfg.PARTIAL_TP_LEVELS). Возвращает dict с описанием действия.
    """
    direction = state["direction"]
    entry = state["entry"]
    risk = abs(entry - state["initial_sl"])
    sign = 1 if direction == "long" else -1
    is_long = direction == "long"

    # 1) ликвидация (если ближе, чем текущий стоп)
    if state.get("liq_price") is not None:
        hit_liq = (current_price <= state["liq_price"]) if is_long else (current_price >= state["liq_price"])
        liq_closer = abs(state["liq_price"] - entry) <= abs(state["sl"] - entry)
        if hit_liq and liq_closer:
            return {"action": "close", "exit_price": state["liq_price"], "reason": "LIQUIDATION"}

    # 2) стоп / БУ
    hit_sl = (current_price <= state["sl"]) if is_long else (current_price >= state["sl"])
    if hit_sl:
        reason = "BE" if state["partials_taken"] > 0 else "SL"
        return {"action": "close", "exit_price": state["sl"], "reason": reason}

    # 3) структурный финальный TP
    hit_tp = (current_price >= state["tp"]) if is_long else (current_price <= state["tp"])
    if hit_tp:
        return {"action": "close", "exit_price": state["tp"], "reason": "TP"}

    # 4) частичные тейки по уровням из cfg.PARTIAL_TP_LEVELS (R от начального риска)
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
    """Берет самый свежий сетап, чья зона входа уже задета текущей ценой."""
    for s in sorted(setups, key=lambda s: s.formed_index, reverse=True):
        if last_bar_index - s.formed_index > ENTRY_LOOKBACK_BARS:
            continue
        lo, hi = sorted([s.entry_zone_bottom, s.entry_zone_top])
        if lo <= current_price <= hi:
            return s
    return None


@app.route("/api/scan", methods=["GET"])
def scan():
    secret = request.args.get("secret") or (request.headers.get("Authorization", "").replace("Bearer ", ""))
    if secret != os.environ.get("CRON_SECRET"):
        return jsonify({"error": "unauthorized"}), 401

    cfg = build_cfg()
    results = {}

    try:
        current_price = bl.get_current_price(SYMBOL)
        all_states = sh.get_all_states()

        # сетапы считаем один раз на все стратегии (дорогая операция —
        # структура/OB/FVG/ликвидность), а не по отдельности в каждой ветке
        ltf_df = None
        htf_df = None
        ltf_ctx = None
        htf_ctx = None

        for strategy in STRATEGIES_TO_RUN:
            state = all_states.get(strategy)

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
                    sh.set_state(strategy, new_state)
                    results[strategy] = {"status": "partial_tp", "pnl_part": round(pnl_part, 2)}
                    continue

                if result["action"] == "close":
                    pnl = (result["exit_price"] - state["entry"]) * state["qty"] if state["direction"] == "long" \
                        else (state["entry"] - result["exit_price"]) * state["qty"]
                    pnl_pct = (pnl / (state["entry"] * state["qty"])) * 100 if state["qty"] else 0.0
                    sh.update_trade_close(state["trade_row"], result["exit_price"], pnl, pnl_pct, result["reason"])
                    new_balance = sh.update_summary(pnl)
                    sh.clear_state(strategy)
                    sh.set_cooldown_until(strategy, datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES))
                    results[strategy] = {"status": "closed", "reason": result["reason"], "pnl": round(pnl, 2), "balance": round(new_balance, 2)}
                    continue

                results[strategy] = {"status": "holding"}
                continue

            # cooldown после недавнего закрытия — не спешим открывать новую сделку
            cooldown_until = sh.get_cooldown_until(strategy)
            if cooldown_until is not None and cooldown_until > datetime.now(timezone.utc):
                results[strategy] = {"status": "cooldown", "until": cooldown_until.isoformat()}
                continue

            # позиции нет — считаем сетапы (лениво, один раз на все стратегии)
            if ltf_ctx is None:
                ltf_df = bl.get_klines(SYMBOL, INTERVAL, KLINES_LIMIT)
                ltf_ctx = ec.build_smc_context(ltf_df, cfg)
                htf_df = ec.resample_ohlcv(ltf_df, HTF_INTERVAL)
                htf_ctx = ec.build_smc_context(htf_df, cfg)

            setups = ec.generate_setups(strategy, ltf_ctx, htf_ctx, cfg)
            last_idx = len(ltf_df) - 1
            setup = find_live_entry(setups, current_price, last_idx)

            if not setup:
                results[strategy] = {"status": "no_signal"}
                continue

            balance = sh.get_balance(DEFAULT_BALANCE)
            risk_amount = balance * (RISK_PCT / 100.0)
            risk_per_unit = abs(setup.entry_ref_price - setup.sl_price)
            qty = risk_amount / risk_per_unit if risk_per_unit > 0 else 0

            if qty <= 0:
                results[strategy] = {"status": "invalid_qty"}
                continue

            liq_price = liq_price_for(setup.entry_ref_price, setup.direction, cfg)
            tp_price = fixed_tp_price(setup.entry_ref_price, setup.direction, setup.sl_price)
            trade_row = sh.append_trade_open(
                strategy, SYMBOL, setup.direction, setup.reason,
                setup.entry_ref_price, setup.sl_price, tp_price, qty,
            )
            sh.set_state(strategy, {
                "symbol": SYMBOL, "direction": setup.direction,
                "entry": setup.entry_ref_price, "initial_sl": setup.sl_price,
                "sl": setup.sl_price, "tp": tp_price, "qty": qty,
                "partials_taken": 0, "trade_row": trade_row, "liq_price": liq_price,
            })
            results[strategy] = {"status": "opened", "direction": setup.direction, "reason": setup.reason, "qty": round(qty, 6)}

        return jsonify({"price": current_price, "results": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# локальный запуск для отладки: python api/index.py
if __name__ == "__main__":
    app.run(port=3000, debug=True)
