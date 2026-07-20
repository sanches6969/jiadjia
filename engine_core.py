"""
engine_core.py — чистое ядро детекции структуры/сетапов из smc_backtest_engine.py,
без интерактивных промптов, backtest-цикла и визуализации. Используется live-сканером
на Vercel (api/index.py) для генерации сетапов по всем 5 стратегиям в реальном времени.
"""
from __future__ import annotations

import os
import json
import math
import warnings
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

class Config:
    # ----- Источник данных -----
    DATA_SOURCE = "yfinance"        # "yfinance" | "binance" | "csv"
    SYMBOL = "BTC-USD"              # yfinance: "BTC-USD", "EURUSD=X", "^GSPC" ...
                                     # binance:  "BTCUSDT", "ETHUSDT" ...
    CSV_PATH = None                 # путь к своему CSV (если DATA_SOURCE == "csv")
    TIMEFRAME = "1h"                # yfinance: 1m,2m,5m,15m,30m,60m,90m,1h,1d,...
                                     # binance:  1m,5m,15m,1h,4h,1d,...
    PERIOD_DAYS = 180               # сколько дней истории тянуть назад от сегодня
    START_DATE = None               # альтернативно: "2024-01-01"
    END_DATE = None                 # альтернативно: "2024-06-01"

    # ----- Капитал и риск (см. модуль "Риск-менеджмент") -----
    INITIAL_BALANCE = 10_000.0
    RISK_PER_TRADE_PCT = 1.0        # % депозита на сделку (рекоменд. курсом: 0.5-2%)
    MAX_DAILY_RISK_PCT = 5.0        # суммарный риск открытых позиций в день
    COMMISSION_PCT = 0.045          # % от объема за сделку (maker+taker, см. модуль 12)
    MAX_LEVERAGE = 20.0              # ограничение объема позиции (защита от нереалистичных qty при очень узком SL)

    # --- Реалистичность плеча: ликвидация по марже ---
    # По умолчанию биржа (isolated margin) выделяет под позицию маржу = notional/leverage
    # и ЛИКВИДИРУЕТ позицию, если цена уходит против вас примерно на (1/leverage - MMR).
    # Если ваш структурный SL (по свингу/OB) дальше этого расстояния — в реальности вас
    # ликвидируют РАНЬШЕ, чем сработает ваш стоп, и вы теряете всю маржу по позиции,
    # а не только "запланированный" % риска. Это НЕ учитывается по умолчанию в большинстве
    # простых бэктестеров и может сильно завышать прибыль на широких стопах + высоком плече.
    MODEL_LIQUIDATION = True         # моделировать принудительную ликвидацию по марже
    MAINTENANCE_MARGIN_RATE = 0.005  # ~0.5%, грубая оценка (у Binance зависит от размера позиции и пары)
    USE_PARTIAL_TP = True
    PARTIAL_TP_LEVELS = [           # (R-multiple, % объема к закрытию)
        (1.0, 0.50),                # фиксация 50% при RR 1:1 -> перевод в BE
        (3.0, 0.30),                # фиксация 30% при RR 1:3
        (4.0, 0.25),                # фиксация 25% при RR 1:4 (остаток держим к финал TP)
    ]
    MOVE_SL_TO_BE_AFTER_FIRST_TP = True
    MIN_RR_FILTER = 1.5             # не открывать сделку, если потенциальный RR ниже

    # ----- Структура / фракталы -----
    SWING_LEFT = 3                  # баров слева для фрактального свинга
    SWING_RIGHT = 3                 # баров справа для фрактального свинга

    # ----- Какие стратегии гонять -----
    STRATEGIES_TO_RUN = [
        "HTF_POI_LTF_OB",
        "ORDER_FLOW_SWEEP_SHIFT",
        "QUASIMODO_POI",
        "FVG_REBALANCE",
        "RANGE_AGGRESSIVE",
    ]

    # ----- Top-down: соотношение HTF/LTF (см. модуль "Top-Down Analysis") -----
    # Используется для ресемплинга HTF из загруженных LTF данных.
    HTF_RESAMPLE_RULE = "4h"        # старший TF для поиска POI
    LTF_RESAMPLE_RULE = None        # None = использовать TIMEFRAME как есть

    # ----- Вывод -----
    OUTPUT_DIR = "smc_backtest_results"
    SAVE_PLOTS = True
    MAX_TRADE_PLOTS = 1             # сколько отдельных "крупных" графиков с сетапами рисовать


