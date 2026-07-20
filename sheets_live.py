"""
Работа с Google Sheets для live-бота (Python/Vercel версия).

Структура таблицы (создать вручную, см. README):

Вкладка "Trades" (заголовки в строке 1, 14 колонок A-N):
  Date | Strategy | Symbol | Direction | Reason | Entry | SL | TP | Qty |
  ExitPrice | ExitTime | PnL | PnL% | Status

Вкладка "State" (заголовки в строке 1, 11 колонок A-K; по одной строке на
КАЖДУЮ из 5 стратегий — так несколько стратегий могут держать позиции
одновременно, независимо друг от друга):
  Strategy | Symbol | Direction | Entry | InitialSL | SL | TP | Qty |
  PartialsTaken | TradeRow | LiqPrice

Вкладка "Summary" (одна строка данных, строка 2, 5 колонок A-E):
  TotalTrades | Wins | Losses | TotalPnL | Balance
"""
import os
import re
from datetime import datetime, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _client():
    email = os.environ["GOOGLE_SERVICE_ACCOUNT_EMAIL"]
    key = os.environ["GOOGLE_PRIVATE_KEY"].replace("\\n", "\n")
    info = {
        "type": "service_account",
        "client_email": email,
        "private_key": key,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _sheet_id():
    return os.environ["GOOGLE_SHEET_ID"]


def get_all_states() -> dict:
    """dict: strategy_name -> state dict (только для стратегий с РЕАЛЬНО открытой
    позицией — строка-якорь по стратегии в Sheets может существовать и при
    закрытой позиции, чтобы хранить cooldown, поэтому проверяем именно Symbol,
    а не сам факт наличия строки)."""
    svc = _client()
    res = svc.spreadsheets().values().get(spreadsheetId=_sheet_id(), range="State!A2:L20", valueRenderOption="UNFORMATTED_VALUE").execute()
    rows = res.get("values", [])
    states = {}
    for row in rows:
        if not row or not row[0]:
            continue
        row = row + [""] * (12 - len(row))
        if not row[1]:  # Symbol пуст -> позиции нет, это просто якорь для cooldown
            continue
        states[row[0]] = {
            "strategy": row[0], "symbol": row[1], "direction": row[2],
            "entry": float(row[3]), "initial_sl": float(row[4]), "sl": float(row[5]),
            "tp": float(row[6]), "qty": float(row[7]),
            "partials_taken": int(row[8] or 0),
            "trade_row": int(row[9]) if row[9] else None,
            "liq_price": float(row[10]) if row[10] not in ("", None) else None,
        }
    return states


def _find_state_row(svc, strategy):
    res = svc.spreadsheets().values().get(spreadsheetId=_sheet_id(), range="State!A2:A20", valueRenderOption="UNFORMATTED_VALUE").execute()
    for i, row in enumerate(res.get("values", [])):
        if row and row[0] == strategy:
            return i + 2
    return None


def set_state(strategy: str, state: dict):
    svc = _client()
    row_idx = _find_state_row(svc, strategy)
    values = [[
        strategy, state["symbol"], state["direction"], state["entry"],
        state["initial_sl"], state["sl"], state["tp"], state["qty"],
        state["partials_taken"], state["trade_row"],
        state["liq_price"] if state.get("liq_price") is not None else "",
    ]]
    if row_idx is None:
        svc.spreadsheets().values().append(
            spreadsheetId=_sheet_id(), range="State!A:K",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": values}).execute()
    else:
        svc.spreadsheets().values().update(
            spreadsheetId=_sheet_id(), range=f"State!A{row_idx}:K{row_idx}",
            valueInputOption="RAW", body={"values": values}).execute()


def clear_state(strategy: str):
    """Очищает поля ОТКРЫТОЙ ПОЗИЦИИ (B:K), но оставляет саму строку (A=strategy)
    на месте — она служит якорем для cooldown-метки в колонке L."""
    svc = _client()
    row_idx = _find_state_row(svc, strategy)
    if row_idx is not None:
        svc.spreadsheets().values().update(
            spreadsheetId=_sheet_id(), range=f"State!B{row_idx}:K{row_idx}",
            valueInputOption="RAW", body={"values": [[""] * 10]}).execute()
    else:
        # строки еще не было вообще — создаем пустую строку-якорь
        svc.spreadsheets().values().append(
            spreadsheetId=_sheet_id(), range="State!A:L",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[strategy] + [""] * 10 + [""]]}).execute()


