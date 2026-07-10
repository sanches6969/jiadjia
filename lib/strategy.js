// Упрощённый live-порт стратегии FVG_REBALANCE из python-движка
// (smc_backtest_engine.py) — по вашим же бэктестам она оказалась самой
// стабильной на разных таймфреймах и периодах.
//
// УПРОЩЕНИЯ относительно полного питон-движка:
//  - нет HTF-контекста (Order Blocks/структура старшего ТФ) — вход строится
//    только по FVG на одном таймфрейме;
//  - финальный TP не структурный, а фиксированный R (см. EXIT_SCHEME ниже) —
//    это как раз комбинация fixed_4R_partial_20@2R, которая чаще всего
//    выигрывала в вашем RR-sweep;
//  - одна открытая позиция за раз.
//
// Схема выхода (по умолчанию): частичный тейк 20% на +2R, перенос SL в БУ,
// финальный выход оставшихся 80% на +4R либо по стопу/БУ.
const EXIT_SCHEME = {
  partialR: 2.0,
  partialPct: 0.20,
  fullR: 4.0,
};

/** Находит все fair value gaps (3-свечной паттерн) в массиве свечей. */
function findFVGs(klines) {
  const fvgs = [];
  for (let i = 2; i < klines.length; i++) {
    const c0 = klines[i - 2];
    const c2 = klines[i];
    if (c2.low > c0.high) {
      // бычий FVG: гэп между high(c0) и low(c2)
      fvgs.push({
        direction: "long",
        index: i,
        top: c2.low,
        bottom: c0.high,
        mid: (c2.low + c0.high) / 2,
      });
    } else if (c2.high < c0.low) {
      // медвежий FVG: гэп между low(c0) и high(c2)
      fvgs.push({
        direction: "short",
        index: i,
        top: c0.low,
        bottom: c2.high,
        mid: (c0.low + c2.high) / 2,
      });
    }
  }
  return fvgs;
}

/**
 * Ищет новый сетап на вход: последний неотработанный FVG, чья 50%-зона
 * уже задета текущей ценой (но гэп еще не пробит целиком — иначе он
 * считается "отработанным" и больше не валиден для входа).
 */
function findEntrySignal(klines, currentPrice) {
  const fvgs = findFVGs(klines);
  // идём с конца — интересен самый свежий валидный гэп
  for (let i = fvgs.length - 1; i >= 0; i--) {
    const g = fvgs[i];
    const barsSinceFormed = klines.length - 1 - g.index;
    if (barsSinceFormed > 100) break; // слишком старый гэп — не рассматриваем

    // проверяем, не пробита ли уже вся зона гэпа последующими свечами
    // (после его формирования) — если да, гэп "закрыт" и невалиден
    let fullyFilled = false;
    for (let j = g.index + 1; j < klines.length; j++) {
      const k = klines[j];
      if (g.direction === "long" && k.low <= g.bottom) fullyFilled = true;
      if (g.direction === "short" && k.high >= g.top) fullyFilled = true;
    }
    if (fullyFilled) continue;

    // текущая цена должна находиться внутри зоны гэпа (уже коснулась 50%)
    const inZone =
      g.direction === "long"
        ? currentPrice <= g.mid && currentPrice >= g.bottom
        : currentPrice >= g.mid && currentPrice <= g.top;

    if (inZone) {
      const entry = currentPrice;
      const sl = g.direction === "long" ? g.bottom * 0.999 : g.top * 1.001;
      const risk = Math.abs(entry - sl);
      const partialTp =
        g.direction === "long" ? entry + risk * EXIT_SCHEME.partialR : entry - risk * EXIT_SCHEME.partialR;
      const fullTp =
        g.direction === "long" ? entry + risk * EXIT_SCHEME.fullR : entry - risk * EXIT_SCHEME.fullR;
      return {
        direction: g.direction,
        reason: `FVG_REBALANCE: вход на 50% гэпа [${g.bottom.toFixed(4)}, ${g.top.toFixed(4)}], сформирован ${barsSinceFormed} баров назад`,
        entry,
        sl,
        partialTp,
        fullTp,
        partialPct: EXIT_SCHEME.partialPct,
      };
    }
  }
  return null;
}

/**
 * Проверяет открытую позицию на предмет частичного тейка / переноса в БУ /
 * финального выхода / стопа при текущей цене.
 * position: { direction, entry, sl, partialTp, fullTp, partialPct, partialTaken }
 * Возвращает { action: "none"|"partial"|"close", ...детали } — action
 * "close" означает полное закрытие позиции (по стопу/БУ/финальному тейку).
 */
function checkPosition(position, currentPrice) {
  const isLong = position.direction === "long";

  // 1) стоп (или БУ, если partial уже взят — sl к этому моменту уже
  //    должен быть переставлен в state на entry)
  const hitSl = isLong ? currentPrice <= position.sl : currentPrice >= position.sl;
  if (hitSl) {
    return { action: "close", exitPrice: position.sl, exitReason: position.partialTaken ? "BE" : "SL" };
  }

  // 2) финальный тейк
  const hitFullTp = isLong ? currentPrice >= position.fullTp : currentPrice <= position.fullTp;
  if (hitFullTp) {
    return { action: "close", exitPrice: position.fullTp, exitReason: "TP" };
  }

  // 3) частичный тейк (только если еще не брали)
  if (!position.partialTaken) {
    const hitPartial = isLong ? currentPrice >= position.partialTp : currentPrice <= position.partialTp;
    if (hitPartial) {
      return { action: "partial", exitPrice: position.partialTp, newSl: position.entry };
    }
  }

  return { action: "none" };
}

module.exports = { findFVGs, findEntrySignal, checkPosition, EXIT_SCHEME };