CFG = Config()


# ==============================================================================
# 1. ЗАГРУЗКА ДАННЫХ
# ==============================================================================


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Ресемплирует LTF OHLCV в HTF (используется для top-down контекста)."""
    out = df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    })
    out.dropna(inplace=True)
    return out


# ==============================================================================
# 2. SMC-ИНДИКАТОРЫ
# ==============================================================================
# Все детекторы работают только с уже закрытыми барами (без заглядывания вперед,
# look-ahead bias исключен: свинг на баре i подтверждается лишь после того как
# сформировались SWING_RIGHT баров справа от него).


@dataclass
class Swing:
    index: int            # позиция бара в DataFrame
    time: pd.Timestamp
    price: float
    kind: str              # "high" | "low"


@dataclass
class FVG:
    start_index: int       # индекс свечи ПЕРЕД импульсной (формирует границу)
    time: pd.Timestamp
    kind: str               # "bullish" | "bearish"
    top: float
    bottom: float
    mid: float
    filled_iofed: bool = False
    filled_50: bool = False
    filled_full: bool = False
    invalidated_index: Optional[int] = None


@dataclass
class OrderBlock:
    index: int
    time: pd.Timestamp
    kind: str                # "bullish" | "bearish"
    open_: float
    close_: float
    high: float
    low: float
    mt: float                 # mean threshold (50% тела)
    origin: str                # "liquidity" | "rebalance" | "block"
    is_breaker: bool = False
    breaker_index: Optional[int] = None
    invalidated_index: Optional[int] = None


@dataclass
class LiquidityLevel:
    index: int
    time: pd.Timestamp
    price: float
    kind: str               # "BSL" (buy-side, над swing high) | "SSL" (sell-side, под swing low)
    swept_index: Optional[int] = None


def find_swings(df: pd.DataFrame, left: int, right: int) -> list[Swing]:
    """Фрактальные swing high / swing low (модуль 5 'Рыночная структура')."""
    swings: list[Swing] = []
    highs, lows = df["high"].values, df["low"].values
    n = len(df)
    for i in range(left, n - right):
        window_h = highs[i - left:i + right + 1]
        window_l = lows[i - left:i + right + 1]
        if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
            swings.append(Swing(i, df.index[i], highs[i], "high"))
        if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
            swings.append(Swing(i, df.index[i], lows[i], "low"))
    swings.sort(key=lambda s: s.index)
    return swings


def label_structure(df: pd.DataFrame, swings: list[Swing]) -> pd.DataFrame:
    """
    Определяет тренд и события BOS / CHOCH (модуль 5).
    BOS   — пробой свинга ПО тренду (продолжение).
    CHOCH — пробой свинга ПРОТИВ тренда (смена/слом структуры).
    Возвращает DataFrame событий: index, time, type ('BOS'/'CHOCH'), direction, level.
    """
    events = []
    trend = None  # "up" | "down"
    last_high = None
    last_low = None
    closes = df["close"].values

    # идем по свингам в хронологическом порядке, проверяя пробой ПОСЛЕДУЮЩИМИ барами
    confirmed_swings = sorted(swings, key=lambda s: s.index)
    pending_highs = []  # свинг-хаи, ожидающие пробоя
    pending_lows = []

    for s in confirmed_swings:
        # Перед добавлением нового свинга проверяем, не пробит ли он же предыдущими барами
        pass

    # Простая и устойчивая реализация: проходим по барам, отслеживаем последний
    # подтвержденный swing high/low, и при закрытии бара телом за его уровнем
    # фиксируем BOS/CHOCH.
    sw_sorted = confirmed_swings
    sw_ptr = 0
    active_highs = []  # список (price, index) еще не пробитых
    active_lows = []

    for i in range(len(df)):
        # добавляем свинги, которые "подтвердились" к этому бару (index + right <= i)
        while sw_ptr < len(sw_sorted) and sw_sorted[sw_ptr].index <= i:
            s = sw_sorted[sw_ptr]
            if s.kind == "high":
                active_highs.append(s)
            else:
                active_lows.append(s)
            sw_ptr += 1

        c = closes[i]
        # Пробой вверх (потенциальный BOS в аптренде / CHOCH в даунтренде)
        if active_highs:
            last_h = active_highs[-1]
            if c > last_h.price:
                ev_type = "CHOCH" if trend == "down" else "BOS"
                events.append({
                    "index": i, "time": df.index[i], "type": ev_type,
                    "direction": "up", "level": last_h.price,
                })
                trend = "up"
                last_high = last_h
                active_highs = [s for s in active_highs if s.price > last_h.price]
        if active_lows:
            last_l = active_lows[-1]
            if c < last_l.price:
                ev_type = "CHOCH" if trend == "up" else "BOS"
                events.append({
                    "index": i, "time": df.index[i], "type": ev_type,
                    "direction": "down", "level": last_l.price,
                })
                trend = "down"
                last_low = last_l
                active_lows = [s for s in active_lows if s.price < last_l.price]

    ev_df = pd.DataFrame(events)
    return ev_df


def current_trend_at(structure_events: pd.DataFrame, i: int) -> Optional[str]:
    """Возвращает направление тренда ('up'/'down') на момент бара i."""
    if structure_events.empty:
        return None
    past = structure_events[structure_events["index"] <= i]
    if past.empty:
        return None
    return past.iloc[-1]["direction"]


def find_fvg(df: pd.DataFrame) -> list[FVG]:
    """
    Fair Value Gap — 3-свечная формация дисбаланса (модуль 9).
    Бычий FVG: high(i-1) < low(i+1)  (зона между ними).
    Медвежий FVG: low(i-1) > high(i+1).
    """
    fvgs: list[FVG] = []
    highs, lows = df["high"].values, df["low"].values
    for i in range(1, len(df) - 1):
        h_prev, l_prev = highs[i - 1], lows[i - 1]
        h_next, l_next = highs[i + 1], lows[i + 1]
        if h_prev < l_next:
            top, bottom = l_next, h_prev
            fvgs.append(FVG(i - 1, df.index[i], "bullish", top, bottom, (top + bottom) / 2))
        elif l_prev > h_next:
            top, bottom = l_prev, h_next
            fvgs.append(FVG(i - 1, df.index[i], "bearish", top, bottom, (top + bottom) / 2))
    return fvgs


def update_fvg_fill_status(fvgs: list[FVG], df: pd.DataFrame) -> None:
    """
    Проставляет статусы ребаланса по ходу времени (IOFED / 0.5 / Full Fill),
    а также индекс инвалидации (полное закрытие телом за зону).
    """
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    for gap in fvgs:
        start = gap.start_index + 2
        for j in range(start, len(df)):
            if gap.kind == "bullish":
                if lows[j] <= gap.top and gap.filled_iofed is False and lows[j] > gap.mid:
                    gap.filled_iofed = True
                if lows[j] <= gap.mid:
                    gap.filled_50 = True
                if lows[j] <= gap.bottom:
                    gap.filled_full = True
                if closes[j] < gap.bottom:
                    gap.invalidated_index = j
                    break
            else:
                if highs[j] >= gap.bottom and gap.filled_iofed is False and highs[j] < gap.mid:
                    gap.filled_iofed = True
                if highs[j] >= gap.mid:
                    gap.filled_50 = True
                if highs[j] >= gap.top:
                    gap.filled_full = True
                if closes[j] > gap.top:
                    gap.invalidated_index = j
                    break


def find_order_blocks(df: pd.DataFrame, swings: list[Swing], fvgs: list[FVG]) -> list[OrderBlock]:
    """
    Order Block (модуль 10): последняя противоположная свеча перед импульсным
    поглощением, тестирующая ключевой уровень (свинг, FVG-ребаланс или другой блок).

    Формализация:
      - Бычий OB: нисходящая свеча, тело которой полностью поглощается (close)
        одной из следующих до 3 бычьих свечей; формируется на/возле свинг-лоу
        (снятие SSL) либо в зоне ребаланса FVG.
      - Медвежий OB — зеркально.
    Стоп-лосс по OB всегда выставляется за весь диапазон блока (high/low, включая фитили).
    """
    obs: list[OrderBlock] = []
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    swing_lows = {s.index for s in swings if s.kind == "low"}
    swing_highs = {s.index for s in swings if s.kind == "high"}
    fvg_zones = [(g.bottom, g.top, g.kind) for g in fvgs]

    def near_key_level(i: int) -> Optional[str]:
        # рядом ли цена с свинг-лоу/хай (снятие ликвидности) в пределах последних 5 баров
        for k in range(max(0, i - 5), i + 1):
            if k in swing_lows or k in swing_highs:
                return "liquidity"
        for bottom, top, kind in fvg_zones:
            if bottom <= l[i] <= top or bottom <= h[i] <= top:
                return "rebalance"
        return None

    for i in range(0, len(df) - 3):
        is_down_candle = c[i] < o[i]
        is_up_candle = c[i] > o[i]

        # --- бычий OB: нисходящая свеча + импульсное поглощение бычьими свечами ---
        if is_down_candle:
            for j in range(i + 1, min(i + 4, len(df))):
                if c[j] > o[i]:  # поглощение тела OB
                    origin = near_key_level(i) or "block"
                    body_top, body_bot = max(o[i], c[i]), min(o[i], c[i])
                    obs.append(OrderBlock(
                        index=i, time=df.index[i], kind="bullish",
                        open_=o[i], close_=c[i], high=h[i], low=l[i],
                        mt=(body_top + body_bot) / 2, origin=origin,
                    ))
                    break

        # --- медвежий OB: восходящая свеча + импульсное поглощение медвежьими свечами ---
        if is_up_candle:
            for j in range(i + 1, min(i + 4, len(df))):
                if c[j] < o[i]:
                    origin = near_key_level(i) or "block"
                    body_top, body_bot = max(o[i], c[i]), min(o[i], c[i])
                    obs.append(OrderBlock(
                        index=i, time=df.index[i], kind="bearish",
                        open_=o[i], close_=c[i], high=h[i], low=l[i],
                        mt=(body_top + body_bot) / 2, origin=origin,
                    ))
                    break

    _mark_breakers_and_invalidation(obs, df)
    return obs


def _mark_breakers_and_invalidation(obs: list[OrderBlock], df: pd.DataFrame) -> None:
    """
    OB инвалидируется при закреплении ТЕЛОМ свечи за уровнем MT (50%).
    Если после инвалидации цена пробивает блок целиком и закрывается за ним —
    блок становится Breaker Block (брейкер), который далее работает как
    зеркальная зона поддержки/сопротивления (модуль 10).
    """
    closes = df["close"].values
    for ob in obs:
        for j in range(ob.index + 1, len(df)):
            if ob.kind == "bullish":
                if closes[j] < ob.mt and ob.invalidated_index is None:
                    ob.invalidated_index = j
                if ob.invalidated_index is not None and closes[j] < ob.low:
                    ob.is_breaker = True
                    ob.breaker_index = j
                    break
            else:
                if closes[j] > ob.mt and ob.invalidated_index is None:
                    ob.invalidated_index = j
                if ob.invalidated_index is not None and closes[j] > ob.high:
                    ob.is_breaker = True
                    ob.breaker_index = j
                    break


def find_liquidity_levels(df: pd.DataFrame, swings: list[Swing], eq_tol_pct: float = 0.05) -> list[LiquidityLevel]:
    """
    BSL (buy-side liquidity) — над swing high; SSL (sell-side liquidity) — под swing low.
    Также схлопывает приблизительно равные хаи/лои (Equal Highs/Lows) в один уровень.
    Помечает sweep — момент, когда цена пробивает уровень ФИТИЛЕМ и закрывается обратно.
    """
    levels: list[LiquidityLevel] = []
    highs_sw = sorted([s for s in swings if s.kind == "high"], key=lambda s: s.index)
    lows_sw = sorted([s for s in swings if s.kind == "low"], key=lambda s: s.index)

    def collapse(sw_list, kind):
        used = [False] * len(sw_list)
        out = []
        for idx, s in enumerate(sw_list):
            if used[idx]:
                continue
            cluster = [s]
            used[idx] = True
            for jdx in range(idx + 1, len(sw_list)):
                if used[jdx]:
                    continue
                if abs(sw_list[jdx].price - s.price) / s.price * 100 <= eq_tol_pct:
                    cluster.append(sw_list[jdx])
                    used[jdx] = True
            rep = cluster[-1]  # самый последний по времени уровень кластера
            out.append(LiquidityLevel(rep.index, rep.time, rep.price, kind))
        return out

    levels.extend(collapse(highs_sw, "BSL"))
    levels.extend(collapse(lows_sw, "SSL"))

    h, l, c = df["high"].values, df["low"].values, df["close"].values
    for lvl in levels:
        for j in range(lvl.index + 1, len(df)):
            if lvl.kind == "BSL" and h[j] > lvl.price and c[j] < lvl.price:
                lvl.swept_index = j
                break
            if lvl.kind == "SSL" and l[j] < lvl.price and c[j] > lvl.price:
                lvl.swept_index = j
                break
    return levels


def premium_discount_zone(swing_high: float, swing_low: float, price: float) -> tuple[str, float]:
    """
    Premium/Discount (аналог сетки Фибоначчи 0-100%, модуль 8).
    0% = swing_low, 100% = swing_high. <50% = Discount (выгодно для покупок),
    >=50% = Premium (выгодно для продаж).
    Возвращает (zone, pct_in_range).
    """
    if swing_high == swing_low:
        return "n/a", 50.0
    pct = (price - swing_low) / (swing_high - swing_low) * 100
    zone = "discount" if pct < 50 else "premium"
    return zone, pct


def detect_open_levels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Daily / Weekly / Monthly Open (модуль 26, PO3/AMD/DO-WO-MO).
    Возвращает DataFrame с колонками do, wo, mo для каждого бара (forward-filled).
    """
    out = pd.DataFrame(index=df.index)
    daily_open = df["open"].groupby(df.index.date).transform("first")
    out["do"] = daily_open.values
    iso = df.index.isocalendar()
    week_key = list(zip(iso.year, iso.week))
    weekly_open = df["open"].groupby(week_key).transform("first")
    out["wo"] = weekly_open.values
    month_key = [(t.year, t.month) for t in df.index]
    monthly_open = df["open"].groupby(month_key).transform("first")
    out["mo"] = monthly_open.values
    return out


