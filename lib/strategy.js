// Упрощённый live-порт стратегии QUASIMODO_POI из python-движка
// (smc_backtest_engine.py / engine_core.py) — по твоим бэктестам она чаще
// всего давала лучший PnL. В отличие от FVG (простой 3-свечной паттерн),
// Quasimodo зависит от полного анализа структуры рынка: свинги -> BOS/CHOCH
// -> Order Block ретест -> зона premium/discount на старшем ТФ. Это честный,
// но УПРОЩЁННЫЙ порт (без FVG-зон ребаланса как альтернативного "origin" для
// OB, без breaker-блоков и инвалидации — этого нет и в исходном фильтре
// сигнала для входа, только в отчётности).
//
// Схема выхода зафиксирована по результатам RR-sweep: 30% на +1.5R -> БУ ->
// полный выход оставшихся 70% на +4R (комбинация fixed_4R_partial_30@1.5R).
const EXIT_SCHEME = {
  partialR: 1.5,
  partialPct: 0.30,
  fullR: 4.0,
};

const SWING_LEFT = 3;
const SWING_RIGHT = 3;
const MIN_RR_FILTER = 1.5;
const HTF_BUCKET_MIN = 240; // 4h — старший таймфрейм для зоны premium/discount

/** Фрактальные свинг-хаи/лои (левых/правых баров подтверждения — как в python SWING_LEFT/RIGHT). */
function findSwings(klines, left = SWING_LEFT, right = SWING_RIGHT) {
  const swings = [];
  for (let i = left; i < klines.length - right; i++) {
    const window = klines.slice(i - left, i + right + 1);
    const isHigh = window.every((k) => k.high <= klines[i].high);
    const isLow = window.every((k) => k.low >= klines[i].low);
    if (isHigh) swings.push({ index: i, kind: "high", price: klines[i].high });
    if (isLow) swings.push({ index: i, kind: "low", price: klines[i].low });
  }
  return swings;
}

/** BOS (пробой по тренду) / CHOCH (слом структуры, против тренда) — как в label_structure(). */
function labelStructure(klines, swings) {
  const events = [];
  let trend = null;
  let activeHighs = [];
  let activeLows = [];
  const sorted = [...swings].sort((a, b) => a.index - b.index);
  let ptr = 0;

  for (let i = 0; i < klines.length; i++) {
    while (ptr < sorted.length && sorted[ptr].index <= i) {
      const s = sorted[ptr];
      if (s.kind === "high") activeHighs.push(s); else activeLows.push(s);
      ptr++;
    }
    const c = klines[i].close;
    if (activeHighs.length) {
      const lastH = activeHighs[activeHighs.length - 1];
      if (c > lastH.price) {
        events.push({ index: i, type: trend === "down" ? "CHOCH" : "BOS", direction: "up", level: lastH.price });
        trend = "up";
        activeHighs = activeHighs.filter((s) => s.price > lastH.price);
      }
    }
    if (activeLows.length) {
      const lastL = activeLows[activeLows.length - 1];
      if (c < lastL.price) {
        events.push({ index: i, type: trend === "up" ? "CHOCH" : "BOS", direction: "down", level: lastL.price });
        trend = "down";
        activeLows = activeLows.filter((s) => s.price < lastL.price);
      }
    }
  }
  return events;
}

/** Order Blocks: последняя противоположная свеча перед импульсным поглощением (до 3 баров вперёд). */
function findOrderBlocks(klines) {
  const obs = [];
  for (let i = 0; i < klines.length - 3; i++) {
    const k = klines[i];
    const isDown = k.close < k.open;
    const isUp = k.close > k.open;
    if (isDown) {
      for (let j = i + 1; j < Math.min(i + 4, klines.length); j++) {
        if (klines[j].close > k.open) {
          obs.push({ index: i, kind: "bullish", open: k.open, close: k.close, high: k.high, low: k.low });
          break;
        }
      }
    } else if (isUp) {
      for (let j = i + 1; j < Math.min(i + 4, klines.length); j++) {
        if (klines[j].close < k.open) {
          obs.push({ index: i, kind: "bearish", open: k.open, close: k.close, high: k.high, low: k.low });
          break;
        }
      }
    }
  }
  return obs;
}

/** Зона premium (верхняя половина диапазона свинг-хай/лоу) или discount (нижняя). */
function premiumDiscountZone(swingHigh, swingLow, price) {
  const eq = (swingHigh + swingLow) / 2;
  const pct = ((price - swingLow) / (swingHigh - swingLow)) * 100;
  return { zone: price >= eq ? "premium" : "discount", pct };
}

