# SMC Paper Trading Bot — ПОЛНАЯ версия (все 5 стратегий, Python)

В отличие от предыдущей JS-версии, здесь используется **настоящее ядро**
твоего python-движка (`engine_core.py` — вырезано из `smc_backtest_engine.py`
без интерактивных промптов и backtest-цикла): реальная структура рынка
(Swing H/L, BOS/CHOCH), Order Blocks, FVG, ликвидность, HTF-контекст — и
**все 5 стратегий**, каждая со своим независимым состоянием (может быть
несколько одновременно открытых позиций по разным стратегиям).

Финальный тейк — **структурный** (не фиксированный R), как в оригинальном
бэктесте. Частичные тейки — по `Config.PARTIAL_TP_LEVELS` (50% на 1R → БУ,
30% на 3R, 25% на 4R), тоже как в оригинале. Ликвидация по марже
моделируется так же, как в питон-движке.

**Важная оговорка та же, что и раньше:** Vercel — serverless, честного
непрерывного realtime тут нет, есть проверка по расписанию (1-5 минут).

---

## Отличия от предыдущей (JS) версии в деплое

1. Файлы теперь **`.py`**, а не `.js` — Vercel сам определяет Python-рантайм.
2. Зависимости — в `requirements.txt`, а не `package.json`.
3. Google Sheets теперь пишутся из Python (`google-api-python-client`), не
   `googleapis` (Node).
4. Структура вкладки **State** изменилась — теперь **одна строка на КАЖДУЮ
   из 5 стратегий** (а не одна строка на весь бот), т.к. стратегии работают
   независимо и могут держать позиции одновременно.

## Шаг 1 — Google Sheet

Создай таблицу с 3 вкладками (названия ТОЧНО такие, с заглавной буквы):

**Trades** (заголовки, строка 1, 14 колонок):
```
Date | Strategy | Symbol | Direction | Reason | Entry | SL | TP | Qty | ExitPrice | ExitTime | PnL | PnL% | Status
```

**State** (заголовки, строка 1, 11 колонок — код сам заполняет данные со
строки 2, по одной строке на стратегию):
```
Strategy | Symbol | Direction | Entry | InitialSL | SL | TP | Qty | PartialsTaken | TradeRow | LiqPrice
```

**Summary** (заголовки строка 1, данные строка 2):
```
TotalTrades | Wins | Losses | TotalPnL | Balance
     0      |  0   |   0    |    0     |  1000
```

## Шаг 2 — сервисный аккаунт Google

Точно так же, как в прошлой версии:
1. [Google Cloud Console](https://console.cloud.google.com/) → новый проект → **APIs & Services → Library** → включи **Google Sheets API**.
2. **Credentials → Create Credentials → Service Account** → создай ключ (JSON).
3. Из JSON возьми `client_email` → `GOOGLE_SERVICE_ACCOUNT_EMAIL`, `private_key` → `GOOGLE_PRIVATE_KEY`.
4. **Расшарь таблицу** на этот email с правом **Editor** (Share → вставить email → Editor).

## Шаг 3 — деплой на Vercel

1. Залей папку в GitHub-репозиторий (или используй `vercel` CLI напрямую).
2. Vercel → **Add New → Project** → импортируй репозиторий → Vercel сам
   определит Python по `requirements.txt` и `api/index.py`.
3. **Environment Variables** — добавь все переменные из `.env.example`.
4. Deploy.

⚠️ **Про `maxDuration: 60` в `vercel.json`**: на бесплатном Hobby-плане
Vercel может понизить это значение автоматически (там свои лимиты по
времени выполнения). Обычно наш скрипт укладывается в несколько секунд, так
что это не критично — но если увидишь таймаут в логах, это будет видно
явно (`FUNCTION_INVOCATION_TIMEOUT`).

## Шаг 4 — расписание

Как и раньше — бесплатный [cron-job.org](https://cron-job.org), URL:
```
https://твой-проект.vercel.app/api/scan?secret=ТВОЙ_CRON_SECRET
```
раз в 1-5 минут. (Или Vercel Cron на платном Pro-плане.)

## Шаг 5 — проверка

Открой:
```
https://твой-проект.vercel.app/api/scan?secret=ТВОЙ_CRON_SECRET
```
Ответ вида:
```json
{
  "price": 118234.5,
  "results": {
    "HTF_POI_LTF_OB": {"status": "no_signal"},
    "ORDER_FLOW_SWEEP_SHIFT": {"status": "no_signal"},
    "QUASIMODO_POI": {"status": "opened", "direction": "long", "reason": "...", "qty": 0.0012},
    "FVG_REBALANCE": {"status": "holding"},
    "RANGE_AGGRESSIVE": {"status": "no_signal"}
  }
}
```
— по каждой из 5 стратегий отдельный статус. Проверь Google Sheet — вкладки
**State**/**Trades** должны заполняться по стратегиям, которые что-то нашли
или держат позицию.

---

## Из чего состоит (файлы)

- `engine_core.py` — реальное ядро: Config, структура/OB/FVG/ликвидность,
  HTF-контекст, все 5 функций-стратегий (`strategy_htf_poi_ltf_ob`,
  `strategy_order_flow_sweep_shift`, `strategy_quasimodo_poi`,
  `strategy_fvg_rebalance`, `strategy_range_aggressive`) — взято из твоего
  `smc_backtest_engine.py` практически без изменений.
- `binance_live.py` — live-цены/свечи с Binance.
- `sheets_live.py` — работа с Google Sheets (состояние по каждой стратегии,
  журнал сделок, сводка).
- `api/index.py` — Flask-обработчик: на каждый тик — для каждой из 5
  стратегий либо проверяет открытую позицию (ликвидация → стоп/БУ →
  структурный TP → частичные тейки), либо (если позиции нет) считает
  сетапы по реальному HTF-контексту и ищет вход.

## Ограничения, которые всё равно остаются

- Реальные исторические 500 баров тянутся на КАЖДЫЙ тик, если хотя бы у
  одной стратегии нет открытой позиции (структура/OB/FVG пересчитываются
  заново) — это тяжелее по времени, чем упрощенная JS-версия, но зато
  честнее по сигналам. Если станет медленно/дорого — можно закешировать
  контекст между тиками (например в отдельной вкладке/Vercel KV), но пока
  не реализовано.
- Общий Summary/баланс — один на все 5 стратегий (упрощение: каждая
  стратегия считает объем позиции от текущего ОБЩЕГО баланса на момент
  входа, а не от отдельного под-баланса на стратегию).
- Не проверяется минимальный лот биржи — расчет объема чисто виртуальный.