@dataclass
class SMCContext:
    """Контейнер всех рассчитанных SMC-объектов для одного датафрейма (одного TF)."""
    df: pd.DataFrame
    swings: list
    structure: pd.DataFrame
    fvgs: list
    obs: list
    liquidity: list


def build_smc_context(df: pd.DataFrame, cfg: Config) -> SMCContext:
    swings = find_swings(df, cfg.SWING_LEFT, cfg.SWING_RIGHT)
    structure = label_structure(df, swings)
    fvgs = find_fvg(df)
    update_fvg_fill_status(fvgs, df)
    obs = find_order_blocks(df, swings, fvgs)
    liquidity = find_liquidity_levels(df, swings)
    return SMCContext(df=df, swings=swings, structure=structure, fvgs=fvgs, obs=obs, liquidity=liquidity)


@dataclass
class Setup:
    """Потенциальный сетап (зона входа), сформированный на баре `formed_index`."""
    formed_index: int
    direction: str            # "long" | "short"
    entry_zone_top: float
    entry_zone_bottom: float
    entry_ref_price: float     # референсный уровень входа внутри зоны (напр. Open OB)
    sl_price: float
    tp_price: float            # финальный (структурный) тейк-профит
    strategy: str
    reason: str
    valid_until_index: Optional[int] = None   # если зона инвалидируется раньше касания