def get_cooldown_until(strategy: str):
    """Метка времени окончания 'остывания' после последнего закрытия сделки
    по этой стратегии (колонка L) — переживает clear_state, т.к. та не
    трогает колонки A и L."""
    svc = _client()
    row_idx = _find_state_row(svc, strategy)
    if row_idx is None:
        return None
    res = svc.spreadsheets().values().get(
        spreadsheetId=_sheet_id(), range=f"State!L{row_idx}",
        valueRenderOption="UNFORMATTED_VALUE").execute()
    vals = res.get("values", [])
    val = vals[0][0] if vals and vals[0] else None
    if not val:
        return None
    from datetime import datetime as _dt
    return _dt.fromisoformat(str(val).replace("Z", "+00:00"))


def set_cooldown_until(strategy: str, until_dt):
    svc = _client()
    row_idx = _find_state_row(svc, strategy)
    if row_idx is None:
        # строки еще нет — создаем якорь с cooldown сразу
        svc.spreadsheets().values().append(
            spreadsheetId=_sheet_id(), range="State!A:L",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[strategy] + [""] * 10 + [until_dt.isoformat()]]}).execute()
        return
    svc.spreadsheets().values().update(
        spreadsheetId=_sheet_id(), range=f"State!L{row_idx}",
        valueInputOption="RAW", body={"values": [[until_dt.isoformat()]]}).execute()


def get_balance(default_balance: float) -> float:
    svc = _client()
    res = svc.spreadsheets().values().get(spreadsheetId=_sheet_id(), range="Summary!E2", valueRenderOption="UNFORMATTED_VALUE").execute()
    vals = res.get("values", [])
    return float(vals[0][0]) if vals and vals[0] and vals[0][0] != "" else default_balance


def append_trade_open(strategy, symbol, direction, reason, entry, sl, tp, qty) -> int:
    svc = _client()
    values = [[
        datetime.now(timezone.utc).isoformat(),
        strategy, symbol, direction, reason, entry, sl, tp, qty,
        "", "", "", "", "OPEN",
    ]]
    res = svc.spreadsheets().values().append(
        spreadsheetId=_sheet_id(), range="Trades!A:N",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": values}).execute()
    updated_range = res["updates"]["updatedRange"]
    m = re.search(r"(\d+)(?::|$)", updated_range)
    return int(m.group(1)) if m else None


def append_partial_note(row: int, note: str):
    svc = _client()
    cur = svc.spreadsheets().values().get(spreadsheetId=_sheet_id(), range=f"Trades!E{row}", valueRenderOption="UNFORMATTED_VALUE").execute()
    existing = cur.get("values", [[""]])[0][0] if cur.get("values") else ""
    svc.spreadsheets().values().update(
        spreadsheetId=_sheet_id(), range=f"Trades!E{row}",
        valueInputOption="RAW", body={"values": [[f"{existing} | {note}"]]}).execute()


def update_trade_close(row: int, exit_price: float, pnl: float, pnl_pct: float, exit_reason: str):
    svc = _client()
    svc.spreadsheets().values().update(
        spreadsheetId=_sheet_id(), range=f"Trades!J{row}:N{row}",
        valueInputOption="RAW",
        body={"values": [[
            exit_price, datetime.now(timezone.utc).isoformat(),
            round(pnl, 2), f"{round(pnl_pct, 2)}%", f"CLOSED ({exit_reason})",
        ]]}).execute()


def update_summary(pnl: float) -> float:
    svc = _client()
    res = svc.spreadsheets().values().get(spreadsheetId=_sheet_id(), range="Summary!A2:E2", valueRenderOption="UNFORMATTED_VALUE").execute()
    row = res.get("values", [["0", "0", "0", "0", "1000"]])[0]
    row = row + ["0"] * (5 - len(row))
    total = int(row[0] or 0) + 1
    wins = int(row[1] or 0) + (1 if pnl > 0 else 0)
    losses = int(row[2] or 0) + (1 if pnl <= 0 else 0)
    total_pnl = float(row[3] or 0) + pnl
    balance = float(row[4] or 1000) + pnl
    svc.spreadsheets().values().update(
        spreadsheetId=_sheet_id(), range="Summary!A2:E2",
        valueInputOption="RAW",
        body={"values": [[total, wins, losses, round(total_pnl, 2), round(balance, 2)]]}).execute()
    return balance
