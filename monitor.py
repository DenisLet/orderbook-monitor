# -*- coding: utf-8 -*-
"""
Orderbook Monitor - single-file FastAPI app
Run: uvicorn monitor:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Optional

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse

app = FastAPI(title="Orderbook Monitor")

# ──────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────

PAIRS = [
    "BTC-USDT", "ETH-USDT", "BNB-USDT", "TRX-USDT",
    "DOGE-USDT", "XRP-USDT", "SOL-USDT", "LINK-USDT",
]

PROVIDERS = {
    "bybit":  "wss://api.citronus.com/web/orderbook/orderbook.1.{pair}/?provider=futures",
    "bombit": "wss://api.citronus.world/web/orderbook/orderbook.1.{pair}/?provider=futures",
}

active_provider: str = "bybit"
monitor_tasks: dict[str, asyncio.Task] = {}

MAX_SILENCE        = 25   # сек — тишина → CRITICAL
WARNING_SILENCE    = 12   # сек — тишина → WARNING
FROZEN_WARN_SEC    = 5    # сек без изменений → FROZEN
FROZEN_CRIT_SEC    = 30   # сек без изменений → FROZEN_CRIT

events: list = []  # неограниченный лог

# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def ts_to_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def fmt_sec(sec: float) -> str:
    sec = int(sec)
    if sec < 60: return f"{sec}s"
    if sec < 3600: return f"{sec//60}m {sec%60}s"
    return f"{sec//3600}h {(sec%3600)//60}m"

# ──────────────────────────────────────────────
#  Frozen episode
# ──────────────────────────────────────────────

class FrozenEpisode:
    def __init__(self, start: float):
        self.start = start
        self.end: Optional[float] = None
        self.start_utc = ts_to_utc(start)

    @property
    def duration(self) -> float:
        return (self.end or time.time()) - self.start

    def close(self):
        self.end = time.time()

    def to_dict(self) -> dict:
        return {
            "start": self.start_utc,
            "end": ts_to_utc(self.end) if self.end else None,
            "duration_sec": round(self.duration, 1),
        }

# ──────────────────────────────────────────────
#  State
# ──────────────────────────────────────────────

class PairState:
    def __init__(self, pair: str):
        self.pair = pair
        self.last_msg_time: float = 0.0
        self.snapshot_count: int = 0
        self.last_lp: Optional[float] = None
        self.bid: Optional[str] = None
        self.bid_qty: Optional[str] = None
        self.ask: Optional[str] = None
        self.ask_qty: Optional[str] = None
        self.connected: bool = False
        self.disconnect_count: int = 0
        self.total_downtime_sec: float = 0.0
        self.disconnect_start: float = 0.0
        self.last_disconnect_utc: Optional[str] = None
        self.last_reconnect_utc: Optional[str] = None
        self.reconnect_delay: float = 1.5
        self.status: str = "INIT"
        self.session_start: float = time.time()

        # Frozen detection — time-based
        # "last change time" — когда последний раз изменился bid, ask, bid_qty или ask_qty
        self.last_change_time: float = 0.0
        self._prev_key: Optional[str] = None  # fingerprint прошлого снапшота

        # История заморозок
        self.frozen_episodes: list[FrozenEpisode] = []
        self._current_freeze: Optional[FrozenEpisode] = None
        self.total_frozen_sec: float = 0.0  # завершённых эпизодов

    @property
    def silence(self) -> float:
        return 0.0 if self.last_msg_time == 0 else time.time() - self.last_msg_time

    @property
    def frozen_since(self) -> Optional[float]:
        """Возвращает время начала текущей заморозки или None."""
        return self._current_freeze.start if self._current_freeze else None

    @property
    def frozen_for(self) -> float:
        """Сколько секунд сейчас заморожено."""
        return self._current_freeze.duration if self._current_freeze else 0.0

    @property
    def total_frozen_including_current(self) -> float:
        cur = self._current_freeze.duration if self._current_freeze else 0.0
        return round(self.total_frozen_sec + cur, 1)

    @property
    def frozen_pct(self) -> float:
        session = time.time() - self.session_start
        if session <= 0:
            return 0.0
        return round(100 * self.total_frozen_including_current / session, 1)

    def update_data(self, bid: Optional[str], bid_qty: Optional[str],
                    ask: Optional[str], ask_qty: Optional[str]) -> str:
        """
        Обновляет данные пары. Возвращает freeze-событие:
        'none' | 'freeze_start' | 'freeze_crit' | 'freeze_end'
        """
        key = f"{bid}|{bid_qty}|{ask}|{ask_qty}"
        now = time.time()
        event = 'none'

        if key != self._prev_key:
            # Данные изменились
            self._prev_key = key
            self.last_change_time = now

            if self._current_freeze:
                # Заморозка закончилась
                self._current_freeze.close()
                self.total_frozen_sec += self._current_freeze.duration
                event = 'freeze_end'
                self._current_freeze = None
        else:
            # Данные не изменились
            if self.last_change_time == 0:
                self.last_change_time = now

            frozen_for = now - self.last_change_time

            if frozen_for >= FROZEN_WARN_SEC and self._current_freeze is None:
                # Только что стало FROZEN
                self._current_freeze = FrozenEpisode(self.last_change_time)
                self.frozen_episodes.append(self._current_freeze)
                event = 'freeze_start'
            elif (frozen_for >= FROZEN_CRIT_SEC
                  and self._current_freeze is not None
                  and not getattr(self._current_freeze, '_crit_logged', False)):
                self._current_freeze._crit_logged = True
                event = 'freeze_crit'

        return event

    def compute_status(self) -> str:
        if not self.connected:
            return "DISCONNECTED"
        s = self.silence
        if s > MAX_SILENCE:
            return "CRITICAL"
        # Frozen проверяем по времени без изменений
        if self.last_change_time > 0:
            frozen_for = time.time() - self.last_change_time
            if self._current_freeze is not None:  # уже в режиме freeze
                if frozen_for >= FROZEN_CRIT_SEC:
                    return "FROZEN_CRIT"
                return "FROZEN"
        if s > WARNING_SILENCE:
            return "WARNING"
        return "OK"

    def frozen_summary(self) -> dict:
        session_sec = time.time() - self.session_start
        total = self.total_frozen_including_current
        return {
            "is_frozen": self._current_freeze is not None,
            "frozen_for": round(self.frozen_for, 1),
            "frozen_since_utc": ts_to_utc(self._current_freeze.start) if self._current_freeze else None,
            "total_episodes": len(self.frozen_episodes),
            "total_frozen_sec": total,
            "session_sec": round(session_sec, 1),
            "frozen_pct": self.frozen_pct,
            "history": [ep.to_dict() for ep in self.frozen_episodes[-10:]],
        }


states: dict[str, PairState] = {p: PairState(p) for p in PAIRS}
dashboard_clients: list[WebSocket] = []


def add_event(kind: str, pair: str, message: str, extra: dict = None):
    ev = {
        "id": len(events),
        "ts": now_utc_str(),
        "kind": kind,
        "pair": pair,
        "message": message,
        **(extra or {}),
    }
    events.append(ev)
    asyncio.create_task(broadcast({"type": "event", "data": ev}))


async def broadcast(msg: dict):
    dead = []
    for ws in dashboard_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        dashboard_clients.remove(ws)


async def broadcast_state(pair: str):
    st = states[pair]
    st.status = st.compute_status()
    fs = st.frozen_summary()
    await broadcast({
        "type": "state",
        "data": {
            "pair": pair,
            "status": st.status,
            "lp": st.last_lp,
            "bid": st.bid,
            "bid_qty": st.bid_qty,
            "ask": st.ask,
            "ask_qty": st.ask_qty,
            "connected": st.connected,
            "disconnect_count": st.disconnect_count,
            "total_downtime_sec": round(st.total_downtime_sec, 1),
            "last_disconnect_utc": st.last_disconnect_utc,
            "last_reconnect_utc": st.last_reconnect_utc,
            "silence": round(st.silence, 1),
            "snapshot_count": st.snapshot_count,
            "session_uptime": round(time.time() - st.session_start),
            "provider": active_provider,
            # Frozen
            "is_frozen": fs["is_frozen"],
            "frozen_for": fs["frozen_for"],
            "frozen_since_utc": fs["frozen_since_utc"],
            "total_freeze_episodes": fs["total_episodes"],
            "total_frozen_sec": fs["total_frozen_sec"],
            "frozen_pct": fs["frozen_pct"],
            "freeze_history": fs["history"],
        }
    })

# ──────────────────────────────────────────────
#  Worker
# ──────────────────────────────────────────────

async def _heartbeat(ws):
    while True:
        try:
            await ws.send(json.dumps({"op": "ping"}))
        except Exception:
            return
        await asyncio.sleep(25)


async def monitor_pair(pair: str):
    st = states[pair]

    while True:
        url = PROVIDERS[active_provider].format(pair=pair)
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                if st.disconnect_start > 0:
                    recovery = time.time() - st.disconnect_start
                    st.total_downtime_sec += recovery
                    st.last_reconnect_utc = now_utc_str()
                    st.disconnect_start = 0.0
                    add_event("reconnect", pair,
                              f"[RECONNECT] Соединение восстановлено за {fmt_sec(recovery)}. "
                              f"Было оффлайн с {st.last_disconnect_utc}. "
                              f"Суммарный даунтайм: {fmt_sec(round(st.total_downtime_sec))}.",
                              {"recovery_sec": round(recovery, 1)})

                st.connected = True
                st.reconnect_delay = 1.5
                await broadcast_state(pair)
                asyncio.create_task(_heartbeat(ws))

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=MAX_SILENCE + 3)
                        msg = json.loads(raw)
                        if msg.get("op") == "pong":
                            continue
                        if msg.get("type") != "snapshot":
                            continue

                        d = msg.get("data", {})
                        lp_str = d.get("lp")
                        asks = d.get("a", [])
                        bids = d.get("b", [])

                        st.last_msg_time = time.time()
                        st.snapshot_count += 1

                        new_bid = new_bid_qty = new_ask = new_ask_qty = None

                        if lp_str is not None:
                            try:
                                lp = float(lp_str)
                                st.last_lp = lp
                                for p_str, q in sorted(bids, key=lambda x: float(x[0]), reverse=True):
                                    if float(p_str) <= lp:
                                        new_bid, new_bid_qty = p_str, q[:10]
                                        st.bid, st.bid_qty = new_bid, new_bid_qty
                                        break
                                for p_str, q in sorted(asks, key=lambda x: float(x[0])):
                                    if float(p_str) >= lp:
                                        new_ask, new_ask_qty = p_str, q[:10]
                                        st.ask, st.ask_qty = new_ask, new_ask_qty
                                        break
                            except (ValueError, IndexError):
                                pass

                        # Frozen detection
                        freeze_ev = st.update_data(new_bid, new_bid_qty, new_ask, new_ask_qty)

                        prev_status = st.status
                        new_status = st.compute_status()
                        st.status = new_status

                        # Логируем ВСЕ переходы статуса
                        if new_status != prev_status:
                            if new_status == "WARNING":
                                add_event("warning", pair,
                                          f"[WARNING] Нет новых данных {st.silence:.0f}с (порог: {WARNING_SILENCE}с). "
                                          f"Последние известные: bid={st.bid}, ask={st.ask}.")
                            elif new_status == "CRITICAL":
                                add_event("critical", pair,
                                          f"[CRITICAL] Нет данных уже {st.silence:.0f}с (порог: {MAX_SILENCE}с) — "
                                          f"фид скорее всего сломан. bid={st.bid}, ask={st.ask}.")
                            elif new_status == "OK" and prev_status in ("WARNING", "CRITICAL"):
                                add_event("info", pair,
                                          f"[RECOVERED] Данные снова поступают (тишина была {st.silence:.0f}с). "
                                          f"bid={st.bid}, ask={st.ask}.")

                        # Логируем заморозку
                        fs = st.frozen_summary()
                        if freeze_ev == "freeze_start":
                            session_elapsed = fmt_sec(round(fs['session_sec']))
                            add_event("frozen", pair,
                                      f"[FROZEN] Данные не менялись {FROZEN_WARN_SEC}с — фид заморожен. "
                                      f"bid={st.bid} (qty={st.bid_qty}), ask={st.ask} (qty={st.ask_qty}). "
                                      f"Это эпизод #{fs['total_episodes']} за сессию ({session_elapsed} работы).",
                                      {"frozen_since": fs["frozen_since_utc"]})

                        elif freeze_ev == "freeze_crit":
                            add_event("frozen", pair,
                                      f"[FROZEN CRIT] Данные заморожены уже {fmt_sec(st.frozen_for)} "
                                      f"(с {fs['frozen_since_utc']}). "
                                      f"bid={st.bid} / ask={st.ask} — не менялись {FROZEN_CRIT_SEC}с+. "
                                      f"Всего замораживаний за сессию: {fs['total_episodes']}.")

                        elif freeze_ev == "freeze_end":
                            last_ep = st.frozen_episodes[-1].to_dict() if st.frozen_episodes else {}
                            dur = last_ep.get('duration_sec', 0)
                            total_fr = fs['total_frozen_sec']
                            pct = fs['frozen_pct']
                            sess = fmt_sec(round(fs['session_sec']))
                            add_event("info", pair,
                                      f"[UNFROZEN] Заморозка закончилась. Длилась: {fmt_sec(dur)}. "
                                      f"bid/ask снова изменились. "
                                      f"— Итог по сессии ({sess}): "
                                      f"{fs['total_episodes']} эпизод(ов) заморозки, "
                                      f"суммарно заморожено {fmt_sec(total_fr)} ({pct}% времени).",
                                      {"episode": last_ep, "stats": fs})

                        await broadcast_state(pair)

                    except asyncio.TimeoutError:
                        st.status = "CRITICAL"
                        add_event("critical", pair,
                                  f"[TIMEOUT] Нет данных {MAX_SILENCE+3}с — принудительное переподключение. "
                                  f"Последние данные: bid={st.bid}, ask={st.ask}.")
                        break

        except websockets.exceptions.ConnectionClosedOK:
            st.connected = False
            st.disconnect_count += 1
            st.last_disconnect_utc = now_utc_str()
            st.disconnect_start = time.time()
            add_event("disconnect", pair,
                      f"[DISCONNECT #{st.disconnect_count}] Сервер закрыл соединение корректно (1000 OK). "
                      f"Последние данные: bid={st.bid}, ask={st.ask}. Переподключение...")
            await broadcast_state(pair)

        except asyncio.CancelledError:
            st.connected = False
            await broadcast_state(pair)
            return

        except Exception as e:
            st.connected = False
            st.disconnect_count += 1
            st.last_disconnect_utc = now_utc_str()
            st.disconnect_start = time.time()
            add_event("disconnect", pair,
                      f"[DISCONNECT #{st.disconnect_count}] Ошибка соединения: {type(e).__name__}: {e}. "
                      f"Переподключение через {st.reconnect_delay:.1f}с...")
            await broadcast_state(pair)

        await asyncio.sleep(st.reconnect_delay)
        st.reconnect_delay = min(st.reconnect_delay * 1.5, 30)

# ──────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    for pair in PAIRS:
        monitor_tasks[pair] = asyncio.create_task(monitor_pair(pair))


@app.post("/api/restart")
async def restart_monitoring(body: dict):
    global active_provider
    new_prov = body.get("provider", active_provider)
    if new_prov not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider: {new_prov}")

    active_provider = new_prov
    for pair in PAIRS:
        t = monitor_tasks.get(pair)
        if t and not t.done():
            t.cancel()
        states[pair] = PairState(pair)

    events.clear()
    await broadcast({"type": "restart", "provider": active_provider})
    await asyncio.sleep(0.2)

    for pair in PAIRS:
        monitor_tasks[pair] = asyncio.create_task(monitor_pair(pair))

    return {"ok": True, "provider": active_provider}


@app.get("/api/state")
async def get_state():
    result = {}
    for pair, st in states.items():
        st.status = st.compute_status()
        fs = st.frozen_summary()
        result[pair] = {
            "pair": pair, "status": st.status, "lp": st.last_lp,
            "bid": st.bid, "bid_qty": st.bid_qty, "ask": st.ask, "ask_qty": st.ask_qty,
            "connected": st.connected, "disconnect_count": st.disconnect_count,
            "total_downtime_sec": round(st.total_downtime_sec, 1),
            "last_disconnect_utc": st.last_disconnect_utc,
            "last_reconnect_utc": st.last_reconnect_utc,
            "silence": round(st.silence, 1), "snapshot_count": st.snapshot_count,
            "session_uptime": round(time.time() - st.session_start),
            "provider": active_provider,
            "is_frozen": fs["is_frozen"],
            "frozen_for": fs["frozen_for"],
            "frozen_since_utc": fs["frozen_since_utc"],
            "total_freeze_episodes": fs["total_episodes"],
            "total_frozen_sec": fs["total_frozen_sec"],
            "frozen_pct": fs["frozen_pct"],
            "freeze_history": fs["history"],
        }
    return result


@app.get("/api/events")
async def get_events(page: int = 1, per_page: int = 1000):
    total = len(events)
    # newest first
    rev = list(reversed(events))
    start = (page - 1) * per_page
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "events": rev[start:start + per_page],
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    dashboard_clients.append(websocket)
    state_snapshot = await get_state()
    total = len(events)
    init_events = list(reversed(events[max(0, total - 1000):]))
    await websocket.send_json({
        "type": "init",
        "states": state_snapshot,
        "events": init_events,
        "total_events": total,
    })
    try:
        while True:
            await asyncio.sleep(1)
            for pair, st in states.items():
                old = st.status
                new = st.compute_status()
                if new != old:
                    st.status = new
                    await broadcast_state(pair)
    except WebSocketDisconnect:
        if websocket in dashboard_clients:
            dashboard_clients.remove(websocket)


# ──────────────────────────────────────────────
#  Dashboard HTML
# ──────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Orderbook Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700;900&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#080c10;--bg2:#0d1117;--bg3:#111820;
    --border:#1e2d3d;--border2:#243447;
    --ok:#00e5a0;--ok-dim:#00e5a015;
    --warn:#f5a623;--warn-dim:#f5a62315;
    --crit:#ff3b3b;--crit-dim:#ff3b3b18;
    --frozen:#b44fff;--frozen-dim:#b44fff18;
    --disc:#4a5568;--disc-dim:#4a556815;
    --init:#6b7c93;--text:#c9d8e8;--text-dim:#5a7a96;
    --accent:#00b4ff;
    --mono:'Share Tech Mono',monospace;--sans:'Barlow',sans-serif;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;overflow-x:hidden}
  body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:40px 40px;opacity:.15;pointer-events:none;z-index:0}
  .app{position:relative;z-index:1;max-width:1440px;margin:0 auto;padding:20px 24px}

  header{display:flex;align-items:center;justify-content:space-between;padding:0 0 20px;border-bottom:1px solid var(--border2);margin-bottom:20px;flex-wrap:wrap;gap:12px}
  .header-left{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .logo{font-family:var(--sans);font-weight:900;font-size:22px;letter-spacing:-.5px;color:#fff}
  .logo span{color:var(--accent)}
  .tag{font-family:var(--mono);font-size:10px;color:var(--text-dim);background:var(--bg3);border:1px solid var(--border2);padding:3px 8px;letter-spacing:2px;text-transform:uppercase}
  .ws-status{font-family:var(--mono);font-size:11px;display:flex;align-items:center;gap:7px}
  .ws-dot{width:7px;height:7px;border-radius:50%;background:var(--disc);transition:background .3s}
  .ws-dot.connected{background:var(--ok);box-shadow:0 0 8px var(--ok);animation:pulse-dot 2s infinite}
  .ws-dot.error{background:var(--crit)}
  @keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.4}}

  .controls{display:flex;align-items:center;gap:10px;margin-bottom:20px;flex-wrap:wrap}
  .provider-label{font-family:var(--mono);font-size:10px;color:var(--text-dim);letter-spacing:2px;text-transform:uppercase}
  .seg{display:flex;border:1px solid var(--border2);overflow:hidden}
  .seg-btn{font-family:var(--mono);font-size:11px;letter-spacing:1px;padding:7px 18px;cursor:pointer;border:none;outline:none;background:var(--bg3);color:var(--text-dim);transition:background .2s,color .2s;text-transform:uppercase}
  .seg-btn.active{background:var(--accent);color:#000;font-weight:700}
  .seg-btn:hover:not(.active){background:var(--border2);color:var(--text)}
  .restart-btn{font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;padding:7px 18px;cursor:pointer;border:1px solid var(--border2);background:var(--bg3);color:var(--text-dim);transition:all .2s;margin-left:auto}
  .restart-btn:hover{border-color:var(--warn);color:var(--warn)}
  .restart-btn.loading{opacity:.5;pointer-events:none}

  .summary-bar{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
  .sum-card{background:var(--bg3);border:1px solid var(--border);padding:10px 18px;display:flex;flex-direction:column;gap:3px;min-width:120px;position:relative;cursor:default}
  .sum-label{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim);font-family:var(--mono);display:flex;align-items:center;gap:5px}
  .sum-value{font-size:24px;font-weight:700;color:#fff;font-family:var(--mono)}
  .sum-value.ok{color:var(--ok)}.sum-value.warn{color:var(--warn)}.sum-value.crit{color:var(--crit)}.sum-value.frozen{color:var(--frozen)}

  [data-tip]{position:relative}
  [data-tip]::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);background:#1a2535;border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:10px;line-height:1.6;padding:8px 12px;pointer-events:none;opacity:0;transition:opacity .15s;z-index:100;min-width:200px;max-width:340px;white-space:pre-wrap}
  [data-tip]:hover::after{opacity:1}
  .help-icon{display:inline-flex;align-items:center;justify-content:center;width:12px;height:12px;border-radius:50%;border:1px solid var(--text-dim);font-size:8px;color:var(--text-dim);flex-shrink:0;cursor:help;line-height:1}

  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-bottom:24px}
  .pair-card{background:var(--bg2);border:1px solid var(--border);padding:16px;position:relative;overflow:hidden;transition:border-color .3s,background .3s}
  .pair-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--disc);transition:background .3s}
  .pair-card.ok{border-color:#1a3528}.pair-card.ok::before{background:var(--ok)}
  .pair-card.warning{border-color:#3d2f10;background:linear-gradient(135deg,var(--bg2),#1a1508)}.pair-card.warning::before{background:var(--warn)}
  .pair-card.critical{border-color:#3d1010;background:linear-gradient(135deg,var(--bg2),#1a0808)}.pair-card.critical::before{background:var(--crit);animation:crit-blink 1s infinite}
  .pair-card.frozen,.pair-card.frozen_crit{border-color:#2d1a40;background:linear-gradient(135deg,var(--bg2),#160d22)}
  .pair-card.frozen::before{background:var(--frozen);animation:frozen-pulse 2s infinite}
  .pair-card.frozen_crit::before{background:var(--frozen);animation:crit-blink .6s infinite}
  .pair-card.disconnected{border-color:#1e2d3d;opacity:.65}.pair-card.disconnected::before{background:var(--disc)}
  @keyframes crit-blink{0%,100%{opacity:1}50%{opacity:.3}}
  @keyframes frozen-pulse{0%,100%{opacity:1}50%{opacity:.4}}

  .card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
  .pair-name{font-family:var(--mono);font-size:15px;color:#fff;letter-spacing:1px}
  .status-badge{font-family:var(--mono);font-size:9px;letter-spacing:2px;padding:3px 8px;border:1px solid;text-transform:uppercase}
  .status-badge.ok{color:var(--ok);border-color:var(--ok);background:var(--ok-dim)}
  .status-badge.warning{color:var(--warn);border-color:var(--warn);background:var(--warn-dim)}
  .status-badge.critical{color:var(--crit);border-color:var(--crit);background:var(--crit-dim);animation:crit-blink 1s infinite}
  .status-badge.frozen,.status-badge.frozen_crit{color:var(--frozen);border-color:var(--frozen);background:var(--frozen-dim)}
  .status-badge.frozen_crit{animation:crit-blink .6s infinite}
  .status-badge.disconnected{color:var(--disc);border-color:var(--disc);background:var(--disc-dim)}
  .status-badge.init{color:var(--init);border-color:var(--init)}

  .price-row{display:flex;align-items:center;justify-content:center;gap:10px;margin:10px 0;padding:10px;background:var(--bg);border:1px solid var(--border)}
  .price-side{text-align:center;flex:1}
  .price-label{font-size:9px;letter-spacing:2px;color:var(--text-dim);font-family:var(--mono);margin-bottom:3px}
  .price-val{font-family:var(--mono);font-size:14px;color:#fff}
  .price-val.bid{color:#00e5a0}.price-val.ask{color:#ff6b6b}
  .price-qty{font-family:var(--mono);font-size:10px;color:var(--text-dim)}
  .price-sep{color:var(--text-dim);font-family:var(--mono);font-size:12px;flex-shrink:0}

  /* Frozen info block — visible only when frozen */
  .frozen-info{margin:8px 0;padding:8px 10px;border:1px solid var(--frozen);background:var(--frozen-dim);font-family:var(--mono);font-size:10px;color:var(--frozen);line-height:1.7}
  .frozen-info .fi-row{display:flex;justify-content:space-between;gap:8px}
  .frozen-info .fi-label{color:var(--text-dim)}

  .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
  .stat{display:flex;flex-direction:column;gap:2px;cursor:default}
  .stat-label{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-dim);font-family:var(--mono);display:flex;align-items:center;gap:4px}
  .stat-val{font-family:var(--mono);font-size:12px;color:var(--text)}
  .stat-val.danger{color:var(--crit)}.stat-val.warn{color:var(--warn)}.stat-val.frozen-v{color:var(--frozen)}
  .stat-full{grid-column:1/-1}

  .silence-bar-wrap{margin-top:10px;height:3px;background:var(--bg);border:1px solid var(--border);overflow:hidden}
  .silence-bar{height:100%;transition:width .5s,background .3s}

  /* Events */
  .events-panel{background:var(--bg2);border:1px solid var(--border);overflow:hidden}
  .events-header{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--bg3);flex-wrap:wrap}
  .events-title{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--accent)}
  .events-meta{font-family:var(--mono);font-size:10px;color:var(--text-dim);margin-left:auto}
  .events-pagination{display:flex;align-items:center;gap:6px}
  .pg-btn{font-family:var(--mono);font-size:10px;padding:3px 10px;cursor:pointer;border:1px solid var(--border2);background:var(--bg3);color:var(--text-dim);transition:all .15s}
  .pg-btn:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
  .pg-btn:disabled{opacity:.3;cursor:default}
  .pg-info{font-family:var(--mono);font-size:10px;color:var(--text-dim);white-space:nowrap;min-width:80px;text-align:center}

  .events-filters{display:flex;gap:6px;padding:8px 16px;border-bottom:1px solid var(--border);background:var(--bg);flex-wrap:wrap}
  .filter-btn{font-family:var(--mono);font-size:9px;letter-spacing:1px;text-transform:uppercase;padding:3px 10px;cursor:pointer;border:1px solid var(--border2);background:transparent;color:var(--text-dim);transition:all .15s}
  .filter-btn.active{background:var(--border2);color:var(--text)}
  .filter-btn.f-disconnect.active{border-color:var(--crit);color:var(--crit)}
  .filter-btn.f-reconnect.active{border-color:var(--ok);color:var(--ok)}
  .filter-btn.f-frozen.active{border-color:var(--frozen);color:var(--frozen)}
  .filter-btn.f-warning.active{border-color:var(--warn);color:var(--warn)}
  .filter-btn.f-critical.active{border-color:var(--crit);color:var(--crit)}
  .filter-btn.f-info.active{border-color:var(--text-dim);color:var(--text-dim)}

  .events-list{max-height:420px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--border2) transparent}
  .event-row{display:grid;grid-template-columns:160px 90px 75px 1fr;gap:10px;padding:7px 16px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:11px;align-items:start}
  .event-row:hover{background:var(--bg3)}
  .event-ts{color:var(--text-dim);font-size:9px;padding-top:1px}
  .event-pair{color:var(--accent);letter-spacing:1px;font-size:10px}
  .event-kind{text-transform:uppercase;font-size:9px;letter-spacing:2px;padding-top:1px}
  .event-kind.disconnect{color:var(--crit)}.event-kind.reconnect{color:var(--ok)}
  .event-kind.warning{color:var(--warn)}.event-kind.critical{color:var(--crit)}
  .event-kind.frozen{color:var(--frozen)}.event-kind.info{color:var(--text-dim)}
  .event-msg{color:var(--text);font-size:10px;line-height:1.5;word-break:break-word}
  .empty{padding:24px;text-align:center;color:var(--text-dim);font-family:var(--mono);font-size:11px}

  @media(max-width:700px){
    .grid{grid-template-columns:1fr}
    .event-row{grid-template-columns:1fr 1fr;grid-template-rows:auto auto auto}
    .event-msg{grid-column:1/-1}
  }
  ::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="header-left">
      <div class="logo">ORDER<span>BOOK</span> MONITOR</div>
      <div class="tag" id="providerTag">BYBIT &middot; CITRONUS</div>
    </div>
    <div class="ws-status"><div class="ws-dot" id="wsDot"></div><span id="wsLabel">CONNECTING</span></div>
  </header>

  <div class="controls">
    <span class="provider-label">Provider:</span>
    <div class="seg">
      <button class="seg-btn active" id="btn-bybit" onclick="switchProvider('bybit')">PROD</button>
      <button class="seg-btn" id="btn-bombit" onclick="switchProvider('bombit')">STAGE</button>
    </div>
    <button class="restart-btn" id="restartBtn" onclick="restartMonitoring()">&#x21BA; Restart Monitoring</button>
  </div>

  <div class="summary-bar">
    <div class="sum-card" data-tip="Всего пар под мониторингом.">
      <div class="sum-label">Всего пар <span class="help-icon">?</span></div><div class="sum-value" id="sumTotal">-</div>
    </div>
    <div class="sum-card" data-tip="Подключены и данные меняются (тишина < 12с).">
      <div class="sum-label">Online <span class="help-icon">?</span></div><div class="sum-value ok" id="sumOk">-</div>
    </div>
    <div class="sum-card" data-tip="Нет данных 12–25 секунд. Фид медленный.">
      <div class="sum-label">Warning <span class="help-icon">?</span></div><div class="sum-value warn" id="sumWarn">-</div>
    </div>
    <div class="sum-card" data-tip="Нет данных более 25 секунд. Фид сломан.">
      <div class="sum-label">Critical <span class="help-icon">?</span></div><div class="sum-value crit" id="sumCrit">-</div>
    </div>
    <div class="sum-card" data-tip="Данные приходят, но bid/ask/объёмы не меняются более 5 секунд.\nFROZEN CRIT — не меняются более 30 секунд.">
      <div class="sum-label">Frozen <span class="help-icon">?</span></div><div class="sum-value frozen" id="sumFrozen">-</div>
    </div>
    <div class="sum-card" data-tip="WebSocket закрыт, идёт переподключение.">
      <div class="sum-label">Offline <span class="help-icon">?</span></div><div class="sum-value" id="sumDisc">-</div>
    </div>
    <div class="sum-card" data-tip="Суммарно разрывов по всем парам.">
      <div class="sum-label">Разрывов <span class="help-icon">?</span></div><div class="sum-value" id="sumDc">-</div>
    </div>
  </div>

  <div class="grid" id="pairsGrid"></div>

  <div class="events-panel">
    <div class="events-header">
      <div class="events-title">EVENT LOG</div>
      <div class="events-meta" id="eventsMeta">0 событий</div>
      <div class="events-pagination">
        <button class="pg-btn" id="pgPrev" onclick="changePage(-1)" disabled>&#x2190;</button>
        <span class="pg-info" id="pgInfo">стр. 1 / 1</span>
        <button class="pg-btn" id="pgNext" onclick="changePage(1)" disabled>&#x2192;</button>
      </div>
    </div>
    <div class="events-filters">
      <button class="filter-btn active" onclick="setFilter('all',this)">ВСЕ</button>
      <button class="filter-btn f-disconnect" onclick="setFilter('disconnect',this)">DISCONNECT</button>
      <button class="filter-btn f-reconnect" onclick="setFilter('reconnect',this)">RECONNECT</button>
      <button class="filter-btn f-frozen" onclick="setFilter('frozen',this)">FROZEN</button>
      <button class="filter-btn f-warning" onclick="setFilter('warning',this)">WARNING</button>
      <button class="filter-btn f-critical" onclick="setFilter('critical',this)">CRITICAL</button>
      <button class="filter-btn f-info" onclick="setFilter('info',this)">INFO</button>
    </div>
    <div class="events-list" id="eventsList"><div class="empty">Ожидание событий...</div></div>
  </div>
</div>

<script>
const PAIRS = ["BTC-USDT","ETH-USDT","BNB-USDT","TRX-USDT","DOGE-USDT","XRP-USDT","SOL-USDT","LINK-USDT"];
const PER_PAGE = 50;
let states = {}, allEvents = [], currentPage = 1, activeFilter = 'all', ws = null, currentProvider = "bybit";
const dirtyPairs = new Set();
let rafPending = false, summaryDirty = false;

function scheduleRender() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => {
    dirtyPairs.forEach(p => { if (states[p]) updateCard(p, states[p]); });
    dirtyPairs.clear();
    if (summaryDirty) { updateSummary(); summaryDirty = false; }
    rafPending = false;
  });
}

function fmtSec(s) {
  s = Math.round(s||0);
  if (s < 60) return s+'s';
  if (s < 3600) return Math.floor(s/60)+'m '+( s%60)+'s';
  return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}

function initCards() {
  const grid = document.getElementById('pairsGrid');
  PAIRS.forEach(pair => {
    const c = document.createElement('div');
    c.className = 'pair-card init'; c.id = 'card-'+pair;
    c.innerHTML = cardHTML(pair, null);
    grid.appendChild(c);
  });
}

function cardHTML(pair, st) {
  const status    = st?.status ?? 'INIT';
  const bid       = st?.bid ?? '-';
  const bidQty    = st?.bid_qty ?? '';
  const ask       = st?.ask ?? '-';
  const askQty    = st?.ask_qty ?? '';
  const dc        = st?.disconnect_count ?? 0;
  const dt        = st?.total_downtime_sec ?? 0;
  const uptime    = st?.session_uptime ?? 0;
  const silence   = st?.silence ?? 0;
  const snaps     = st?.snapshot_count ?? 0;
  const isFrozen  = st?.is_frozen ?? false;
  const frozenFor = st?.frozen_for ?? 0;
  const frozenSince = st?.frozen_since_utc ?? '';
  const totalEps  = st?.total_freeze_episodes ?? 0;
  const totalFrSec= st?.total_frozen_sec ?? 0;
  const frozenPct = st?.frozen_pct ?? 0;
  const history   = st?.freeze_history ?? [];

  const sPct = Math.min(100,(silence/25)*100);
  const sClr = silence>25?'var(--crit)':silence>12?'var(--warn)':'var(--ok)';
  const sCls = silence>25?'danger':silence>12?'warn':'';
  const fvCls = totalFrSec > 0 ? 'frozen-v' : '';

  // Frozen info block — показывается только когда сейчас заморожено
  const frozenBlock = isFrozen ? `
    <div class="frozen-info">
      <div class="fi-row"><span class="fi-label">Заморожен с</span><span>${frozenSince.slice(11,19)} UTC</span></div>
      <div class="fi-row"><span class="fi-label">Заморожен уже</span><span>${fmtSec(frozenFor)}</span></div>
      <div class="fi-row"><span class="fi-label">bid / ask</span><span>${bid} / ${ask}</span></div>
    </div>` : '';

  // История эпизодов для тултипа
  const histLines = history.length
    ? history.slice(-5).reverse().map(ep =>
        `${ep.start.slice(11,19)}  →  ${ep.end ? ep.end.slice(11,19) : 'сейчас'}  (${fmtSec(ep.duration_sec)})`
      ).join('\\n')
    : 'Эпизодов ещё не было';

  return `
    <div class="card-header">
      <div class="pair-name">${pair}</div>
      <div class="status-badge ${status.toLowerCase()}">${status.replace('_',' ')}</div>
    </div>
    <div class="price-row">
      <div class="price-side">
        <div class="price-label">BID</div>
        <div class="price-val bid">${bid}</div>
        <div class="price-qty">${bidQty}</div>
      </div>
      <div class="price-sep">&#x27F7;</div>
      <div class="price-side">
        <div class="price-label">ASK</div>
        <div class="price-val ask">${ask}</div>
        <div class="price-qty">${askQty}</div>
      </div>
    </div>
    ${frozenBlock}
    <div class="stats-grid">
      <div class="stat" data-tip="Секунд без входящих данных.\\nOrange >12с, Red >25с.">
        <div class="stat-label">Тишина <span class="help-icon">?</span></div>
        <div class="stat-val ${sCls}">${silence}с</div>
      </div>
      <div class="stat" data-tip="Снапшотов получено с момента подключения.">
        <div class="stat-label">Снапшотов <span class="help-icon">?</span></div>
        <div class="stat-val">${snaps}</div>
      </div>
      <div class="stat" data-tip="Сколько раз WebSocket разрывался для этой пары.">
        <div class="stat-label">Разрывов <span class="help-icon">?</span></div>
        <div class="stat-val ${dc>0?'warn':''}">${dc}</div>
      </div>
      <div class="stat" data-tip="Суммарное время оффлайн / длительность сессии.">
        <div class="stat-label">Даунтайм <span class="help-icon">?</span></div>
        <div class="stat-val ${dt>30?'danger':dt>5?'warn':''}">${fmtSec(dt)} / ${fmtSec(uptime)}</div>
      </div>
      <div class="stat stat-full" data-tip="Заморозка = bid/ask/объёмы не менялись >5с.\\nCRIT = не менялись >30с.\\n\\nПоследние эпизоды:\\n${histLines}">
        <div class="stat-label">История заморозок <span class="help-icon">?</span></div>
        <div class="stat-val ${fvCls}">${totalEps} раз заморажив. &nbsp;&middot;&nbsp; ${fmtSec(totalFrSec)} всего &nbsp;&middot;&nbsp; ${frozenPct}% сессии</div>
      </div>
    </div>
    <div class="silence-bar-wrap">
      <div class="silence-bar" style="width:${sPct}%;background:${sClr}"></div>
    </div>`;
}

function updateCard(pair, st) {
  const card = document.getElementById('card-'+pair);
  if (!card) return;
  card.className = 'pair-card '+(st.status||'init').toLowerCase();
  card.innerHTML = cardHTML(pair, st);
}

function updateSummary() {
  const vals = Object.values(states);
  document.getElementById('sumTotal').textContent  = vals.length||PAIRS.length;
  document.getElementById('sumOk').textContent     = vals.filter(s=>s.status==='OK').length;
  document.getElementById('sumWarn').textContent   = vals.filter(s=>s.status==='WARNING').length;
  document.getElementById('sumCrit').textContent   = vals.filter(s=>s.status==='CRITICAL').length;
  document.getElementById('sumFrozen').textContent = vals.filter(s=>s.status==='FROZEN'||s.status==='FROZEN_CRIT').length;
  document.getElementById('sumDisc').textContent   = vals.filter(s=>s.status==='DISCONNECTED').length;
  document.getElementById('sumDc').textContent     = vals.reduce((a,s)=>a+(s.disconnect_count||0),0);
}

function filteredEvents() {
  return activeFilter==='all' ? allEvents : allEvents.filter(e=>e.kind===activeFilter);
}

function renderEvents() {
  const fe = filteredEvents();
  const total = fe.length;
  const pages = Math.max(1, Math.ceil(total/PER_PAGE));
  if (currentPage > pages) currentPage = pages;
  const start = (currentPage-1)*PER_PAGE;
  const slice = fe.slice(start, start+PER_PAGE);

  document.getElementById('eventsMeta').textContent =
    `${total.toLocaleString()} событий${activeFilter!=='all'?' (фильтр)':''} / ${allEvents.length.toLocaleString()} всего`;
  document.getElementById('pgInfo').textContent = `стр. ${currentPage} / ${pages}`;
  document.getElementById('pgPrev').disabled = currentPage<=1;
  document.getElementById('pgNext').disabled = currentPage>=pages;

  const list = document.getElementById('eventsList');
  if (!slice.length) { list.innerHTML='<div class="empty">Нет событий по фильтру.</div>'; return; }
  list.innerHTML = slice.map(ev => {
    const kind = ev.kind||'info';
    return `<div class="event-row">
      <div class="event-ts">${ev.ts}</div>
      <div class="event-pair">${ev.pair}</div>
      <div class="event-kind ${kind}">${kind.toUpperCase()}</div>
      <div class="event-msg">${ev.message}</div>
    </div>`;
  }).join('');
}

function setFilter(f, btn) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  currentPage = 1;
  renderEvents();
}

function changePage(dir) {
  currentPage += dir;
  renderEvents();
  document.getElementById('eventsList').scrollTop = 0;
}

function prependEvent(ev) {
  allEvents.unshift(ev);
  // Always re-render if on page 1 (updates buttons, counts, and rows)
  if (currentPage === 1) {
    renderEvents();
  } else {
    // On other pages just update the meta count
    const fe = filteredEvents();
    const pages = Math.max(1, Math.ceil(fe.length / PER_PAGE));
    document.getElementById('eventsMeta').textContent =
      `${fe.length.toLocaleString()} событий${activeFilter!=='all'?' (фильтр)':''} / ${allEvents.length.toLocaleString()} всего`;
    document.getElementById('pgInfo').textContent = `стр. ${currentPage} / ${pages}`;
    document.getElementById('pgPrev').disabled = currentPage <= 1;
    document.getElementById('pgNext').disabled = currentPage >= pages;
  }
}

function setProviderUI(prov) {
  currentProvider = prov;
  document.querySelectorAll('.seg-btn').forEach(b=>b.classList.remove('active'));
  const btn = document.getElementById('btn-'+prov);
  if (btn) btn.classList.add('active');
  const labels = {bybit:'PROD', bombit:'STAGE'};
  document.getElementById('providerTag').textContent = (labels[prov]||prov.toUpperCase())+' · CITRONUS';
}

function switchProvider(prov) {
  if (prov===currentProvider) return;
  setProviderUI(prov);
  restartMonitoring();
}

async function restartMonitoring() {
  const btn = document.getElementById('restartBtn');
  btn.classList.add('loading'); btn.textContent='... Restarting';
  try {
    const resp = await fetch('/api/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:currentProvider})});
    const data = await resp.json();
    setProviderUI(data.provider);
  } catch(e) { console.error(e); }
  btn.classList.remove('loading'); btn.textContent='↺ Restart Monitoring';
}

function connect() {
  const proto = location.protocol==='https:'?'wss':'ws';
  ws = new WebSocket(proto+'://'+location.host+'/ws');
  ws.onopen = () => { document.getElementById('wsDot').className='ws-dot connected'; document.getElementById('wsLabel').textContent='LIVE'; };
  ws.onclose = () => { document.getElementById('wsDot').className='ws-dot error'; document.getElementById('wsLabel').textContent='DISCONNECTED'; setTimeout(connect,2000); };
  ws.onerror = () => { document.getElementById('wsDot').className='ws-dot error'; };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type==='init') {
      states=msg.states||{}; allEvents=msg.events||[];
      const any=Object.values(states)[0];
      if (any?.provider) setProviderUI(any.provider);
      Object.values(states).forEach(st=>updateCard(st.pair,st));
      updateSummary(); currentPage=1; renderEvents();
    }
    if (msg.type==='state') {
      states[msg.data.pair]=msg.data;
      dirtyPairs.add(msg.data.pair); summaryDirty=true; scheduleRender();
    }
    if (msg.type==='event') { prependEvent(msg.data); }
    if (msg.type==='restart') {
      states={}; allEvents=[];
      PAIRS.forEach(p=>{ const c=document.getElementById('card-'+p); if(c){c.className='pair-card init';c.innerHTML=cardHTML(p,null);} });
      currentPage=1; renderEvents(); updateSummary();
    }
  };
}

initCards();
connect();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def root():
    return DASHBOARD_HTML