# ==============================================================================
# 4. ГЕНЕРАТОРЫ СЕТАПОВ (СТРАТЕГИИ)
# ==============================================================================
# Каждая функция возвращает list[Setup]. Сетап формируется ТОЛЬКО на основе
# баров вплоть до formed_index включительно (без заглядывания вперед).
# Касание зоны и исполнение производится позже, в backtest_engine().

def _nearest_opposite_liquidity(liquidity: list[LiquidityLevel], from_index: int, direction: str, current_price: float):
    """Ближайший непробитый уровень противоположной ликвидности, лежащий ВПЕРЕДИ
    по направлению сделки относительно текущей цены — целевой TP."""
    target_kind = "BSL" if direction == "long" else "SSL"
    candidates = [lv for lv in liquidity if lv.kind == target_kind and lv.index <= from_index
                  and (lv.swept_index is None or lv.swept_index > from_index)]
    if direction == "long":
        candidates = [lv for lv in candidates if lv.price > current_price]
        return min(candidates, key=lambda lv: lv.price) if candidates else None
    else:
        candidates = [lv for lv in candidates if lv.price < current_price]
        return max(candidates, key=lambda lv: lv.price) if candidates else None


def strategy_htf_poi_ltf_ob(ltf: SMCContext, htf: SMCContext, cfg: Config) -> list[Setup]:
    """
    [HTF_POI_LTF_OB] "HTF POI + LTF Entry" (модуль 32 п.1).
    Логика: на HTF определяем POI (валидный Order Block в Discount для лонга /
    Premium для шорта по направлению структуры), затем на LTF ждем вход от
    Order Block внутри этой зоны.
    """
    setups: list[Setup] = []
    ltf_df, htf_df = ltf.df, htf.df
    if len(htf_df) < 20 or len(ltf_df) < 20:
        return setups

    # map: каждому LTF-бару сопоставляем последний завершенный HTF-бар (без lookahead)
    htf_times = htf_df.index
    for ob in ltf.obs:
        if ob.invalidated_index is not None and ob.invalidated_index <= ob.index + 1:
            continue
        t = ltf_df.index[ob.index]
        # последний HTF бар, полностью завершившийся до этого момента
        pos = htf_times.searchsorted(t, side="right") - 1
        if pos < 1:
            continue
        htf_trend = current_trend_at(htf.structure, pos)
        if htf_trend is None:
            continue
        # ближайший валидный HTF swing range для premium/discount
        recent_highs = [s for s in htf.swings if s.kind == "high" and s.index <= pos]
        recent_lows = [s for s in htf.swings if s.kind == "low" and s.index <= pos]
        if not recent_highs or not recent_lows:
            continue
        sw_h, sw_l = recent_highs[-1].price, recent_lows[-1].price
        zone, pct = premium_discount_zone(sw_h, sw_l, ob.mt)

        if ob.kind == "bullish" and htf_trend == "up" and zone == "discount":
            sl = ob.low
            entry = ob.open_
            risk = entry - sl
            if risk <= 0:
                continue
            tp = sw_h
            if (tp - entry) / risk < cfg.MIN_RR_FILTER:
                continue
            setups.append(Setup(ob.index, "long", ob.high, ob.low, entry, sl, tp,
                                 "HTF_POI_LTF_OB", f"HTF up-trend, LTF bullish OB @discount {pct:.0f}%"))
        elif ob.kind == "bearish" and htf_trend == "down" and zone == "premium":
            sl = ob.high
            entry = ob.open_
            risk = sl - entry
            if risk <= 0:
                continue
            tp = sw_l
            if (entry - tp) / risk < cfg.MIN_RR_FILTER:
                continue
            setups.append(Setup(ob.index, "short", ob.high, ob.low, entry, sl, tp,
                                 "HTF_POI_LTF_OB", f"HTF down-trend, LTF bearish OB @premium {pct:.0f}%"))
    return setups