/** Группирует LTF-свечи в HTF-бары по времени (аналог resample_ohlcv). */
function resampleToHTF(klines, bucketMinutes = HTF_BUCKET_MIN) {
  const bucketMs = bucketMinutes * 60 * 1000;
  const buckets = new Map();
  for (const k of klines) {
    const bucketStart = Math.floor(k.openTime / bucketMs) * bucketMs;
    if (!buckets.has(bucketStart)) {
      buckets.set(bucketStart, { openTime: bucketStart, open: k.open, high: k.high, low: k.low, close: k.close });
    } else {
      const b = buckets.get(bucketStart);
      b.high = Math.max(b.high, k.high);
      b.low = Math.min(b.low, k.low);
      b.close = k.close;
    }
  }
  return [...buckets.values()].sort((a, b) => a.openTime - b.openTime);
}

/**
 * Ищет самый свежий валидный сетап QUASIMODO_POI: CHOCH на LTF, зона HTF
 * (discount для лонга / premium для шорта), ближайший OB того же
 * направления для ретеста. Возвращает сетап, только если текущая цена
 * уже находится внутри диапазона этого OB (готова к входу).
 */
function findEntrySignal(klines, currentPrice) {
  const swings = findSwings(klines);
  const structure = labelStructure(klines, swings);
  const obs = findOrderBlocks(klines);
  const htf = resampleToHTF(klines);
  const htfSwings = findSwings(htf, 2, 2); // на HTF окно свинга поменьше — баров меньше

  const chochEvents = structure.filter((e) => e.type === "CHOCH").sort((a, b) => b.index - a.index);

  for (const ev of chochEvents) {
    if (klines.length - 1 - ev.index > 100) break; // слишком старый CHOCH
    const direction = ev.direction === "up" ? "long" : "short";
    const evTime = klines[ev.index].openTime;

    // ближайшие HTF-свинги, сформированные ДО момента CHOCH
    const htfBarIdx = htf.findIndex((b) => b.openTime > evTime) - 1;
    if (htfBarIdx < 1) continue;
    const recentHighs = htfSwings.filter((s) => s.kind === "high" && s.index <= htfBarIdx);
    const recentLows = htfSwings.filter((s) => s.kind === "low" && s.index <= htfBarIdx);
    if (!recentHighs.length || !recentLows.length) continue;
    const swH = recentHighs[recentHighs.length - 1].price;
    const swL = recentLows[recentLows.length - 1].price;

    const { zone } = premiumDiscountZone(swH, swL, klines[ev.index].close);
    if (direction === "long" && zone !== "discount") continue;
    if (direction === "short" && zone !== "premium") continue;

    // ближайший OB того же направления, сформированный до CHOCH
    const candidateObs = obs.filter((ob) =>
      ob.index <= ev.index && ((direction === "long" && ob.kind === "bullish") || (direction === "short" && ob.kind === "bearish"))
    );
    if (!candidateObs.length) continue;
    const ob = candidateObs[candidateObs.length - 1];

    const entry = ob.open;
    const sl = direction === "long" ? ob.low : ob.high;
    const risk = Math.abs(entry - sl);
    if (risk <= 0) continue;
    const structuralTp = direction === "long" ? swH : swL;
    const rr = Math.abs(structuralTp - entry) / risk;
    if (rr < MIN_RR_FILTER) continue;

    // текущая цена должна быть внутри диапазона OB, чтобы считать вход "созревшим"
    const inZone = currentPrice >= ob.low && currentPrice <= ob.high;
    if (!inZone) continue;

    const partialTp = direction === "long" ? entry + risk * EXIT_SCHEME.partialR : entry - risk * EXIT_SCHEME.partialR;
    const fullTp = direction === "long" ? entry + risk * EXIT_SCHEME.fullR : entry - risk * EXIT_SCHEME.fullR;

    return {
      direction,
      reason: `QUASIMODO_POI: CHOCH @ ${zone} (${ev.index}), retest OB [${ob.low.toFixed(4)}, ${ob.high.toFixed(4)}]`,
      entry,
      sl,
      partialTp,
      fullTp,
      partialPct: EXIT_SCHEME.partialPct,
    };
  }
  return null;
}

/**
 * Проверяет открытую позицию на предмет частичного тейка / переноса в БУ /
 * финального выхода / стопа при текущей цене.
 */
function checkPosition(position, currentPrice) {
  const isLong = position.direction === "long";

  const hitSl = isLong ? currentPrice <= position.sl : currentPrice >= position.sl;
  if (hitSl) {
    return { action: "close", exitPrice: position.sl, exitReason: position.partialTaken ? "BE" : "SL" };
  }

  const hitFullTp = isLong ? currentPrice >= position.fullTp : currentPrice <= position.fullTp;
  if (hitFullTp) {
    return { action: "close", exitPrice: position.fullTp, exitReason: "TP" };
  }

  if (!position.partialTaken) {
    const hitPartial = isLong ? currentPrice >= position.partialTp : currentPrice <= position.partialTp;
    if (hitPartial) {
      return { action: "partial", exitPrice: position.partialTp, newSl: position.entry };
    }
  }

  return { action: "none" };
}

module.exports = {
  findSwings, labelStructure, findOrderBlocks, resampleToHTF, premiumDiscountZone,
  findEntrySignal, checkPosition, EXIT_SCHEME,
};