def strategy_order_flow_sweep_shift(ltf: SMCContext, cfg: Config) -> list[Setup]:
    """
    [ORDER_FLOW_SWEEP_SHIFT] "Order Flow: снятие ликвидности структурной точки + Shift"
    (модуль 32 п.2 / модуль 22). Вход после liquidity sweep свинга и подтверждения
    разворота (CHOCH) на этом же таймфрейме.
    """
    setups: list[Setup] = []
    df = ltf.df
    structure_by_index = {row["index"]: row for _, row in ltf.structure.iterrows()} if not ltf.structure.empty else {}
    choch_indices = sorted(structure_by_index.keys()) if structure_by_index else []

    for lvl in ltf.liquidity:
        if lvl.swept_index is None:
            continue
        sweep_i = lvl.swept_index
        # ищем ближайший CHOCH в пределах 10 баров после sweep
        choch_row = None
        for idx in choch_indices:
            if sweep_i <= idx <= sweep_i + 10:
                row = structure_by_index[idx]
                if row["type"] == "CHOCH":
                    choch_row = row
                    break
        if choch_row is None:
            continue

        direction = "long" if lvl.kind == "SSL" else "short"
        if direction == "long" and choch_row["direction"] != "up":
            continue
        if direction == "short" and choch_row["direction"] != "down":
            continue

        entry_i = int(choch_row["index"])
        entry_price = df["close"].values[entry_i]
        if direction == "long":
            sl = min(df["low"].values[sweep_i:entry_i + 1])
            risk = entry_price - sl
            if risk <= 0:
                continue
            target = _nearest_opposite_liquidity(ltf.liquidity, entry_i, "long", entry_price)
            tp = target.price if target else entry_price + risk * 3
            if (tp - entry_price) / risk < cfg.MIN_RR_FILTER:
                continue
        else:
            sl = max(df["high"].values[sweep_i:entry_i + 1])
            risk = sl - entry_price
            if risk <= 0:
                continue
            target = _nearest_opposite_liquidity(ltf.liquidity, entry_i, "short", entry_price)
            tp = target.price if target else entry_price - risk * 3
            if (entry_price - tp) / risk < cfg.MIN_RR_FILTER:
                continue

        setups.append(Setup(entry_i, direction, entry_price, entry_price, entry_price, sl, tp,
                             "ORDER_FLOW_SWEEP_SHIFT",
                             f"Sweep {lvl.kind}@{lvl.price:.4f} + CHOCH confirm"))
    return setups


def strategy_quasimodo_poi(ltf: SMCContext, htf: SMCContext, cfg: Config) -> list[Setup]:
    """
    [QUASIMODO_POI] Quasimodo Level @ POI HTF (модуль 16, п.3).
    3 фазы: формирование свинг-хая/лоу -> снятие ликвидности -> слом структуры (CHOCH),
    с ретестом OB/Breaker/FVG. Используется только внутри HTF POI (discount для лонга,
    premium для шорта).
    """
    setups: list[Setup] = []
    df = ltf.df
    htf_times = htf.df.index
    struct_rows = ltf.structure.to_dict("records") if not ltf.structure.empty else []

    for row in struct_rows:
        if row["type"] != "CHOCH":
            continue
        i = int(row["index"])
        direction = "long" if row["direction"] == "up" else "short"

        t = df.index[i]
        pos = htf_times.searchsorted(t, side="right") - 1
        if pos < 1:
            continue
        recent_highs = [s for s in htf.swings if s.kind == "high" and s.index <= pos]
        recent_lows = [s for s in htf.swings if s.kind == "low" and s.index <= pos]
        if not recent_highs or not recent_lows:
            continue
        sw_h, sw_l = recent_highs[-1].price, recent_lows[-1].price
        zone, pct = premium_discount_zone(sw_h, sw_l, df["close"].values[i])
        if direction == "long" and zone != "discount":
            continue
        if direction == "short" and zone != "premium":
            continue

        # ищем ближайший OB того же направления, сформированный до CHOCH, как зону ретеста
        candidate_obs = [ob for ob in ltf.obs
                          if ob.index <= i and ((direction == "long" and ob.kind == "bullish")
                                                 or (direction == "short" and ob.kind == "bearish"))]
        if not candidate_obs:
            continue
        ob = candidate_obs[-1]
        if direction == "long":
            sl = ob.low
            entry = ob.open_
            risk = entry - sl
            if risk <= 0:
                continue
            tp = sw_h
            if (tp - entry) / risk < cfg.MIN_RR_FILTER:
                continue
        else:
            sl = ob.high
            entry = ob.open_
            risk = sl - entry
            if risk <= 0:
                continue
            tp = sw_l
            if (entry - tp) / risk < cfg.MIN_RR_FILTER:
                continue

        setups.append(Setup(ob.index, direction, ob.high, ob.low, entry, sl, tp,
                             "QUASIMODO_POI", f"QM CHOCH @ {zone} {pct:.0f}%, retest OB"))
    return setups


def strategy_fvg_rebalance(ltf: SMCContext, cfg: Config) -> list[Setup]:
    """
    [FVG_REBALANCE] Вход на ребалансе FVG по уровню 0.5 (модуль 9).
    Направление сделки = направление самого FVG (бычий FVG -> лонг от 0.5,
    медвежий FVG -> шорт от 0.5), стоп — за дальнюю границу FVG.
    """
    setups: list[Setup] = []
    df = ltf.df
    for gap in ltf.fvgs:
        if gap.invalidated_index is not None:
            continue
        direction = "long" if gap.kind == "bullish" else "short"
        entry = gap.mid
        if direction == "long":
            sl = gap.bottom
            risk = entry - sl
            if risk <= 0:
                continue
            tp = entry + risk * 3.0
        else:
            sl = gap.top
            risk = sl - entry
            if risk <= 0:
                continue
            tp = entry - risk * 3.0
        setups.append(Setup(gap.start_index + 1, direction, gap.top, gap.bottom, entry, sl, tp,
                             "FVG_REBALANCE", f"{gap.kind} FVG rebalance @0.5"))
    return setups


def strategy_range_aggressive(ltf: SMCContext, cfg: Config, lookback: int = 40, min_touches: int = 2) -> list[Setup]:
    """
    [RANGE_AGGRESSIVE] Торговля в боковике, агрессивный вариант (модуль 13/16):
    вход на девиации (ложный вынос за границу диапазона с возвратом телом внутрь),
    стоп за экстремум выноса, тейк — противоположная граница диапазона.
    Диапазон определяется как зона между последними swing high/low, не обновлявшимися
    `lookback` баров (отсутствие явного тренда).
    """
    setups: list[Setup] = []
    df = ltf.df
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    swings_sorted = sorted(ltf.swings, key=lambda s: s.index)

    for i in range(lookback, len(df)):
        window_highs = [s for s in swings_sorted if i - lookback <= s.index <= i and s.kind == "high"]
        window_lows = [s for s in swings_sorted if i - lookback <= s.index <= i and s.kind == "low"]
        if len(window_highs) < min_touches or len(window_lows) < min_touches:
            continue
        range_top = max(s.price for s in window_highs)
        range_bot = min(s.price for s in window_lows)
        if range_top <= range_bot:
            continue
        range_width_pct = (range_top - range_bot) / range_bot * 100
        if range_width_pct > 8:   # слишком широкий диапазон — это уже не боковик
            continue

        # девиация вниз с возвратом телом внутрь диапазона -> long
        if l[i] < range_bot and c[i] > range_bot:
            sl = l[i]
            entry = c[i]
            risk = entry - sl
            if risk <= 0:
                continue
            tp = range_top
            if (tp - entry) / risk < cfg.MIN_RR_FILTER:
                continue
            setups.append(Setup(i, "long", entry, entry, entry, sl, tp,
                                 "RANGE_AGGRESSIVE", "Deviation below range, body close back inside"))
        # девиация вверх с возвратом телом внутрь диапазона -> short
        if h[i] > range_top and c[i] < range_top:
            sl = h[i]
            entry = c[i]
            risk = sl - entry
            if risk <= 0:
                continue
            tp = range_bot
            if (entry - tp) / risk < cfg.MIN_RR_FILTER:
                continue
            setups.append(Setup(i, "short", entry, entry, entry, sl, tp,
                                 "RANGE_AGGRESSIVE", "Deviation above range, body close back inside"))
    return setups


STRATEGY_REGISTRY = {
    "HTF_POI_LTF_OB": "HTF POI + LTF Order Block Entry",
    "ORDER_FLOW_SWEEP_SHIFT": "Order Flow: Liquidity Sweep + Shift (CHOCH)",
    "QUASIMODO_POI": "Quasimodo Level @ HTF POI",
    "FVG_REBALANCE": "FVG Rebalance Entry (0.5 level)",
    "RANGE_AGGRESSIVE": "Range Trading — Aggressive (Deviation)",
}


def generate_setups(strategy_name: str, ltf: SMCContext, htf: SMCContext, cfg: Config) -> list[Setup]:
    if strategy_name == "HTF_POI_LTF_OB":
        return strategy_htf_poi_ltf_ob(ltf, htf, cfg)
    if strategy_name == "ORDER_FLOW_SWEEP_SHIFT":
        return strategy_order_flow_sweep_shift(ltf, cfg)
    if strategy_name == "QUASIMODO_POI":
        return strategy_quasimodo_poi(ltf, htf, cfg)
    if strategy_name == "FVG_REBALANCE":
        return strategy_fvg_rebalance(ltf, cfg)
    if strategy_name == "RANGE_AGGRESSIVE":
        return strategy_range_aggressive(ltf, cfg)
    raise ValueError(f"Неизвестная стратегия: {strategy_name}")
