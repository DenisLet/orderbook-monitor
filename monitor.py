# -*- coding: utf-8 -*-
"""
BetX2 — prediction market с ордербуком (Polymarket-style)
Run: uvicorn betx2:app --host 0.0.0.0 --port 8000

МЕХАНИКА:
  • Актив: YES-контракт стоимостью $1 при победе
  • Цена = вероятность (0–99¢)
  • BID = хочу купить YES (ставлю на YES)
  • ASK = хочу продать YES (ставлю на NO)
  • При матче: покупатель платит price¢, продавец — (100-price)¢ за контракт
  • Победитель забирает $1 за контракт
"""
import asyncio, uuid, logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__).info

# ─── Данные ───────────────────────────────────────────────────────────────
users     = {}   # uid → {id, name, balance, color}
markets   = {}   # mid → {id, title, desc, category, status, outcome, last_price, created_at}
orders    = {}   # oid → {id, market_id, user_id, side, price, contracts, filled, status, created_at}
trades    = {}   # tid → {id, market_id, buy_oid, sell_oid, price, contracts, created_at}
activity  = []
ws_clients: List[WebSocket] = []
CONFIG    = {"commission_pct": 0.02}   # 2% с выигрыша
SERVICE_BALANCE = {"total": 0.0}       # накопленные комиссии сервиса
COLORS    = ["#6366f1","#ec4899","#f59e0b","#10b981","#3b82f6","#ef4444","#8b5cf6","#14b8a6"]

def gen_id():   return uuid.uuid4().hex[:10]
def now_ts():   return datetime.now(timezone.utc).strftime("%H:%M:%S")
def now_full(): return datetime.now(timezone.utc).strftime("%d.%m %H:%M")

def add_act(kind, msg):
    e = {"kind": kind, "message": msg, "ts": now_ts()}
    activity.append(e)
    if len(activity) > 400: activity.pop(0)
    return e

async def broadcast(msg):
    dead = []
    for ws in ws_clients:
        try:    await ws.send_json(msg)
        except: dead.append(ws)
    for ws in dead:
        if ws in ws_clients: ws_clients.remove(ws)

# ─── Helpers ──────────────────────────────────────────────────────────────
def get_open_orders(mid):
    return [o for o in orders.values()
            if o["market_id"] == mid and o["status"] == "open"]

def get_orderbook(mid):
    bids = {}
    asks = {}
    for o in get_open_orders(mid):
        rem = o["contracts"] - o["filled"]
        if rem <= 0: continue
        if o["side"] == "BUY":
            bids[o["price"]] = round(bids.get(o["price"], 0) + rem, 4)
        else:
            asks[o["price"]] = round(asks.get(o["price"], 0) + rem, 4)
    return {
        "bids": sorted([{"price": p, "contracts": c, "dollars": round(p/100*c, 2)}
                        for p, c in bids.items()], key=lambda x: -x["price"]),
        "asks": sorted([{"price": p, "contracts": c, "dollars": round((100-p)/100*c, 2)}
                        for p, c in asks.items()], key=lambda x:  x["price"]),
    }

def get_market_trades(mid, limit=50):
    ts = [t for t in trades.values() if t["market_id"] == mid]
    return sorted(ts, key=lambda x: x["created_at"], reverse=True)[:limit]

def market_snap(mid):
    m  = markets[mid]
    ob = get_orderbook(mid)
    ts = get_market_trades(mid, 1)
    last_price  = ts[0]["price"] if ts else m.get("last_price")
    best_bid    = ob["bids"][0]["price"] if ob["bids"] else None
    best_ask    = ob["asks"][0]["price"] if ob["asks"] else None
    mid_price   = round((best_bid + best_ask) / 2, 1) if (best_bid and best_ask) else last_price
    trade_count = len([t for t in trades.values() if t["market_id"] == mid])
    volume      = round(sum(t["price"]/100 * t["contracts"] + (100-t["price"])/100 * t["contracts"]
                            for t in trades.values() if t["market_id"] == mid), 2)
    return {
        **m,
        "last_price":  last_price,
        "best_bid":    best_bid,
        "best_ask":    best_ask,
        "mid_price":   mid_price,
        "spread":      round(best_ask - best_bid, 1) if (best_bid and best_ask and best_ask != best_bid) else None,
        "orderbook":   ob,
        "trade_count": trade_count,
        "volume":      volume,
    }

def do_match(mid, new_oid):
    new_ord = orders[new_oid]
    new_trades = []

    while True:
        new_rem = new_ord["contracts"] - new_ord["filled"]
        if new_rem < 0.0001 or new_ord["status"] != "open":
            break

        if new_ord["side"] == "BUY":
            candidates = [o for o in get_open_orders(mid)
                          if o["side"] == "SELL" and o["price"] <= new_ord["price"]
                          and o["id"] != new_oid
                          and o["user_id"] != new_ord["user_id"]]  # no self-match
            if not candidates: break
            opp = min(candidates, key=lambda x: x["price"])
            exec_price = opp["price"]
        else:
            candidates = [o for o in get_open_orders(mid)
                          if o["side"] == "BUY" and o["price"] >= new_ord["price"]
                          and o["id"] != new_oid
                          and o["user_id"] != new_ord["user_id"]]  # no self-match
            if not candidates: break
            opp = max(candidates, key=lambda x: x["price"])
            exec_price = opp["price"]

        opp_rem   = opp["contracts"] - opp["filled"]
        match_qty = round(min(new_rem, opp_rem), 4)
        if match_qty < 0.0001: break

        if new_ord["side"] == "BUY":
            buy_ord, sell_ord = new_ord, opp
        else:
            buy_ord, sell_ord = opp, new_ord

        buyer_cost  = round(exec_price / 100 * match_qty, 4)
        seller_cost = round((100 - exec_price) / 100 * match_qty, 4)

        buy_user  = users[buy_ord["user_id"]]
        sell_user = users[sell_ord["user_id"]]

        buyer_overpay = round((buy_ord["price"] - exec_price) / 100 * match_qty, 4)
        if buyer_overpay > 0:
            buy_user["balance"] = round(buy_user["balance"] + buyer_overpay, 4)

        opp["filled"]     = round(opp["filled"]     + match_qty, 4)
        new_ord["filled"] = round(new_ord["filled"]  + match_qty, 4)
        if opp["contracts"] - opp["filled"] < 0.0001:
            opp["filled"] = opp["contracts"]; opp["status"] = "filled"
        if new_ord["contracts"] - new_ord["filled"] < 0.0001:
            new_ord["filled"] = new_ord["contracts"]; new_ord["status"] = "filled"

        tid = gen_id()
        t = {"id": tid, "market_id": mid,
             "buy_order_id": buy_ord["id"], "sell_order_id": sell_ord["id"],
             "buy_user_id":  buy_ord["user_id"], "sell_user_id": sell_ord["user_id"],
             "price":     exec_price,
             "contracts": match_qty,
             "buyer_cost":  buyer_cost,
             "seller_cost": seller_cost,
             "created_at": now_ts()}
        trades[tid] = t
        new_trades.append(t)

        log(f"TRADE {mid[:8]}: {match_qty} contracts @ {exec_price}¢ | "
            f"buyer={users[buy_ord['user_id']]['name']} seller={users[sell_ord['user_id']]['name']}")

        buy_name  = users[buy_ord["user_id"]]["name"]
        sell_name = users[sell_ord["user_id"]]["name"]
        add_act("trade", f"Сделка @ {exec_price}¢ × {match_qty:.1f} | {buy_name} ↑ / {sell_name} ↓ | {markets[mid]['title'][:35]}")

    return new_trades

# ─── Lifespan / seed data ─────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    for i, (name, bal) in enumerate([("Алексей",10000),("Мария",8000),("Дмитрий",5000),("Анна",12000)]):
        uid = gen_id()
        users[uid] = {"id": uid, "name": name, "balance": float(bal), "color": COLORS[i]}
    for title, desc, cat in [
        ("Аргентина выиграет ЧМ 2026?",        "Сборная Аргентины станет чемпионом мира FIFA 2026", "Спорт"),
        ("Bitcoin превысит $150k до конца 2026?","BTC коснётся отметки $150,000 на любой бирже",    "Крипто"),
        ("Новая страна вступит в ЕС до 2027?",  "Любая страна официально вступит в Евросоюз",       "Политика"),
    ]:
        mid = gen_id()
        markets[mid] = {"id": mid, "title": title, "desc": desc, "category": cat,
                        "status": "open", "outcome": None, "last_price": None,
                        "created_at": now_full()}
    yield

app = FastAPI(lifespan=lifespan)

# ─── Pydantic ──────────────────────────────────────────────────────────────
class CreateUser(BaseModel):  name: str; balance: float = 5000.0
class CreateMarket(BaseModel): title: str; desc: str = ""; category: str = "Другое"
class PlaceOrder(BaseModel):
    user_id: str; market_id: str
    side:  str
    price: float
    dollars: float
class CancelOrder(BaseModel): order_id: str; user_id: str
class ResolveMarket(BaseModel): market_id: str; outcome: str
class UpdateConfig(BaseModel): commission_pct: float

# ─── API ───────────────────────────────────────────────────────────────────
@app.get("/api/state")
async def get_state():
    return {"users":    list(users.values()),
            "markets":  [market_snap(m) for m in markets],
            "activity": list(reversed(activity[-60:])),
            "config":   CONFIG,
            "service_balance": SERVICE_BALANCE["total"]}

@app.post("/api/users")
async def create_user(body: CreateUser):
    uid = gen_id()
    u = {"id": uid, "name": body.name.strip()[:24],
         "balance": round(body.balance, 2), "color": COLORS[len(users) % len(COLORS)]}
    users[uid] = u
    ev = add_act("user", f"Новый участник: {u['name']}")
    await broadcast({"type": "users",    "data": list(users.values())})
    await broadcast({"type": "activity", "data": ev})
    return u

@app.post("/api/markets")
async def create_market(body: CreateMarket):
    mid = gen_id()
    markets[mid] = {"id": mid, "title": body.title.strip()[:200],
                    "desc": body.desc.strip()[:500], "category": body.category.strip()[:30],
                    "status": "open", "outcome": None, "last_price": None,
                    "created_at": now_full()}
    ev = add_act("market", f"Новый рынок: {body.title[:60]}")
    await broadcast({"type": "markets",  "data": [market_snap(m) for m in markets]})
    await broadcast({"type": "activity", "data": ev})
    return market_snap(mid)

@app.post("/api/order")
async def place_order(body: PlaceOrder):
    if body.user_id   not in users:   raise HTTPException(404, "Пользователь не найден")
    if body.market_id not in markets: raise HTTPException(404, "Рынок не найден")
    if markets[body.market_id]["status"] != "open": raise HTTPException(400, "Рынок закрыт")
    if body.side not in ("BUY","SELL"): raise HTTPException(400, "side: BUY или SELL")
    price = round(body.price * 10) / 10  # до 0.1¢
    if not 0.1 <= price <= 99.9: raise HTTPException(400, "Цена: 0.1–99.9 центов")
    if body.dollars < 0.01: raise HTTPException(400, "Минимум $0.01")

    if body.side == "BUY":
        contracts = round(body.dollars / (price / 100), 4)
        cost      = round(body.dollars, 4)
    else:
        contracts = round(body.dollars / ((100 - price) / 100), 4)
        cost      = round(body.dollars, 4)

    u = users[body.user_id]
    if u["balance"] < cost:
        raise HTTPException(400, f"Недостаточно средств. Баланс: ${u['balance']:.2f}, нужно: ${cost:.2f}")

    u["balance"] = round(u["balance"] - cost, 4)

    oid = gen_id()
    orders[oid] = {"id": oid, "market_id": body.market_id, "user_id": body.user_id,
                   "side": body.side, "price": price, "contracts": contracts,
                   "filled": 0.0, "status": "open", "cost": cost, "created_at": now_ts()}

    side_lbl = "YES (BUY)" if body.side == "BUY" else "NO (SELL)"
    log(f"ORDER {body.market_id[:8]}: {side_lbl} {contracts:.2f}c @ {price}¢ (${cost:.2f}) by {u['name']}")
    add_act("order", f"{u['name']} → {side_lbl} @ {price}¢ × {contracts:.1f}c (${cost:.2f}) | {markets[body.market_id]['title'][:35]}")

    new_trades = do_match(body.market_id, oid)

    await broadcast({"type": "markets",  "data": [market_snap(m) for m in markets]})
    await broadcast({"type": "users",    "data": list(users.values())})
    await broadcast({"type": "activity", "data": activity[-1]})
    o = orders[oid]
    return {"order_id": oid, "contracts": contracts, "price": price,
            "cost": cost, "filled": o["filled"], "status": o["status"],
            "trades": len(new_trades)}

@app.post("/api/order/cancel")
async def cancel_order(body: CancelOrder):
    if body.order_id not in orders: raise HTTPException(404, "Ордер не найден")
    o = orders[body.order_id]
    if o["user_id"] != body.user_id: raise HTTPException(403, "Не ваш ордер")
    if o["status"] != "open": raise HTTPException(400, "Ордер уже закрыт")
    o["status"] = "cancelled"
    rem_contracts = o["contracts"] - o["filled"]
    refund = 0.0
    if rem_contracts > 0:
        if o["side"] == "BUY":
            refund = round(rem_contracts * o["price"] / 100, 4)
        else:
            refund = round(rem_contracts * (100 - o["price"]) / 100, 4)
        users[body.user_id]["balance"] = round(users[body.user_id]["balance"] + refund, 4)
        log(f"CANCEL {body.order_id[:8]}: refund ${refund:.2f} to {users[body.user_id]['name']}")
    await broadcast({"type": "markets",  "data": [market_snap(m) for m in markets]})
    await broadcast({"type": "users",    "data": list(users.values())})
    return {"cancelled": True, "refund": refund}

@app.post("/api/resolve")
async def resolve_market(body: ResolveMarket):
    if body.market_id not in markets:    raise HTTPException(404, "Рынок не найден")
    m = markets[body.market_id]
    if m["status"] == "resolved":        raise HTTPException(400, "Уже разрешён")
    if body.outcome not in ("YES","NO"): raise HTTPException(400, "outcome: YES или NO")

    m["status"] = "resolved"; m["outcome"] = body.outcome

    refunds = {}
    for o in orders.values():
        if o["market_id"] != body.market_id or o["status"] != "open": continue
        o["status"] = "cancelled"
        rem = o["contracts"] - o["filled"]
        if rem > 0:
            if o["side"] == "BUY":
                ref = round(rem * o["price"] / 100, 4)
            else:
                ref = round(rem * (100 - o["price"]) / 100, 4)
            users[o["user_id"]]["balance"] = round(users[o["user_id"]]["balance"] + ref, 4)
            refunds[o["user_id"]] = round(refunds.get(o["user_id"], 0) + ref, 4)

    payouts = {}
    comm    = CONFIG["commission_pct"]
    total_commission = 0.0
    for t in trades.values():
        if t["market_id"] != body.market_id: continue
        if body.outcome == "YES":
            winner_uid = t["buy_user_id"]
        else:
            winner_uid = t["sell_user_id"]
        contracts  = t["contracts"]
        gross = round(contracts * 1.0, 4)
        fee   = round(gross * comm, 4)
        net   = round(gross - fee, 4)
        payouts[winner_uid] = round(payouts.get(winner_uid, 0) + net, 4)
        total_commission = round(total_commission + fee, 4)

    for uid, amt in payouts.items():
        if uid in users:
            users[uid]["balance"] = round(users[uid]["balance"] + amt, 4)

    # Зачисляем комиссию на счёт сервиса
    SERVICE_BALANCE["total"] = round(SERVICE_BALANCE["total"] + total_commission, 4)

    total_payout = sum(payouts.values())
    total_refund = sum(refunds.values())
    lbl = "ДА" if body.outcome == "YES" else "НЕТ"
    ev  = add_act("resolve", f"Рынок «{m['title'][:40]}» → {lbl} | выплаты: ${total_payout:.2f} | комиссия: ${total_commission:.2f}")
    log(f"RESOLVE {body.market_id[:8]} → {body.outcome} | payouts=${total_payout:.2f} commission=${total_commission:.2f}")

    await broadcast({"type": "markets",         "data": [market_snap(m) for m in markets]})
    await broadcast({"type": "users",           "data": list(users.values())})
    await broadcast({"type": "activity",        "data": ev})
    await broadcast({"type": "service_balance", "data": SERVICE_BALANCE["total"]})
    return {"outcome": body.outcome, "payouts": payouts, "refunds": refunds,
            "total_payout": total_payout, "total_commission": total_commission}

@app.get("/api/orders/{market_id}")
async def get_orders(market_id: str):
    ords = [o for o in orders.values() if o["market_id"] == market_id]
    return [{**o, "user_name": users.get(o["user_id"], {}).get("name", "?"),
             "user_color": users.get(o["user_id"], {}).get("color", "#666")} for o in ords]

@app.get("/api/trades/{market_id}")
async def get_trades(market_id: str):
    ts = get_market_trades(market_id, 50)
    result = []
    for t in ts:
        result.append({**t,
            "buy_user_name":  users.get(t["buy_user_id"],  {}).get("name", "?"),
            "sell_user_name": users.get(t["sell_user_id"], {}).get("name", "?"),
        })
    return result

@app.post("/api/config")
async def update_config(body: UpdateConfig):
    CONFIG["commission_pct"] = max(0, min(0.2, body.commission_pct))
    await broadcast({"type": "config", "data": CONFIG})
    return CONFIG

@app.get("/api/portfolio/{user_id}")
async def get_portfolio(user_id: str):
    if user_id not in users: raise HTTPException(404, "Не найден")
    positions = {}
    for t in trades.values():
        mid = t["market_id"]
        if mid not in positions:
            positions[mid] = {"market": markets.get(mid, {}), "yes_contracts": 0, "no_contracts": 0,
                              "yes_cost": 0, "no_cost": 0}
        if t["buy_user_id"] == user_id:
            positions[mid]["yes_contracts"] = round(positions[mid]["yes_contracts"] + t["contracts"], 4)
            positions[mid]["yes_cost"]      = round(positions[mid]["yes_cost"] + t["buyer_cost"], 4)
        if t["sell_user_id"] == user_id:
            positions[mid]["no_contracts"]  = round(positions[mid]["no_contracts"] + t["contracts"], 4)
            positions[mid]["no_cost"]       = round(positions[mid]["no_cost"] + t["seller_cost"], 4)
    pending = [o for o in orders.values() if o["user_id"] == user_id and o["status"] == "open"]
    return {"positions": list(positions.values()), "pending_orders": pending}

@app.post("/api/reset")
async def reset_state():
    """Полный сброс — пересоздаёт начальные данные."""
    users.clear(); markets.clear(); orders.clear(); trades.clear()
    activity.clear(); SERVICE_BALANCE["total"] = 0.0
    for i, (name, bal) in enumerate([("Алексей",10000),("Мария",8000),("Дмитрий",5000),("Анна",12000)]):
        uid = gen_id()
        users[uid] = {"id": uid, "name": name, "balance": float(bal), "color": COLORS[i]}
    for title, desc, cat in [
        ("Аргентина выиграет ЧМ 2026?",        "Сборная Аргентины станет чемпионом мира FIFA 2026", "Спорт"),
        ("Bitcoin превысит $150k до конца 2026?","BTC коснётся отметки $150,000 на любой бирже",    "Крипто"),
        ("Новая страна вступит в ЕС до 2027?",  "Любая страна официально вступит в Евросоюз",       "Политика"),
    ]:
        mid = gen_id()
        markets[mid] = {"id": mid, "title": title, "desc": desc, "category": cat,
                        "status": "open", "outcome": None, "last_price": None,
                        "created_at": now_full()}
    ev = add_act("market", "🔄 Сброс выполнен — данные пересозданы")
    log("RESET: state cleared and reseeded")
    state = await get_state()
    await broadcast({"type": "reset", **state})
    return {"ok": True}

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept(); ws_clients.append(ws)
    await ws.send_json({"type": "init", **(await get_state())})
    try:
        while True: await asyncio.sleep(30); await ws.send_json({"type": "ping"})
    except:
        if ws in ws_clients: ws_clients.remove(ws)

@app.get("/", response_class=HTMLResponse)
async def root(): return HTML

# ─── HTML ──────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BetX 2 — Prediction Markets</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#07070e;--s1:#0e0e1c;--s2:#141424;--s3:#1a1a2e;--b1:#222238;--b2:#2e2e50;
  --yes:#00d68f;--yes2:rgba(0,214,143,.1);--yes3:rgba(0,214,143,.22);
  --no:#ff3d5a;--no2:rgba(255,61,90,.1);--no3:rgba(255,61,90,.22);
  --gold:#ffc107;--gold2:rgba(255,193,7,.1);--gold3:rgba(255,193,7,.2);
  --purple:#7b68ff;--purple2:rgba(123,104,255,.12);--purple3:rgba(123,104,255,.28);
  --bid:#00d68f;--ask:#ff3d5a;
  --text:#b8c8e8;--text2:#4e6080;--text3:#2a3a58;
  --r:12px;--r2:8px;--r3:6px;
  --mono:'Space Mono',monospace;--sans:'Manrope',sans-serif;
  --fsize:13px;
}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;font-size:var(--fsize)}
body::before{content:'';position:fixed;inset:0;pointer-events:none;
  background:radial-gradient(ellipse 900px 600px at 0% 100%,rgba(0,214,143,.025) 0%,transparent 55%),
             radial-gradient(ellipse 700px 500px at 100% 0%,rgba(123,104,255,.035) 0%,transparent 55%)}
.app{max-width:1200px;margin:0 auto;padding:0 16px 80px}

/* ── HEADER ── */
header{display:flex;align-items:center;gap:10px;padding:12px 0 18px;border-bottom:1px solid var(--b1);margin-bottom:20px;flex-wrap:wrap}
.logo{font-family:var(--mono);font-size:14px;font-weight:700;color:#fff;letter-spacing:2px}
.logo em{color:var(--yes);font-style:normal}.logo sup{color:var(--purple);font-size:10px}
.logo small{color:var(--text3);font-size:9px;letter-spacing:1px;display:block;margin-top:2px}
.hsp{flex:1;min-width:8px}

/* ── SERVICE BALANCE CHIP ── */
.svc-chip{display:flex;align-items:center;gap:7px;padding:6px 12px;background:var(--gold2);border:1px solid var(--gold3);border-radius:30px}
.svc-chip-icon{font-size:11px}
.svc-chip-lbl{font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--gold);opacity:.7}
.svc-chip-val{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--gold)}

.wsdot{width:7px;height:7px;border-radius:50%;background:var(--text3);transition:.4s;flex-shrink:0}
.wsdot.on{background:var(--yes);box-shadow:0 0 8px var(--yes)}

/* ── USER DROPDOWN ── */
.upill{display:flex;align-items:center;gap:6px;padding:5px 11px 5px 6px;background:var(--s2);border:1px solid var(--b1);border-radius:30px;cursor:pointer;user-select:none;position:relative;transition:.15s}
.upill:hover{border-color:var(--b2)}
.av{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:10px;color:#fff;flex-shrink:0}
.uname{font-size:12px;font-weight:700;pointer-events:none}
.ubal{font-family:var(--mono);font-size:10px;color:var(--yes);pointer-events:none}
.chev{color:var(--text3);font-size:9px;pointer-events:none;transition:.2s}
.upill.open .chev{transform:rotate(180deg)}

/* Dropdown — fixed position, repositioned by JS to align under upill */
.dd{
  position:fixed;min-width:210px;
  background:var(--s2);border:1px solid var(--b2);border-radius:var(--r);
  padding:4px;z-index:9999;
  opacity:0;pointer-events:none;transform:translateY(-6px);
  transition:opacity .15s,transform .15s;
  box-shadow:0 20px 60px rgba(0,0,0,.85)
}
.dd.open{opacity:1;pointer-events:all;transform:none}
.ddi{
  display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:var(--r2);
  cursor:pointer;transition:.12s;user-select:none
}
.ddi:hover{background:var(--s3)}
.ddi.sel{background:var(--purple2)}
.ddi .av{width:24px;height:24px;font-size:9px;flex-shrink:0;pointer-events:none}
.ddi .diname{font-size:12px;font-weight:700;flex:1;pointer-events:none}
.ddi .dibal{font-family:var(--mono);font-size:10px;color:var(--yes);pointer-events:none}
.ddsep{padding:6px 10px 3px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--text3);user-select:none}
.ddnew{
  display:flex;align-items:center;gap:7px;padding:8px 10px;border-radius:var(--r2);
  cursor:pointer;color:var(--purple);font-size:11px;font-weight:700;
  border-top:1px solid var(--b1);margin-top:3px;transition:.12s
}
.ddnew:hover{background:var(--purple2)}

/* ── TABS ── */
.tabs{display:flex;gap:2px;margin-bottom:20px;background:var(--s1);border-radius:var(--r);padding:4px;width:fit-content;flex-wrap:wrap}
.tab{padding:6px 14px;border-radius:var(--r2);font-size:9px;font-weight:700;letter-spacing:1px;cursor:pointer;color:var(--text2);font-family:var(--mono);text-transform:uppercase;transition:.15s}
.tab.active{background:var(--s3);color:#fff}.tab:hover:not(.active){color:var(--text)}
.panel{display:none}.panel.active{display:block}

/* ── MARKET GRID ── */
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.mcard{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);overflow:hidden;cursor:pointer;transition:.22s}
.mcard:hover{border-color:var(--b2);transform:translateY(-2px);box-shadow:0 12px 40px rgba(0,0,0,.55)}
.mcard.resolved{opacity:.55;cursor:default;filter:saturate(0.4)}
.mcard-head{padding:14px 14px 10px}
.mc-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:7px}
.cat{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--text3)}
.badge{font-size:9px;font-weight:700;padding:2px 8px;border-radius:20px}
.badge.open{background:var(--yes2);color:var(--yes);border:1px solid var(--yes3)}
.badge.ry{background:var(--yes2);color:var(--yes);border:1px solid var(--yes3)}
.badge.rn{background:var(--no2);color:var(--no);border:1px solid var(--no3)}
.mc-title{font-size:14px;font-weight:700;line-height:1.4;color:#fff;margin-bottom:12px}
.mc-price{display:flex;align-items:baseline;gap:6px;margin-bottom:3px}
.mc-pval{font-family:var(--mono);font-size:28px;font-weight:700;color:#fff}
.mc-punit{font-size:11px;color:var(--text3)}
.mc-plbl{font-size:10px;color:var(--text2)}
.mc-ba{display:flex;gap:8px;margin-bottom:10px}
.mc-bid,.mc-ask{flex:1;padding:6px 8px;border-radius:var(--r3);text-align:center}
.mc-bid{background:var(--yes2);border:1px solid var(--yes3)}
.mc-ask{background:var(--no2);border:1px solid var(--no3)}
.mc-ba-lbl{font-size:8px;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:2px}
.mc-bid .mc-ba-lbl{color:var(--yes)}.mc-ask .mc-ba-lbl{color:var(--no)}
.mc-ba-val{font-family:var(--mono);font-size:15px;font-weight:700}
.mc-bid .mc-ba-val{color:var(--yes)}.mc-ask .mc-ba-val{color:var(--no)}
.mc-ba-odds{font-family:var(--mono);font-size:10px;font-weight:700;margin-top:2px;opacity:.75}
.mc-bid .mc-ba-odds{color:var(--yes)}.mc-ask .mc-ba-odds{color:var(--no)}
.mc-foot{display:flex;justify-content:space-between;padding:8px 14px;background:var(--s2);border-top:1px solid var(--b1);font-size:10px;color:var(--text3);font-family:var(--mono)}

/* ── MARKET DETAIL MODAL ── */
.ovl{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:300;display:none;align-items:flex-start;justify-content:center;backdrop-filter:blur(12px);padding:14px;overflow-y:auto}
.ovl.open{display:flex}
.mdl{background:var(--s1);border:1px solid var(--b2);border-radius:14px;width:100%;max-width:900px;margin:auto;animation:pop .2s cubic-bezier(.34,1.56,.64,1)}
@keyframes pop{from{transform:scale(.9) translateY(20px);opacity:0}to{transform:none;opacity:1}}
.mdl-head{padding:16px 20px;border-bottom:1px solid var(--b1);display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.mdl-title{font-size:15px;font-weight:700;line-height:1.4;flex:1}
.mcls{width:28px;height:28px;border-radius:50%;background:var(--s2);border:none;color:var(--text2);cursor:pointer;font-size:13px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.mcls:hover{background:var(--s3);color:var(--text)}
.mdl-body{padding:16px 20px;display:grid;grid-template-columns:1fr 360px;gap:20px}
@media(max-width:750px){.mdl-body{grid-template-columns:1fr}}
.ob-wrap{display:flex;flex-direction:column;gap:14px}
.ob{background:var(--s2);border:1px solid var(--b1);border-radius:var(--r);overflow:hidden}
.ob-hdr{display:grid;grid-template-columns:52px 1fr 1fr 1fr;padding:6px 12px;border-bottom:1px solid var(--b1);font-size:8px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text3);font-family:var(--mono)}
.ob-rows{font-family:var(--mono);font-size:11px}
/* Fixed-height scrollable zones for asks/bids */
.ob-asks-wrap{overflow-y:auto;max-height:220px;display:flex;flex-direction:column-reverse}
.ob-bids-wrap{overflow-y:auto;max-height:220px}
.ob-row{display:grid;grid-template-columns:52px 1fr 1fr 1fr;padding:4px 12px;transition:background .15s;cursor:pointer;position:relative}
.ask-row{border-left:2px solid rgba(255,61,90,.4)}
.bid-row{border-left:2px solid rgba(0,214,143,.4)}
.ask-row:hover{background:rgba(255,61,90,.08)!important;border-left-color:var(--ask)}
.bid-row:hover{background:rgba(0,214,143,.08)!important;border-left-color:var(--bid)}
.ob-row:hover{background:var(--s3)}
.ask-row .p{color:var(--ask)}.bid-row .p{color:var(--bid)}
/* Depth bar behind each row */
.ob-row .depth-bar{position:absolute;top:0;bottom:0;right:0;opacity:.13;pointer-events:none;transition:width .3s}
.ask-row .depth-bar{background:var(--ask)}.bid-row .depth-bar{background:var(--bid)}
/* Flash animation on match */
@keyframes ob-flash-buy{0%{background:rgba(0,214,143,.35)}100%{background:transparent}}
@keyframes ob-flash-sell{0%{background:rgba(255,61,90,.35)}100%{background:transparent}}
.ob-row.flash-buy{animation:ob-flash-buy .6s ease-out}
.ob-row.flash-sell{animation:ob-flash-sell .6s ease-out}
.ob-mid{padding:5px 12px;background:var(--s3);border-top:1px solid var(--b1);border-bottom:1px solid var(--b1);font-size:10px;color:var(--text2);font-family:var(--mono);display:flex;gap:10px;align-items:center}
.ob-mid .lp{font-size:13px;font-weight:700;color:#fff}
.ob-mid .spread{color:var(--text3);flex:1}
.ob-expand{font-size:9px;color:var(--purple);cursor:pointer;padding:2px 6px;border:1px solid var(--purple3);border-radius:10px;background:var(--purple2);font-family:var(--mono);font-weight:700;white-space:nowrap}
.ob-expand:hover{background:var(--purple3)}
.trades-wrap{background:var(--s2);border:1px solid var(--b1);border-radius:var(--r);max-height:220px;overflow-y:auto}
.trades-hdr{padding:8px 12px;border-bottom:1px solid var(--b1);font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text3);display:grid;grid-template-columns:40px 55px 1fr 50px;gap:4px}
.tr-row{display:grid;grid-template-columns:40px 55px 1fr 50px;align-items:center;gap:4px;padding:5px 12px;font-size:10px;border-bottom:1px solid var(--b1);font-family:var(--mono)}
.tr-price{font-size:12px;font-weight:700;color:#fff}
.tr-qty{color:var(--text3)}
.tr-names{color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tr-ts{color:var(--text3);font-size:9px;text-align:right}

/* ── ORDER FORM ── */
.form-wrap{display:flex;flex-direction:column;gap:12px}
.res-strip{background:var(--gold2);border:1px solid var(--gold3);border-radius:var(--r2);padding:10px 13px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.res-strip p{font-size:11px;color:var(--gold);font-weight:700;flex:1}
.rbtn{padding:7px 14px;border-radius:20px;border:none;cursor:pointer;font-weight:800;font-size:10px;font-family:var(--mono);transition:.15s}
.rbtn.y{background:var(--yes);color:#001a0d}.rbtn.n{background:var(--no);color:#fff}
.rbtn:hover{filter:brightness(1.1)}
.res-banner{text-align:center;padding:12px;border-radius:var(--r2);font-weight:800;font-size:13px;font-family:var(--mono)}
.res-banner.yes{background:var(--yes2);border:1px solid var(--yes3);color:var(--yes)}
.res-banner.no{background:var(--no2);border:1px solid var(--no3);color:var(--no)}
.oform{background:var(--s2);border:1px solid var(--b1);border-radius:var(--r);padding:14px}
.of-tabs{display:flex;gap:4px;margin-bottom:14px}
.of-tab{flex:1;padding:8px;border-radius:var(--r2);border:2px solid var(--b1);background:transparent;cursor:pointer;text-align:center;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:1px;transition:.15s}
.of-tab.buy.active{border-color:var(--yes);background:var(--yes2);color:var(--yes)}
.of-tab.sell.active{border-color:var(--no);background:var(--no2);color:var(--no)}
.of-tab:not(.active){color:var(--text2)}.of-tab:hover:not(.active){border-color:var(--b2);background:var(--s3)}
.flbl{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text2);margin-bottom:5px}
.frow{margin-bottom:12px}
.price-row{display:flex;align-items:center;gap:8px;margin-bottom:0}
.price-inp{width:80px;background:var(--s1);border:1px solid var(--b2);border-radius:var(--r2);font-family:var(--mono);font-size:22px;font-weight:700;padding:5px 8px;outline:none;transition:.2s;-moz-appearance:textfield}
.price-inp::-webkit-outer-spin-button,.price-inp::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.price-inp.buy{color:var(--yes);border-color:var(--yes3)}.price-inp.buy:focus{border-color:var(--yes)}
.price-inp.sell{color:var(--no);border-color:var(--no3)}.price-inp.sell:focus{border-color:var(--no)}
.price-unit{font-size:11px;color:var(--text3)}
.pslider{flex:1;-webkit-appearance:none;height:4px;border-radius:2px;background:var(--b1);outline:none}
.pslider.buy::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--yes);cursor:pointer}
.pslider.sell::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--no);cursor:pointer}
.pmarkers{display:flex;justify-content:space-between;font-size:9px;color:var(--text3);font-family:var(--mono);margin-top:3px;margin-bottom:2px}
.dinp-wrap{display:flex;align-items:center;gap:7px;margin-bottom:6px}
.dollar{font-size:16px;color:var(--text3);font-family:var(--mono);font-weight:700}
.dinp{flex:1;background:var(--s1);border:1px solid var(--b2);border-radius:var(--r2);color:#fff;font-family:var(--mono);font-size:18px;font-weight:700;padding:9px 11px;outline:none;transition:.2s}
.dinp:focus{border-color:var(--purple)}
.aps{display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap}
.ap{padding:3px 9px;border-radius:20px;background:var(--s1);border:1px solid var(--b1);font-size:9px;font-weight:700;cursor:pointer;color:var(--text2);font-family:var(--mono);transition:.15s}
.ap:hover{background:var(--s3);color:var(--text)}
.oprev{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r2);padding:10px 12px;margin-bottom:10px}
.oprev-t{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text3);margin-bottom:8px}
.opr{display:flex;justify-content:space-between;padding:2px 0;font-size:11px}
.opr .l{color:var(--text2)}.opr .v{font-weight:700;font-family:var(--mono)}
.opr.win{border-top:1px solid var(--b1);margin-top:5px;padding-top:7px}
.opr.win .l{font-weight:700;color:var(--text)}.opr.win .v{font-size:15px;color:var(--yes)}
.sub{width:100%;padding:12px;border-radius:var(--r2);border:none;cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:1px;transition:.18s}
.sub.buy{background:var(--yes);color:#001a0d}.sub.sell{background:var(--no);color:#fff}
.sub:hover{filter:brightness(1.08)}.sub:disabled{opacity:.4;pointer-events:none}
/* Market order */
.mkt-box{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r2);padding:10px 12px;margin-bottom:8px}
.mkt-box-t{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text3);margin-bottom:6px;display:flex;justify-content:space-between}
.mkt-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;font-size:11px}
.mkt-row:last-child{margin-bottom:0}
.mkt-price{font-family:var(--mono);font-size:13px;font-weight:700}
.mkt-btn{padding:9px;border-radius:var(--r2);border:none;cursor:pointer;font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:1px;transition:.18s;width:100%;margin-top:6px}
.mkt-btn.buy{background:var(--yes);color:#001a0d}.mkt-btn.sell{background:var(--no);color:#fff}
.mkt-btn:hover{filter:brightness(1.1)}.mkt-btn:disabled{opacity:.35;pointer-events:none}
.mkt-warn{font-size:9px;color:var(--gold);margin-top:5px;padding:4px 7px;background:var(--gold2);border-radius:4px}
.order-type-tabs{display:flex;gap:4px;margin-bottom:10px}
.ot-tab{flex:1;padding:5px 8px;border-radius:var(--r3);border:1px solid var(--b1);background:transparent;cursor:pointer;font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:1px;color:var(--text2);transition:.15s;text-align:center}
.ot-tab.active{background:var(--s3);color:#fff;border-color:var(--b2)}
.ot-tab:hover:not(.active){border-color:var(--b2);color:var(--text)}
.pos-panel,.ord-panel{background:var(--s2);border:1px solid var(--b1);border-radius:var(--r);padding:12px;margin-top:2px}
.pos-t{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text3);margin-bottom:8px}
.pos-row{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:11px;border-bottom:1px solid var(--b1);flex-wrap:wrap}
.pos-row:last-child{border:none}
.pos-side{padding:2px 7px;border-radius:20px;font-size:8px;font-weight:800;letter-spacing:1px;text-transform:uppercase;flex-shrink:0}
.pos-side.yes{background:var(--yes2);color:var(--yes)}.pos-side.no{background:var(--no2);color:var(--no)}
.ord-row{display:flex;align-items:center;gap:7px;padding:5px;background:var(--s1);border-radius:var(--r3);margin-bottom:4px;font-size:10px}
.ord-cancel{padding:2px 7px;border-radius:20px;background:transparent;border:1px solid var(--b2);color:var(--text2);cursor:pointer;font-size:9px;font-family:var(--mono);transition:.12s;margin-left:auto}
.ord-cancel:hover{border-color:var(--no);color:var(--no)}

/* ── PORTFOLIO ── */
.port-header{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:16px;gap:12px;flex-wrap:wrap}
.port-bal-block{}
.port-bal-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px}
.port-bal-val{font-family:var(--mono);font-size:30px;font-weight:700;color:var(--yes)}
/* Portfolio position cards */
.pcard{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);overflow:hidden;margin-bottom:10px}
.pcard-head{padding:12px 14px 10px;display:flex;align-items:flex-start;gap:10px;cursor:pointer}
.pcard-head:hover{background:var(--s2)}
.pcard-title{font-size:14px;font-weight:700;color:var(--text);flex:1;line-height:1.35}
.pcard-meta{display:flex;align-items:center;gap:6px;margin-top:3px;font-size:10px;color:var(--text3)}
.pcard-pnl{font-family:var(--mono);font-size:14px;font-weight:800;white-space:nowrap;text-align:right}
.pcard-pnl.up{color:var(--yes)}.pcard-pnl.dn{color:var(--no)}.pcard-pnl.flat{color:var(--text3)}
.pcard-body{padding:0 14px 12px;border-top:1px solid var(--b1)}
.ppos{display:grid;grid-template-columns:44px 1fr 1fr 1fr auto;gap:8px;align-items:center;padding:10px 0;border-bottom:1px solid var(--b1)}
.ppos:last-child{border:none}
.ppos-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.ppos-val{font-family:var(--mono);font-weight:700;font-size:13px;color:var(--text)}
.ppos-val.up{color:var(--yes)}.ppos-val.dn{color:var(--no)}.ppos-val.flat{color:var(--text3)}
.pord-row{display:flex;align-items:center;gap:8px;padding:7px 0;font-size:11px;border-bottom:1px solid var(--b1);color:var(--text2)}
.pord-row:last-child{border:none}
.port-empty-pos{color:var(--text3);font-size:10px;padding:8px 0;font-family:var(--mono)}
.port-total-bar{display:flex;align-items:center;gap:16px;padding:12px 16px;background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);margin-bottom:16px;flex-wrap:wrap}
.port-total-item{text-align:center}
.port-total-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px}
.port-total-val{font-family:var(--mono);font-size:18px;font-weight:800;color:var(--text)}
.port-stats{display:flex;gap:10px;flex-wrap:wrap}
.port-stat{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r2);padding:8px 14px;text-align:center}
.port-stat-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px}
.port-stat-val{font-family:var(--mono);font-size:15px;font-weight:700}
.pcard{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);padding:14px;margin-bottom:10px}
.pcard-t{font-size:13px;font-weight:700;margin-bottom:5px;line-height:1.35}
.pcard-m{display:flex;gap:8px;font-size:10px;color:var(--text3);font-family:var(--mono);margin-bottom:8px}

/* ── ACTIVITY ── */
.alist{display:flex;flex-direction:column;gap:5px}
.ai{display:flex;align-items:flex-start;gap:7px;padding:8px 11px;background:var(--s1);border:1px solid var(--b1);border-radius:var(--r3);font-size:11px;animation:fi .18s}
@keyframes fi{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}
.adot{width:5px;height:5px;border-radius:50%;flex-shrink:0;margin-top:4px}
.adot.trade{background:var(--yes)}.adot.order{background:var(--purple)}
.adot.resolve{background:var(--gold)}.adot.market{background:var(--text2)}.adot.user{background:var(--no)}
.ats{font-size:9px;color:var(--text3);margin-left:auto;font-family:var(--mono);white-space:nowrap;padding-left:6px}

/* ── CREATE FORM ── */
.cform{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);padding:20px;max-width:520px}
.fld{margin-bottom:12px}
.fld label{display:block;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text2);margin-bottom:4px}
.fld input,.fld textarea,.fld select{width:100%;background:var(--s2);border:1px solid var(--b2);border-radius:var(--r2);color:var(--text);font-family:var(--sans);font-size:13px;padding:8px 10px;outline:none;transition:.18s}
.fld input:focus,.fld textarea:focus,.fld select:focus{border-color:var(--purple)}
.fld textarea{resize:vertical;min-height:58px}.fld select option{background:var(--s2)}

/* ── CONFIG ── */
.cfgcard{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);padding:20px;max-width:420px}
.cfg-t{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:var(--text2);margin-bottom:14px}
/* Service balance card in config */
.svc-balance-card{background:var(--gold2);border:1px solid var(--gold3);border-radius:var(--r);padding:16px;margin-bottom:18px}
.svc-balance-card .lbl{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--gold);opacity:.7;margin-bottom:4px}
.svc-balance-card .val{font-family:var(--mono);font-size:32px;font-weight:700;color:var(--gold)}
.svc-balance-card .sub{font-size:10px;color:var(--gold);opacity:.6;margin-top:3px}

/* ── MISC ── */
.cbtn{padding:9px 20px;border-radius:var(--r2);border:none;cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:700;background:var(--purple);color:#fff;transition:.15s}
.cbtn:hover{filter:brightness(1.12)}
.stitle{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--text3);margin-bottom:14px}
.empty{text-align:center;padding:40px 20px;color:var(--text3);font-size:11px;font-family:var(--mono)}

/* ── TOASTS ── */
.toasts{position:fixed;bottom:16px;right:16px;z-index:999;display:flex;flex-direction:column;gap:5px;pointer-events:none}
.toast{padding:8px 13px;border-radius:var(--r2);font-size:11px;font-weight:700;max-width:320px;animation:ti .25s cubic-bezier(.34,1.56,.64,1);box-shadow:0 8px 30px rgba(0,0,0,.7);font-family:var(--mono)}
@keyframes ti{from{transform:translateX(40px);opacity:0}to{transform:none;opacity:1}}
.toast.ok{background:var(--yes);color:#001a0d}.toast.err{background:var(--no);color:#fff}.toast.info{background:var(--s3);color:var(--text);border:1px solid var(--b2)}

/* ── NO USER HINT ── */
.no-user-hint{background:var(--purple2);border:1px solid var(--purple3);border-radius:var(--r2);padding:10px 14px;font-size:11px;color:var(--purple);font-weight:600;margin-bottom:16px;display:none}
.no-user-hint.visible{display:block}
/* Simulation panel */
.sim-panel{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
@media(max-width:600px){.sim-panel{grid-template-columns:1fr}}
.sim-card{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);padding:14px}
.sim-card-t{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--text3);margin-bottom:10px}
.sim-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;font-size:11px}
.sim-row:last-child{margin-bottom:0}
.sim-inp{width:70px;background:var(--s2);border:1px solid var(--b2);border-radius:var(--r3);color:var(--text);font-family:var(--mono);font-size:12px;padding:4px 7px;outline:none;text-align:right}
.sim-inp:focus{border-color:var(--purple)}
.sim-toggle{display:flex;align-items:center;gap:8px}
.sim-switch{position:relative;width:36px;height:20px;flex-shrink:0}
.sim-switch input{opacity:0;width:0;height:0}
.sim-slider{position:absolute;cursor:pointer;inset:0;background:var(--b2);border-radius:20px;transition:.3s}
.sim-slider:before{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
.sim-switch input:checked+.sim-slider{background:var(--purple)}
.sim-switch input:checked+.sim-slider:before{transform:translateX(16px)}
.sim-status{display:flex;align-items:center;gap:6px;padding:8px 12px;background:var(--s2);border:1px solid var(--b1);border-radius:var(--r2);font-size:10px;font-family:var(--mono)}
.sim-dot{width:6px;height:6px;border-radius:50%;background:var(--text3);flex-shrink:0}
.sim-dot.running{background:var(--yes);box-shadow:0 0 6px var(--yes);animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.sim-log{font-size:10px;color:var(--text3);font-family:var(--mono);margin-top:8px;height:80px;overflow-y:auto;background:var(--s2);border-radius:var(--r3);padding:6px 8px;line-height:1.7}
.sim-log div{animation:fi .2s}
.sim-actions{display:flex;gap:8px;margin-top:10px}
.sim-btn-start{flex:1;padding:9px;border-radius:var(--r2);border:none;cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:700;background:var(--purple);color:#fff;transition:.15s}
.sim-btn-start:hover{filter:brightness(1.1)}
.sim-btn-stop{flex:1;padding:9px;border-radius:var(--r2);border:1px solid var(--no);background:transparent;cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:700;color:var(--no);transition:.15s}
.sim-btn-stop:hover{background:var(--no2)}
/* Position P&L */
.pos-pnl{font-family:var(--mono);font-size:10px;font-weight:700;margin-left:auto}
.pos-pnl.up{color:var(--yes)}.pos-pnl.dn{color:var(--no)}.pos-pnl.flat{color:var(--text3)}
.pos-sell-btn{padding:5px 12px;border-radius:var(--r2);border:1px solid var(--no);background:var(--no2);color:var(--no);cursor:pointer;font-size:11px;font-family:var(--mono);font-weight:700;transition:.15s;white-space:nowrap}
.pos-sell-btn:hover{background:var(--no);color:#fff}
/* Reset button */
.reset-btn{width:100%;margin-top:14px;padding:10px;border-radius:var(--r2);border:1px solid rgba(255,61,90,.3);background:rgba(255,61,90,.07);color:var(--no);cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:1px;transition:.18s}
.reset-btn:hover{background:rgba(255,61,90,.15);border-color:var(--no)}
/* Reset confirm modal */
.reset-modal{background:var(--s1);border:1px solid var(--no);border-radius:14px;width:100%;max-width:380px;margin:auto;animation:pop .2s cubic-bezier(.34,1.56,.64,1)}
.reset-modal-body{padding:24px}
.reset-modal-icon{font-size:32px;text-align:center;margin-bottom:12px}
.reset-modal-title{font-size:15px;font-weight:800;color:#fff;text-align:center;margin-bottom:6px}
.reset-modal-sub{font-size:11px;color:var(--text2);text-align:center;line-height:1.6;margin-bottom:20px}
.reset-modal-ul{font-size:10px;color:var(--text3);margin-bottom:20px;padding:10px 14px;background:var(--s2);border-radius:var(--r2);line-height:2}
.reset-modal-btns{display:flex;gap:8px}
.reset-cancel{flex:1;padding:10px;border-radius:var(--r2);border:1px solid var(--b2);background:transparent;color:var(--text2);cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:700;transition:.15s}
.reset-cancel:hover{background:var(--s3);color:var(--text)}
.reset-confirm{flex:1;padding:10px;border-radius:var(--r2);border:none;background:var(--no);color:#fff;cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:700;transition:.15s}
.reset-confirm:hover{filter:brightness(1.1)}
.reset-confirm:disabled{opacity:.5;pointer-events:none}
</style>
</head>
<body>
<div class="app">
<header>
  <div class="logo">
    BET<em>X</em><sup>2</sup>
    <small>Prediction Markets</small>
  </div>
  <div class="hsp"></div>
  <!-- Service balance chip -->
  <div class="svc-chip" title="Накопленные комиссии сервиса">
    <span class="svc-chip-icon">🏦</span>
    <span class="svc-chip-lbl">Сервис</span>
    <span class="svc-chip-val" id="svcBalHdr">$0.00</span>
  </div>
  <div class="wsdot" id="wsDot"></div>
  <!-- User pill -->
  <div class="upill" id="upill">
    <div class="av" id="hAv" style="background:#333">?</div>
    <span class="uname" id="hName">Выбрать</span>
    <span class="ubal" id="hBal"></span>
    <span class="chev">▾</span>
  </div>
</header>
<!-- Dropdown OUTSIDE upill to avoid event bubbling conflicts -->
<div class="dd" id="dd">
  <div class="ddsep">Участники</div>
  <div id="ddList"></div>
  <div class="ddnew" id="btnNewU">＋ Новый участник</div>
</div>

<div class="tabs">
  <div class="tab active" id="tab-markets"  >Рынки</div>
  <div class="tab"        id="tab-portfolio">Портфель</div>
  <div class="tab"        id="tab-activity" >Лента</div>
  <div class="tab"        id="tab-simulate" >🤖 Симуляция</div>
  <div class="tab"        id="tab-create"   >＋ Создать</div>
  <div class="tab"        id="tab-config"   >⚙ Настройки</div>
</div>

<!-- MARKETS -->
<div class="panel active" id="panel-markets">
  <div class="no-user-hint" id="noUserHint">👆 Выберите участника в правом верхнем углу, чтобы торговать</div>
  <div class="mgrid" id="mgrid"><div class="empty">Загрузка…</div></div>
</div>

<!-- PORTFOLIO -->
<div class="panel" id="panel-portfolio">
  <div id="portContent"><div class="empty">Выберите участника</div></div>
</div>

<!-- ACTIVITY -->
<div class="panel" id="panel-activity">
  <div class="stitle">Лента сделок</div>
  <div class="alist" id="alist"><div class="empty">Нет событий</div></div>
</div>

<!-- SIMULATE -->
<div class="panel" id="panel-simulate">
  <div class="stitle">🤖 Автоматическая симуляция торговли</div>
  <div class="sim-status" id="simStatus">
    <div class="sim-dot" id="simDot"></div>
    <span id="simStatusTxt">Симуляция остановлена</span>
    <span style="margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--text3)" id="simTick"></span>
  </div>

  <div class="sim-panel" style="margin-top:12px">
    <!-- LEFT: parameters -->
    <div class="sim-card">
      <div class="sim-card-t">⚙ Параметры</div>
      <div class="sim-row">
        <span style="color:var(--text2)">Рынок</span>
        <select id="simMarketSel" class="sim-inp" style="width:150px">
          <option value="ALL">🌐 Все открытые</option>
        </select>
      </div>
      <div class="sim-row">
        <span style="color:var(--text2)">Интервал (сек)</span>
        <input type="number" class="sim-inp" id="simInterval" value="1.5" min="0.3" max="10" step="0.1">
      </div>
      <div class="sim-row">
        <span style="color:var(--text2)">Размер ордера ($)</span>
        <input type="number" class="sim-inp" id="simOrderSize" value="80" min="5" max="1000">
      </div>
      <div class="sim-row">
        <span style="color:var(--text2)">Агрессивность</span>
        <select id="simAggression" class="sim-inp">
          <option value="passive">Пассивный (лимит)</option>
          <option value="mixed" selected>Смешанный</option>
          <option value="aggressive">Агрессивный (маркет)</option>
        </select>
      </div>
      <div class="sim-row">
        <span style="color:var(--text2)">Разброс цены (¢)</span>
        <input type="number" class="sim-inp" id="simSpread" value="4" min="1" max="20">
      </div>
      <div class="sim-row">
        <span style="color:var(--text2)">Дрейф цены</span>
        <div class="sim-toggle">
          <label class="sim-switch"><input type="checkbox" id="simDrift" checked><span class="sim-slider"></span></label>
          <span style="font-size:10px;color:var(--text3)" id="simCenterLbl"></span>
        </div>
      </div>
      <div class="sim-row" id="simCenterRow">
        <span style="color:var(--text2)">Целевая цена (¢)</span>
        <input type="number" class="sim-inp" id="simCenterPrice" value="50" min="5" max="95" step="1">
      </div>
    </div>
    <!-- RIGHT: bots -->
    <div class="sim-card">
      <div class="sim-card-t">👥 Боты-участники</div>
      <div id="simBotList" style="font-size:11px;color:var(--text3);margin-bottom:8px;max-height:160px;overflow-y:auto">Загрузка…</div>
      <div style="border-top:1px solid var(--b1);padding-top:8px;margin-top:4px">
        <div class="sim-row">
          <span style="color:var(--text2);font-size:10px">Баланс бота ($)</span>
          <input type="number" class="sim-inp" id="simBotBal" value="5000" min="500">
        </div>
        <button class="cbtn" id="btnAddBot" style="width:100%;margin-top:8px;font-size:9px;padding:7px">＋ Добавить бота</button>
      </div>
      <!-- sim stats -->
      <div style="border-top:1px solid var(--b1);padding-top:8px;margin-top:8px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;text-align:center">
        <div><div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px">Тиков</div><div style="font-family:var(--mono);font-weight:700;font-size:14px" id="statTicks">0</div></div>
        <div><div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px">Сделок</div><div style="font-family:var(--mono);font-weight:700;font-size:14px;color:var(--yes)" id="statTrades">0</div></div>
        <div><div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px">Объём</div><div style="font-family:var(--mono);font-weight:700;font-size:14px;color:var(--purple)" id="statVol">$0</div></div>
      </div>
    </div>
  </div>

  <div class="sim-actions">
    <button class="sim-btn-start" id="btnSimStart">▶ Запустить</button>
    <button class="sim-btn-stop"  id="btnSimStop" style="display:none">■ Стоп</button>
    <button class="cbtn" id="btnSimBurst" style="font-size:9px;padding:8px 14px;background:var(--gold);color:#000">⚡ Burst x10</button>
  </div>
  <div class="sim-log" id="simLog"><div style="color:var(--text3)">Лог симуляции появится здесь…</div></div>
</div>

<!-- CREATE -->
<div class="panel" id="panel-create">
  <div class="stitle">Новый рынок</div>
  <div class="cform">
    <div class="fld"><label>Вопрос</label><input type="text" id="nTitle" placeholder="Произойдёт ли X до Y?" maxlength="200"></div>
    <div class="fld"><label>Условие разрешения</label><textarea id="nDesc" placeholder="Когда считается что событие произошло…"></textarea></div>
    <div class="fld"><label>Категория</label>
      <select id="nCat">
        <option>Спорт</option><option>Крипто</option><option>Политика</option>
        <option>Технологии</option><option>Финансы</option><option>Другое</option>
      </select>
    </div>
    <button class="cbtn" id="btnCreate">Создать рынок</button>
  </div>
</div>

<!-- CONFIG -->
<div class="panel" id="panel-config">
  <div class="cfgcard">
    <div class="svc-balance-card">
      <div class="lbl">💰 Баланс сервиса</div>
      <div class="val" id="svcBalCfg">$0.00</div>
      <div class="sub">Накопленные комиссии с закрытых рынков</div>
    </div>
    <div class="cfg-t">⚙ Настройки</div>
    <div class="fld"><label>Комиссия с выигрыша (%)</label>
      <input type="number" id="cfgComm" min="0" max="20" step="0.1" value="2" style="width:120px">
    </div>
    <button class="cbtn" id="btnSaveCfg">Сохранить</button>
    <div style="margin-top:18px;font-size:11px;color:var(--text3);line-height:1.9;border-top:1px solid var(--b1);padding-top:14px">
      <b style="color:var(--text2);font-size:10px;text-transform:uppercase;letter-spacing:1px">Механика рынка</b><br><br>
      • Каждый контракт YES стоит <b style="color:var(--text)">$1</b> при победе, $0 при поражении<br>
      • Цена = вероятность (<b style="color:var(--text)">1–99¢</b>)<br>
      • <b style="color:var(--yes)">BUY YES @ 30¢</b>: платишь $30, при победе получаешь $100<br>
      • <b style="color:var(--no)">SELL YES @ 30¢</b> (= BUY NO @ 70¢): платишь $70, при НЕТ получаешь $100<br>
      • Сделка: BUY 30¢ + SELL 30¢ = $100 пот → победитель забирает всё<br>
      • Комиссия сервиса берётся с выигрыша при резолве рынка
    </div>
    <button class="reset-btn" id="btnReset">⚠ Сбросить все данные</button>
  </div>
</div>

<!-- RESET CONFIRM MODAL -->
<div class="ovl" id="resetOvl">
<div class="reset-modal">
  <div class="reset-modal-body">
    <div class="reset-modal-icon">⚠️</div>
    <div class="reset-modal-title">Сбросить все данные?</div>
    <div class="reset-modal-sub">Это действие нельзя отменить.<br>Все данные будут удалены и пересозданы.</div>
    <div class="reset-modal-ul">
      🗑 Все участники и их балансы<br>
      🗑 Все рынки и ордера<br>
      🗑 Вся история сделок<br>
      🗑 Баланс сервиса<br>
      ✅ Стартовые данные будут пересозданы
    </div>
    <div class="reset-modal-btns">
      <button class="reset-cancel" id="btnResetCancel">Отмена</button>
      <button class="reset-confirm" id="btnResetConfirm">🔄 Сбросить</button>
    </div>
  </div>
</div>
</div>
</div>

<!-- MARKET DETAIL MODAL -->
<div class="ovl" id="mktOvl">
<div class="mdl">
  <div class="mdl-head">
    <div>
      <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px" id="dCat"></div>
      <div class="mdl-title" id="dTitle"></div>
      <div style="font-size:11px;color:var(--text2);margin-top:4px" id="dDesc"></div>
    </div>
    <button class="mcls" id="btnClose">✕</button>
  </div>
  <div class="mdl-body">
    <!-- LEFT: Orderbook + Trades -->
    <div class="ob-wrap">
      <div>
        <div class="stitle">Ордербук</div>
        <div class="ob" id="obWidget">
          <div class="ob-hdr">
            <span>ЦЕНА</span><span style="text-align:right">КОНТ.</span><span style="text-align:right">ОБЪЁМ $</span><span style="text-align:right">ИТОГО $</span>
          </div>
          <div class="ob-asks-wrap"><div id="obAsks"></div></div>
          <div class="ob-mid" id="obMid">
            <span class="lp" id="obMidLp">—</span>
            <span class="spread" id="obMidSpread"></span>
            <span class="ob-expand" id="obExpand">▼ 20 уровней</span>
          </div>
          <div class="ob-bids-wrap"><div id="obBids"></div></div>
        </div>
      </div>
      <div>
        <div class="stitle">История сделок</div>
        <div class="trades-wrap">
          <div class="trades-hdr">
            <span>ЦЕНА</span><span>КОЛ-ВО</span><span>ПОКУПАТЕЛЬ / ПРОДАВЕЦ</span><span style="text-align:right">ВРЕМЯ</span>
          </div>
          <div id="tradesList"><div class="empty" style="padding:14px">Нет сделок</div></div>
        </div>
      </div>
    </div>
    <!-- RIGHT: Order form -->
    <div class="form-wrap">
      <div id="dResStrip"></div>
      <div class="oform" id="orderForm">
        <!-- Direction: YES / NO -->
        <div class="of-tabs">
          <button class="of-tab buy active" id="ofBuy">▲ КУПИТЬ YES</button>
          <button class="of-tab sell"       id="ofSell">▼ ПРОДАТЬ YES (NO)</button>
        </div>
        <!-- Order type: Limit / Market -->
        <div class="order-type-tabs">
          <div class="ot-tab active" id="otLimit">Лимит</div>
          <div class="ot-tab" id="otMarket">По рынку</div>
        </div>

        <!-- LIMIT form -->
        <div id="limitForm">
          <div class="frow">
            <div class="flbl">Вероятность / цена</div>
            <div class="price-row">
              <input type="number" class="price-inp buy" id="priceInp" min="0.1" max="99.9" step="0.1" value="50">
              <div style="display:flex;flex-direction:column;gap:2px">
                <div style="font-size:9px;color:var(--text3);font-family:var(--mono)">ЦЕНА YES</div>
                <div class="price-unit">¢&nbsp;<span id="oddsDisp" style="color:var(--text2);font-size:11px">2.00×</span></div>
              </div>
            </div>
            <!-- Secondary price display for SELL: shows what you actually pay (NO price) -->
            <div id="noPriceRow" style="display:none;margin-top:6px;padding:6px 10px;background:var(--no2);border:1px solid var(--no3);border-radius:var(--r3);font-size:11px;font-family:var(--mono)">
              <span style="color:var(--text3)">Твоя цена NO: </span>
              <span id="noPriceVal" style="color:var(--no);font-weight:700;font-size:14px">50¢</span>
              <span style="color:var(--text3)"> → коэф </span>
              <span id="noOddsVal" style="color:var(--no);font-weight:700">2.00×</span>
              <span style="color:var(--text3);font-size:9px;display:block;margin-top:2px">платишь 100 − цена YES = NO цена за контракт</span>
            </div>
            <input type="range" class="pslider buy" id="priceSlider" min="1" max="99" step="1" value="50" style="width:100%;margin-top:6px">
            <div class="pmarkers">
              <span>1¢</span><span>25¢</span><span>50¢</span><span>75¢</span><span>99¢</span>
            </div>
            <div style="font-size:10px;color:var(--text3);margin-top:4px" id="priceHint"></div>
          </div>
          <div class="frow">
            <div class="flbl">Сумма (моя ставка)</div>
            <div class="dinp-wrap"><span class="dollar">$</span><input class="dinp" type="number" id="dollarInp" value="100" min="1"></div>
            <div class="aps">
              <button class="ap" data-v="50">$50</button>
              <button class="ap" data-v="100">$100</button>
              <button class="ap" data-v="200">$200</button>
              <button class="ap" data-v="500">$500</button>
              <button class="ap" data-v="1000">$1k</button>
              <button class="ap" id="apMax">MAX</button>
            </div>
          </div>
          <div class="oprev">
            <div class="oprev-t">Твоя ставка</div>
            <div class="opr"><span class="l">Тип</span><span class="v" id="pvType" style="color:var(--yes)">BUY YES</span></div>
            <div class="opr"><span class="l" id="pvPriceLbl">Цена за контракт</span><span class="v" id="pvPrice">—</span></div>
            <div class="opr"><span class="l">Контрактов</span><span class="v" id="pvContracts">—</span></div>
            <div class="opr"><span class="l">Вложено</span><span class="v" id="pvCost">—</span></div>
            <div class="opr win"><span class="l">🏆 При победе (после комиссии)</span><span class="v" id="pvWin">—</span></div>
            <div class="opr"><span class="l" style="font-size:9px;color:var(--text3)">Чистая прибыль</span><span class="v" id="pvProfit" style="font-size:11px">—</span></div>
            <!-- Counterparty info box -->
            <div id="pvCounterparty" style="margin-top:8px;padding:7px 10px;background:var(--s2);border:1px solid var(--b1);border-radius:var(--r3);font-size:10px;color:var(--text3);line-height:1.8;display:none">
            </div>
          </div>
          <button class="sub buy" id="subBtn">Разместить ордер BUY</button>
        </div>

        <!-- MARKET form -->
        <div id="marketForm" style="display:none">
          <div class="mkt-box">
            <div class="mkt-box-t"><span>Лучшие цены в стакане</span><span id="mktSpread" style="color:var(--text3)"></span></div>
            <div class="mkt-row">
              <span style="color:var(--yes)">▲ Купить YES (лучший ASK)</span>
              <span class="mkt-price" id="mktBestAsk" style="color:var(--yes)">—</span>
            </div>
            <div class="mkt-row">
              <span style="color:var(--no)">▼ Продать YES (лучший BID)</span>
              <span class="mkt-price" id="mktBestBid" style="color:var(--no)">—</span>
            </div>
          </div>
          <div class="frow" style="margin-bottom:10px">
            <div class="flbl">Сумма (моя ставка)</div>
            <div class="dinp-wrap"><span class="dollar">$</span><input class="dinp" type="number" id="mktDollarInp" value="100" min="1"></div>
            <div class="aps">
              <button class="ap mkt-ap" data-v="50">$50</button>
              <button class="ap mkt-ap" data-v="100">$100</button>
              <button class="ap mkt-ap" data-v="200">$200</button>
              <button class="ap mkt-ap" data-v="500">$500</button>
              <button class="ap" id="mktApMax">MAX</button>
            </div>
          </div>
          <!-- Market preview -->
          <div class="oprev" id="mktPreview">
            <div class="oprev-t">Исполнение по рынку</div>
            <div class="opr"><span class="l">Цена исполнения</span><span class="v" id="mktPvPrice" style="color:var(--yes)">—</span></div>
            <div class="opr"><span class="l">Коэффициент</span><span class="v" id="mktPvOdds">—</span></div>
            <div class="opr"><span class="l">Контрактов</span><span class="v" id="mktPvContracts">—</span></div>
            <div class="opr"><span class="l">Доступно в стакане ($)</span><span class="v" id="mktPvAvail">—</span></div>
            <div class="opr win"><span class="l">🏆 При победе (после комиссии)</span><span class="v" id="mktPvWin">—</span></div>
            <div class="opr"><span class="l" style="font-size:9px;color:var(--text3)">Чистая прибыль</span><span class="v" id="mktPvProfit" style="font-size:11px">—</span></div>
          </div>
          <div id="mktWarn"></div>
          <button class="mkt-btn buy" id="mktBtn">⚡ Купить по рынку</button>
        </div>
      </div>
      <div class="pos-panel" id="myPosPanel">
        <div class="pos-t">Мои позиции</div>
        <div id="myPosList"><div style="font-size:10px;color:var(--text3)">Нет позиций</div></div>
      </div>
      <div class="ord-panel" id="myOrdPanel">
        <div class="pos-t">Мои открытые ордера</div>
        <div id="myOrdList"><div style="font-size:10px;color:var(--text3)">Нет ордеров</div></div>
      </div>
    </div>
  </div>
</div>
</div>

<!-- USER MODAL -->
<div class="ovl" id="usrOvl">
<div class="mdl" style="max-width:380px;margin:auto">
  <div class="mdl-head">
    <div class="mdl-title">Новый участник</div>
    <button class="mcls" id="btnCloseU">✕</button>
  </div>
  <div style="padding:16px 20px">
    <div class="fld"><label>Имя</label><input type="text" id="nuName" placeholder="Имя" maxlength="24"></div>
    <div class="fld"><label>Начальный баланс ($)</label><input type="number" id="nuBal" value="5000" min="100"></div>
    <button class="cbtn" style="width:100%" id="btnCrtU">Создать</button>
  </div>
</div>
</div>

<div class="toasts" id="toasts"></div>

<script>
var mkts={}, usrs={}, cfg={commission_pct:.02}, curU=null, curMid=null, curSide='BUY', actLog=[];
var svcBalance=0;

// ── Helpers ───────────────────────────────────────────────────────────
function fmtBal(v){ return '$'+(v||0).toFixed(2); }
function fmtVol(v){
  if(v>=1000000) return '$'+(v/1000000).toFixed(1)+'M';
  if(v>=1000)    return '$'+(v/1000).toFixed(1)+'k';
  return '$'+Math.round(v||0);
}
function updSvcBalance(v){
  svcBalance=v||0;
  var s=fmtBal(svcBalance);
  document.getElementById('svcBalHdr').textContent=s;
  document.getElementById('svcBalCfg').textContent=s;
}

// ── API ───────────────────────────────────────────────────────────────
async function api(p,method,body){
  var r=await fetch(p,{method:method||'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  var d=await r.json();
  if(!r.ok) throw new Error(d.detail||'Ошибка');
  return d;
}

// ── WebSocket ─────────────────────────────────────────────────────────
function connect(){
  var ws;
  try{ ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws'); }
  catch(e){ loadHttp(); return; }
  ws.onopen=function(){ document.getElementById('wsDot').className='wsdot on'; };
  ws.onclose=function(){ document.getElementById('wsDot').className='wsdot'; setTimeout(connect,2000); };
  ws.onerror=function(){ loadHttp(); };
  ws.onmessage=function(e){
    var m=JSON.parse(e.data);
    if(m.type==='init'){ applyState(m); return; }
    if(m.type==='markets'){ (m.data||[]).forEach(function(mk){ mkts[mk.id]=mk; }); renderMarkets(); updateSimMarketSel(); if(curMid&&mkts[curMid]){ renderOB(mkts[curMid]); if(curOrderType==='market') updMktPreview(); } }
    if(m.type==='users'){ (m.data||[]).forEach(function(u){ usrs[u.id]=u; }); renderHdr(); renderDD(); renderPort(); updateSimBotList(); }
    if(m.type==='activity'){ actLog.unshift(m.data); prependAct(m.data); }
    if(m.type==='reset'){
      stopSim(); mkts={}; usrs={}; curU=null; curMid=null; actLog=[];
      (m.users||[]).forEach(function(u){ usrs[u.id]=u; });
      (m.markets||[]).forEach(function(mk){ mkts[mk.id]=mk; });
      actLog=m.activity||[];
      updSvcBalance(m.service_balance||0);
      renderMarkets(); renderHdr(); renderDD(); renderAct();
      document.getElementById('portContent').innerHTML='<div class="empty">Выберите участника</div>';
      prevOBPrices={bids:{},asks:{}};
      toast('🔄 Данные сброшены','info');
    }
    if(m.type==='config'){ cfg=m.data; document.getElementById('cfgComm').value=(cfg.commission_pct*100).toFixed(1); }
    if(m.type==='service_balance'){ updSvcBalance(m.data); }
  };
}
async function loadHttp(){ try{ applyState(await api('/api/state')); }catch(e){} }
function applyState(d){
  (d.users||[]).forEach(function(u){ usrs[u.id]=u; });
  (d.markets||[]).forEach(function(mk){ mkts[mk.id]=mk; });
  actLog=d.activity||[]; if(d.config) cfg=d.config;
  document.getElementById('cfgComm').value=(cfg.commission_pct*100).toFixed(1);
  updSvcBalance(d.service_balance||0);
  renderMarkets(); renderHdr(); renderDD(); renderAct();
}

// ── Market grid ───────────────────────────────────────────────────────
function calcOdds(p){ return p>0&&p<100?(100/p).toFixed(2)+'×':null; }

function renderMarkets(){
  var g=document.getElementById('mgrid'), list=Object.values(mkts);
  document.getElementById('noUserHint').className='no-user-hint'+(curU?'':' visible');
  if(!list.length){ g.innerHTML='<div class="empty">Нет рынков</div>'; return; }
  g.innerHTML='';
  list.forEach(function(m){
    var isR=m.status==='resolved';
    var bCls=isR?(m.outcome==='YES'?'ry':'rn'):'open';
    var bTxt=isR?(m.outcome==='YES'?'✓ ДА':'✗ НЕТ'):'Открыто';
    var lp_num=m.last_price!=null?m.last_price:null;  // only real trades, no mid_price

    // Коэффициенты
    var lpOdds = lp_num!=null ? calcOdds(lp_num) : null;           // на YES
    var lpOddsNo = lp_num!=null ? calcOdds(100-lp_num) : null;     // на NO

    // BID = лучший покупатель YES, ASK = лучший продавец YES
    // Для пользователя:
    //   Купить YES → исполнится по лучшему ASK → коэф 100/askP
    //   Купить NO  → исполнится по лучшему BID → коэф 100/(100-bidP)
    var bidP = m.best_bid;   // highest buyer of YES
    var askP = m.best_ask;   // lowest seller of YES

    // "Купить YES" — ты берёшь лучший ASK
    var buyYesOdds = askP!=null ? calcOdds(askP) : null;
    // "Купить NO"  — ты берёшь лучший BID (продаёшь YES, платишь 100-bidP за NO)
    var buyNoOdds  = bidP!=null ? calcOdds(100-bidP) : null;
    var buyNoPrice = bidP!=null ? Math.round((100-bidP)*10)/10 : null;

    var bidHtml = askP!=null
      ? '<div class="mc-ba-val">'+askP+'¢ YES</div><div class="mc-ba-odds">'+buyYesOdds+'</div>'
      : '<div class="mc-ba-val">—</div>';
    var askHtml = bidP!=null
      ? '<div class="mc-ba-val">'+buyNoPrice+'¢ NO</div><div class="mc-ba-odds">'+buyNoOdds+'</div>'
      : '<div class="mc-ba-val">—</div>';

    var d=document.createElement('div');
    d.className='mcard'+(isR?' resolved':'');
    d.innerHTML=
      '<div class="mcard-head">'+
        '<div class="mc-top"><div class="cat">'+m.category+'</div><div class="badge '+bCls+'">'+bTxt+'</div></div>'+
        '<div class="mc-title">'+m.title+'</div>'+
        '<div class="mc-price">'+
          '<div class="mc-pval">'+(lp_num!=null?lp_num:'—')+'</div>'+
          '<div style="display:flex;flex-direction:column;gap:2px">'+
            '<div class="mc-punit">¢</div>'+
            '<div style="font-size:9px;color:var(--text3);white-space:nowrap">'+
              (lp_num!=null?'последняя сделка':'нет сделок')+
            '</div>'+
            (m.spread!=null&&m.spread>0?'<div style="font-size:9px;color:var(--gold)">спред '+m.spread+'¢</div>':''+
            (m.best_bid&&m.best_ask&&m.best_bid===m.best_ask?'<div style="font-size:9px;color:var(--text3)">спред 0¢ (равновесие)</div>':'')+
            '')+
          '</div>'+
        '</div>'+
        '<div class="mc-ba">'+
          '<div class="mc-bid"><div class="mc-ba-lbl">▲ КУПИТЬ YES</div>'+bidHtml+'</div>'+
          '<div class="mc-ask"><div class="mc-ba-lbl">▼ КУПИТЬ NO</div>'+askHtml+'</div>'+
        '</div>'+
      '</div>'+
      '<div class="mc-foot"><span>'+m.trade_count+' сделок · '+fmtVol(m.volume)+'</span><span>'+m.created_at+'</span></div>';
    if(!isR){
      d.addEventListener('click',function(){ openMarket(m.id); });
    }
    g.appendChild(d);
  });
}

// ── Market detail ─────────────────────────────────────────────────────
function openMarket(mid){
  if(!curU){ toast('Выберите участника','err'); return; }
  curMid=mid;
  document.getElementById('mktOvl').classList.add('open');
  selSide('BUY');          // always reset to BUY on open
  setOrderType('limit');   // always reset to limit tab
  refreshDetail();
}

async function refreshDetail(){
  if(!curMid) return;
  var m=mkts[curMid];
  document.getElementById('dCat').textContent=m.category;
  document.getElementById('dTitle').textContent=m.title;
  document.getElementById('dDesc').textContent=m.desc||'';
  var isR=m.status==='resolved';
  document.getElementById('orderForm').style.display=isR?'none':'block';
  var strip=document.getElementById('dResStrip');
  strip.innerHTML='';
  if(isR){
    strip.innerHTML='<div class="res-banner '+(m.outcome||'').toLowerCase()+'">'+(m.outcome==='YES'?'✓ ПРОИЗОШЛО — YES WIN':'✗ НЕ ПРОИЗОШЛО — NO WIN')+'</div>';
  } else {
    strip.innerHTML='<div class="res-strip"><p>⚡ Разрешить рынок</p>'+
      '<button class="rbtn y" id="rY">YES</button><button class="rbtn n" id="rN">NO</button></div>';
    document.getElementById('rY').addEventListener('click',function(){ doResolve('YES'); });
    document.getElementById('rN').addEventListener('click',function(){ doResolve('NO'); });
  }
  renderOB(m);
  await loadTrades();
  await loadMyPositions();
  updPreview();
  if(curOrderType==='market') updMktPreview();
}

var obLevels = 10; // 10 or 20
var prevOBPrices = {bids:{}, asks:{}}; // for flash detection

function renderOB(m){
  var ob=m.orderbook||{bids:[],asks:[]};
  var asksEl=document.getElementById('obAsks');
  var bidsEl=document.getElementById('obBids');

  var lim = obLevels;

  // --- Max dollar for depth bar scaling ---
  var allDollars = ob.asks.concat(ob.bids).map(function(r){ return r.dollars; });
  var maxD = allDollars.length ? Math.max.apply(null,allDollars) : 1;

  // Asks: cumulative from bottom (lowest ask = nearest mid)
  var asksSlice = ob.asks.slice(0, lim);        // ascending price
  var askCumul = 0;
  var asksWithCumul = asksSlice.map(function(r){
    askCumul = Math.round((askCumul + r.dollars)*100)/100;
    return {price:r.price, contracts:r.contracts, dollars:r.dollars, cumul:askCumul};
  });
  var asksReversed = [...asksWithCumul].reverse(); // display high→low price

  // Bids: cumulative from top (highest bid = nearest mid)
  var bidsSlice = ob.bids.slice(0, lim);        // descending price
  var bidCumul = 0;
  var bidsWithCumul = bidsSlice.map(function(r){
    bidCumul = Math.round((bidCumul + r.dollars)*100)/100;
    return {price:r.price, contracts:r.contracts, dollars:r.dollars, cumul:bidCumul};
  });

  function makeRow(r, side){
    var cls = side==='ask' ? 'ob-row ask-row' : 'ob-row bid-row';
    var fn  = side==='ask' ? 'BUY' : 'SELL';  // click ASK → you BUY; click BID → you SELL
    var barW = Math.round(r.dollars/maxD*100);
    // detect new/changed price level for flash
    var flashCls = '';
    var prev = side==='ask' ? prevOBPrices.asks[r.price] : prevOBPrices.bids[r.price];
    if(prev !== undefined && Math.abs(prev - r.dollars) > 0.01){
      flashCls = side==='ask' ? ' flash-sell' : ' flash-buy';
    }
    return '<div class="'+cls+flashCls+'" onclick="clickOBRow(\''+fn+'\','+r.price+')" data-price="'+r.price+'">'+
      '<span class="depth-bar" style="width:'+barW+'%"></span>'+
      '<span class="p">'+r.price+'¢</span>'+
      '<span style="text-align:right">'+r.contracts.toFixed(1)+'</span>'+
      '<span style="text-align:right">$'+r.dollars.toFixed(0)+'</span>'+
      '<span style="text-align:right;color:var(--text3)">$'+r.cumul.toFixed(0)+'</span>'+
    '</div>';
  }

  // Add column header for asks: clicking = BUY
  asksEl.innerHTML = asksReversed.length
    ? '<div style="padding:3px 12px;font-size:8px;color:var(--ask);font-family:var(--mono);font-weight:700;letter-spacing:1px;opacity:.7">▲ ПРОДАВЦЫ YES — клик чтобы КУПИТЬ</div>'+
      asksReversed.map(function(r){ return makeRow(r,'ask'); }).join('')
    : '<div style="padding:8px 12px;font-size:10px;color:var(--text3)">Нет продавцов YES</div>';

  bidsEl.innerHTML = bidsWithCumul.length
    ? bidsWithCumul.map(function(r){ return makeRow(r,'bid'); }).join('')+
      '<div style="padding:3px 12px;font-size:8px;color:var(--bid);font-family:var(--mono);font-weight:700;letter-spacing:1px;opacity:.7">▼ ПОКУПАТЕЛИ YES — клик чтобы ПРОДАТЬ</div>'
    : '<div style="padding:8px 12px;font-size:10px;color:var(--text3)">Нет покупателей YES</div>';

  // Update mid strip
  var midLp = document.getElementById('obMidLp');
  var midSp = document.getElementById('obMidSpread');
  var lp=m.last_price;
  var sp=m.spread!=null?' спред: '+m.spread+'¢':'';
  if(lp!=null){
    midLp.textContent=lp+'¢'; midSp.textContent='посл.'+sp;
  } else if(m.best_bid||m.best_ask){
    var mp=m.best_bid&&m.best_ask?Math.round((m.best_bid+m.best_ask)/2*10)/10:null;
    midLp.textContent=mp!=null?'~'+mp+'¢':'—';
    midSp.textContent=mp!=null?'mid'+sp:'нет сделок';
  } else {
    midLp.textContent='—'; midSp.textContent='нет сделок';
  }

  // Save current state for next flash comparison
  prevOBPrices.asks={};
  prevOBPrices.bids={};
  ob.asks.forEach(function(r){ prevOBPrices.asks[r.price]=r.dollars; });
  ob.bids.forEach(function(r){ prevOBPrices.bids[r.price]=r.dollars; });

  // Update expand button label
  var expBtn = document.getElementById('obExpand');
  if(expBtn) expBtn.textContent = obLevels===10 ? '▼ 20 уровней' : '▲ 10 уровней';
}

function clickOBRow(side,price){
  selSide(side);
  document.getElementById('priceInp').value=price;
  document.getElementById('priceSlider').value=Math.round(price);
  onPriceChange();
}

async function loadTrades(){
  try{
    var ts=await api('/api/trades/'+curMid);
    var el=document.getElementById('tradesList');
    if(!ts.length){ el.innerHTML='<div class="empty" style="padding:12px">Нет сделок</div>'; return; }
    el.innerHTML=ts.map(function(t){
      return '<div class="tr-row">'+
        '<span class="tr-price">'+t.price+'¢</span>'+
        '<span class="tr-qty">×'+t.contracts.toFixed(1)+'</span>'+
        '<span class="tr-names"><span style="color:var(--yes)">'+t.buy_user_name+'</span>'+
          '<span style="color:var(--text3)"> vs </span>'+
          '<span style="color:var(--no)">'+t.sell_user_name+'</span></span>'+
        '<span class="tr-ts">'+t.created_at+'</span>'+
      '</div>';
    }).join('');
  }catch(e){}
}

async function loadMyPositions(){
  if(!curU||!curMid) return;
  try{
    var p=await api('/api/portfolio/'+curU);
    var pos=p.positions.find(function(x){ return x.market&&x.market.id===curMid; });
    var posList=document.getElementById('myPosList');
    var m=mkts[curMid];
    var curPrice=m?(m.last_price||m.mid_price):null;
    if(!pos||(!pos.yes_contracts&&!pos.no_contracts)){
      posList.innerHTML='<div style="font-size:10px;color:var(--text3)">Нет позиций</div>';
    } else {
      var html='';
      if(pos.yes_contracts>0){
        var avgEntry=pos.yes_cost/pos.yes_contracts*100; // cents
        var curVal=curPrice!=null?pos.yes_contracts*curPrice/100:null;
        var pnl=curVal!=null?Math.round((curVal-pos.yes_cost)*100)/100:null;
        var pnlCls=pnl==null?'flat':pnl>0?'up':'dn';
        var pnlTxt=pnl!=null?(pnl>=0?'+':'')+pnl.toFixed(2)+'$':'—';
        var bidForSell=m&&m.best_bid?m.best_bid:null; // sell YES = fill BID
        html+='<div class="pos-row">'+
          '<span class="pos-side yes">YES</span>'+
          '<span>'+pos.yes_contracts.toFixed(2)+'c</span>'+
          '<span style="color:var(--text3);font-size:10px">вход ~'+Math.round(avgEntry)+'¢</span>'+
          '<span class="pos-pnl '+pnlCls+'">'+pnlTxt+'</span>'+
          (bidForSell&&m.status==='open'?'<button class="pos-sell-btn" onclick="quickSell(1,'+bidForSell+','+pos.yes_contracts+')">Продать @ '+bidForSell+'¢</button>':'')+
        '</div>';
      }
      if(pos.no_contracts>0){
        var avgEntryNo=pos.no_cost/pos.no_contracts*100; // NO cents = 100-yesPrice
        var curPriceNo=curPrice!=null?100-curPrice:null;
        var curValNo=curPriceNo!=null?pos.no_contracts*curPriceNo/100:null;
        var pnlNo=curValNo!=null?Math.round((curValNo-pos.no_cost)*100)/100:null;
        var pnlClsNo=pnlNo==null?'flat':pnlNo>0?'up':'dn';
        var pnlTxtNo=pnlNo!=null?(pnlNo>=0?'+':'')+pnlNo.toFixed(2)+'$':'—';
        var askForSell=m&&m.best_ask?m.best_ask:null; // buy back YES = fill ASK to close NO
        html+='<div class="pos-row">'+
          '<span class="pos-side no">NO</span>'+
          '<span>'+pos.no_contracts.toFixed(2)+'c</span>'+
          '<span style="color:var(--text3);font-size:10px">вход ~'+Math.round(avgEntryNo)+'¢NO</span>'+
          '<span class="pos-pnl '+pnlClsNo+'">'+pnlTxtNo+'</span>'+
          (askForSell&&m.status==='open'?'<button class="pos-sell-btn" onclick="quickSell(2,'+askForSell+','+pos.no_contracts+')">Закрыть @ '+askForSell+'¢</button>':'')+
        '</div>';
      }
      posList.innerHTML=html;
    }
    var pending=p.pending_orders.filter(function(o){ return o.market_id===curMid; });
    var ordList=document.getElementById('myOrdList');
    if(!pending.length){
      ordList.innerHTML='<div style="font-size:10px;color:var(--text3)">Нет открытых ордеров</div>';
    } else {
      ordList.innerHTML=pending.map(function(o){
        var rem=o.contracts-o.filled;
        var sideLbl=o.side==='BUY'?'▲ YES':'▼ NO';
        var sideCl =o.side==='BUY'?'var(--yes)':'var(--no)';
        return '<div class="ord-row">'+
          '<span style="color:'+sideCl+';font-family:var(--mono);font-size:10px">'+sideLbl+'</span>'+
          '<span style="font-family:var(--mono)">@'+o.price+'¢</span>'+
          '<span style="color:var(--text3)">'+rem.toFixed(1)+'/'+o.contracts.toFixed(1)+'c</span>'+
          '<button class="ord-cancel" onclick="cancelOrd(\''+o.id+'\')">Отменить</button>'+
        '</div>';
      }).join('');
    }
  }catch(e){}
}

async function cancelOrd(oid){
  try{
    var r=await api('/api/order/cancel','POST',{order_id:oid,user_id:curU});
    toast('Ордер отменён, возврат '+fmtBal(r.refund),'ok');
    await loadMyPositions();
  }catch(e){ toast(e.message,'err'); }
}

// Quick sell / close position at market
// type: 1 = sell YES (have BUY position), 2 = close NO (have SELL position)
async function quickSell(type, price, contracts){
  if(!curU||!curMid) return;
  var orderSide = type===1 ? 'SELL' : 'BUY';
  var dollars = type===1
    ? Math.round(contracts * price / 100 * 100) / 100
    : Math.round(contracts * (100-price) / 100 * 100) / 100;
  if(dollars < 0.01){ toast('Слишком маленький объём','err'); return; }
  try{
    var d=await api('/api/order','POST',{user_id:curU,market_id:curMid,side:orderSide,price:price,dollars:dollars});
    var msg=d.trades>0?'✅ Позиция закрыта @ '+price+'¢':'📋 Ордер на закрытие в книге';
    toast(msg, d.trades>0?'ok':'info');
    await loadTrades(); await loadMyPositions();
  }catch(e){ toast(e.message,'err'); }
}

// ── Order form ────────────────────────────────────────────────────────
function selSide(s){
  curSide=s;
  document.getElementById('ofBuy').className ='of-tab buy' +(s==='BUY' ?' active':'');
  document.getElementById('ofSell').className='of-tab sell'+(s==='SELL'?' active':'');
  var slider=document.getElementById('priceSlider');
  slider.className='pslider '+(s==='BUY'?'buy':'sell');
  document.getElementById('priceInp').className='price-inp '+(s==='BUY'?'buy':'sell');
  var sub=document.getElementById('subBtn');
  sub.className='sub '+(s==='BUY'?'buy':'sell');
  sub.textContent=s==='BUY'?'Разместить BUY YES ▲':'Разместить SELL YES (BUY NO) ▼';
  updPreview();
}

function getPrice(){
  var v=parseFloat(document.getElementById('priceInp').value)||50;
  return Math.round(Math.min(99.9,Math.max(0.1,v))*10)/10;
}
function onSliderChange(){
  // slider moved → update input
  var v=parseInt(document.getElementById('priceSlider').value);
  document.getElementById('priceInp').value=v;
  onPriceChange();
}
function onPriceInpChange(){
  // input changed → update slider (integer snap for slider)
  var p=getPrice();
  document.getElementById('priceSlider').value=Math.round(p);
  onPriceChange();
}
function onPriceChange(){
  var p=getPrice();
  var isB=curSide==='BUY';
  var noP=Math.round((100-p)*10)/10;  // цена NO = 100 - цена YES
  var inp=document.getElementById('priceInp');
  inp.className='price-inp '+(isB?'buy':'sell');

  // Коэф всегда от СВОЕЙ ставки:
  // BUY YES:  платишь p¢,      выигрыш $1 → коэф = 100/p
  // SELL YES: платишь (100-p)¢, выигрыш $1 → коэф = 100/(100-p)
  var myOdds = isB ? (100/p).toFixed(2) : (100/noP).toFixed(2);
  document.getElementById('oddsDisp').textContent=myOdds+'×';

  // Show/hide NO price row
  var noRow=document.getElementById('noPriceRow');
  if(!isB){
    noRow.style.display='block';
    document.getElementById('noPriceVal').textContent=noP+'¢';
    document.getElementById('noOddsVal').textContent=(100/noP).toFixed(2)+'×';
  } else {
    noRow.style.display='none';
  }

  // Hint
  if(isB){
    document.getElementById('priceHint').textContent=
      'Платишь '+p+'¢ за контракт YES · коэф '+myOdds+' на YES · считаешь вероятность '+p+'%';
  } else {
    document.getElementById('priceHint').textContent=
      'Платишь '+noP+'¢ за контракт NO · коэф '+myOdds+' на NO · считаешь вероятность '+noP+'%';
  }
  updPreview();
}

function updPreview(){
  if(!curMid) return;
  var price=getPrice();
  var dollars=parseFloat(document.getElementById('dollarInp').value)||0;
  var isB=curSide==='BUY';
  var contracts=isB?dollars/(price/100):dollars/((100-price)/100);
  contracts=Math.round(contracts*100)/100;
  var winGross=contracts;
  var comm=cfg.commission_pct;
  var winNet=Math.round(winGross*(1-comm)*100)/100;
  var profit=Math.round((winNet-dollars)*100)/100;
  var noPrice = Math.round((100-price)*10)/10;
  var myOdds  = isB ? (100/price).toFixed(2) : (100/noPrice).toFixed(2);
  document.getElementById('pvType').textContent=isB?'▲ BUY YES':'▼ SELL YES (BUY NO)';
  document.getElementById('pvType').style.color=isB?'var(--yes)':'var(--no)';
  // pvPrice label changes meaning: for BUY = YES price, for SELL = NO price (what you actually pay)
  document.getElementById('pvPrice').textContent=
    isB ? price.toFixed(1)+'¢ за YES' : noPrice.toFixed(1)+'¢ за NO ('+price.toFixed(1)+'¢ YES)';
  document.getElementById('pvContracts').textContent=contracts.toFixed(2)+' контр.';
  document.getElementById('pvCost').textContent=fmtBal(dollars)+' × '+myOdds+' возм.';
  document.getElementById('pvWin').textContent=fmtBal(winNet);
  document.getElementById('pvProfit').textContent=(profit>=0?'+':'')+profit.toFixed(2)+'$ ('+myOdds+' коэф)';
  document.getElementById('pvProfit').style.color=profit>=0?'var(--yes)':'var(--no)';

  // Counterparty explanation
  var cpBox = document.getElementById('pvCounterparty');
  if(cpBox){
    var cpPrice    = isB ? noPrice : price;           // what counterparty pays per contract
    var cpSide     = isB ? 'NO' : 'YES';
    var cpOdds     = isB ? (100/noPrice).toFixed(2) : (100/price).toFixed(2);
    var cpColor    = isB ? 'var(--no)' : 'var(--yes)';
    var cpWin      = isB ? 'Аргентина НЕ выиграет' : 'Аргентина выиграет';
    cpBox.style.display = 'block';
    cpBox.innerHTML =
      '<span style="font-weight:700;color:var(--text2)">Контрагент (продавец)</span><br>'+
      'Ставит на <span style="color:'+cpColor+';font-weight:700">'+cpSide+'</span> · '+
      'платит <span style="font-family:var(--mono);color:'+cpColor+'">'+cpPrice.toFixed(1)+'¢</span> · '+
      'коэф <span style="font-family:var(--mono);font-weight:700;color:'+cpColor+'">'+cpOdds+'×</span><br>'+
      '<span style="color:var(--text3)">Считает что '+cpWin+' (вероятность ~'+cpPrice.toFixed(0)+'%)</span><br>'+
      '<span style="color:var(--text3)">Пот: ваши '+price.toFixed(1)+'¢ + его '+noPrice.toFixed(1)+'¢ = $1 на контракт</span>';
  }
}

async function submitOrder(){
  var price=getPrice();
  var dollars=parseFloat(document.getElementById('dollarInp').value);
  if(!dollars||dollars<1){ toast('Минимум $1','err'); return; }
  var btn=document.getElementById('subBtn'); btn.disabled=true;
  try{
    var d=await api('/api/order','POST',{user_id:curU,market_id:curMid,side:curSide,price:price,dollars:dollars});
    var msg=d.trades>0?'✅ Ордер исполнен! Сделок: '+d.trades:'📋 Ордер в книге — ждём сделки';
    toast(msg,'ok');
    await loadTrades(); await loadMyPositions();
  }catch(e){ toast(e.message,'err'); }
  finally{ btn.disabled=false; }
}

// ── Market order ──────────────────────────────────────────────────────
var curOrderType = 'limit'; // 'limit' | 'market'

function setOrderType(t){
  curOrderType = t;
  document.getElementById('otLimit').className='ot-tab'+(t==='limit'?' active':'');
  document.getElementById('otMarket').className='ot-tab'+(t==='market'?' active':'');
  document.getElementById('limitForm').style.display=t==='limit'?'block':'none';
  document.getElementById('marketForm').style.display=t==='market'?'block':'none';
  if(t==='market') updMktPreview();
}

function getMktSide(){
  // BUY YES → нужен ASK (продавцы YES), SELL YES → нужен BID (покупатели YES)
  return curSide;
}

function updMktPreview(){
  if(!curMid) return;
  var m=mkts[curMid];
  var ob=m.orderbook||{bids:[],asks:[]};
  var isB=curSide==='BUY';
  var dollars=parseFloat(document.getElementById('mktDollarInp').value)||0;
  var comm=cfg.commission_pct;

  // Best price available
  var bestAsk=ob.asks&&ob.asks.length?ob.asks[0].price:null;  // lowest ask
  var bestBid=ob.bids&&ob.bids.length?ob.bids[0].price:null;  // highest bid

  document.getElementById('mktBestAsk').textContent=bestAsk!=null?bestAsk+'¢':'нет продавцов';
  document.getElementById('mktBestAsk').style.color=bestAsk!=null?'var(--yes)':'var(--text3)';
  document.getElementById('mktBestBid').textContent=bestBid!=null?bestBid+'¢':'нет покупателей';
  document.getElementById('mktBestBid').style.color=bestBid!=null?'var(--no)':'var(--text3)';

  var spread=bestAsk!=null&&bestBid!=null?Math.round((bestAsk-bestBid)*10)/10:null;
  document.getElementById('mktSpread').textContent=spread!=null?'спред '+spread+'¢':'';

  // Execution price
  var execPrice = isB ? bestAsk : bestBid;
  var side = isB ? 'asks' : 'bids';
  var levels = ob[side] || [];

  // Total available in book on opposite side
  var totalAvailDollars = isB
    ? levels.reduce(function(s,r){ return s+r.dollars; }, 0)    // ask dollars = (100-p)/100 * contracts
    : levels.reduce(function(s,r){ return s+r.dollars; }, 0);   // bid dollars = p/100 * contracts

  var btn=document.getElementById('mktBtn');
  var warn=document.getElementById('mktWarn');
  warn.innerHTML='';

  if(!execPrice){
    document.getElementById('mktPvPrice').textContent='—';
    document.getElementById('mktPvOdds').textContent='—';
    document.getElementById('mktPvContracts').textContent='—';
    document.getElementById('mktPvAvail').textContent='—';
    document.getElementById('mktPvWin').textContent='—';
    document.getElementById('mktPvProfit').textContent='—';
    btn.textContent=(isB?'⚡ Купить':'⚡ Продать')+' по рынку';
    btn.disabled=true;
    warn.innerHTML='<div class="mkt-warn">⚠ Нет '+( isB?'продавцов YES (ASK)':'покупателей YES (BID)')+' в стакане</div>';
    return;
  }

  // Simulate fill across levels (walk the book)
  var remaining = dollars;
  var totalContracts = 0;
  var totalCost = 0;
  var levels2 = isB ? [...levels] : [...levels].reverse(); // asks asc, bids desc
  for(var i=0;i<levels2.length;i++){
    var lvl=levels2[i];
    var lvlPrice=lvl.price;
    var costPerContract = isB ? lvlPrice/100 : (100-lvlPrice)/100;
    var lvlMaxDollars = lvl.contracts * costPerContract;
    var useD = Math.min(remaining, lvlMaxDollars);
    var useC = useD / costPerContract;
    totalContracts += useC;
    totalCost += useD;
    remaining -= useD;
    if(remaining < 0.01) break;
  }

  var filled = totalCost;
  var avgPrice = totalContracts>0 ? Math.round(totalCost/totalContracts*100) : execPrice;
  var slippage = isB ? avgPrice - execPrice : execPrice - avgPrice;
  var winGross = totalContracts;
  var winNet = Math.round(winGross*(1-comm)*100)/100;
  var profit = Math.round((winNet - filled)*100)/100;
  var unfilled = remaining;

  document.getElementById('mktPvPrice').textContent=
    (totalContracts>0&&slippage>0.5 ? '~'+Math.round(avgPrice*10)/10 : execPrice)+'¢';
  document.getElementById('mktPvPrice').style.color=isB?'var(--yes)':'var(--no)';
  document.getElementById('mktPvOdds').textContent=
    isB ? (100/execPrice).toFixed(2)+'×' : (100/(100-execPrice)).toFixed(2)+'×';
  document.getElementById('mktPvContracts').textContent=(Math.round(totalContracts*100)/100).toFixed(2)+' конт.';
  document.getElementById('mktPvAvail').textContent=fmtBal(Math.round(totalAvailDollars*100)/100);
  document.getElementById('mktPvWin').textContent=fmtBal(winNet);
  document.getElementById('mktPvProfit').textContent=(profit>=0?'+':'')+profit.toFixed(2)+'$';
  document.getElementById('mktPvProfit').style.color=profit>=0?'var(--yes)':'var(--no)';

  btn.textContent=(isB?'⚡ Купить YES @ '+execPrice+'¢':'⚡ Продать YES @ '+execPrice+'¢')+' (×'+(isB?(100/execPrice).toFixed(1):(100/(100-execPrice)).toFixed(1))+')';
  btn.className='mkt-btn '+(isB?'buy':'sell');
  btn.disabled=!dollars||dollars<1;

  if(unfilled>0.5){
    warn.innerHTML='<div class="mkt-warn">⚠ Стакан покроет только '+fmtBal(Math.round(filled*100)/100)+' из '+fmtBal(dollars)+' — остаток встанет лимитом</div>';
  }
  if(slippage>1){
    warn.innerHTML+='<div class="mkt-warn">⚠ Проскальзывание ~'+Math.round(slippage*10)/10+'¢ (крупный ордер ест несколько уровней)</div>';
  }
}

async function submitMarketOrder(){
  if(!curMid||!curU) return;
  var m=mkts[curMid];
  var ob=m.orderbook||{bids:[],asks:[]};
  var isB=curSide==='BUY';
  var dollars=parseFloat(document.getElementById('mktDollarInp').value);
  if(!dollars||dollars<1){ toast('Минимум $1','err'); return; }

  // For market order: use best opposite price (aggressive limit)
  var execPrice = isB
    ? (ob.asks&&ob.asks.length ? ob.asks[0].price : null)
    : (ob.bids&&ob.bids.length ? ob.bids[0].price : null);

  if(!execPrice){ toast('Нет ликвидности в стакане','err'); return; }

  var btn=document.getElementById('mktBtn'); btn.disabled=true;
  try{
    // Place as aggressive limit — will immediately match
    var d=await api('/api/order','POST',{user_id:curU,market_id:curMid,side:curSide,price:execPrice,dollars:dollars});
    var msg=d.trades>0?
      '⚡ Исполнено по рынку @ '+execPrice+'¢! Сделок: '+d.trades:
      '📋 Ордер в книге (стакан изменился)';
    toast(msg,'ok');
    await loadTrades(); await loadMyPositions(); updMktPreview();
  }catch(e){ toast(e.message,'err'); }
  finally{ btn.disabled=false; }
}

async function doResolve(outcome){
  if(!confirm('Закрыть рынок с исходом «'+outcome+'»?')) return;
  try{
    var d=await api('/api/resolve','POST',{market_id:curMid,outcome:outcome});
    toast('✅ Рынок закрыт! Выплаты: '+fmtBal(d.total_payout)+' · Комиссия: '+fmtBal(d.total_commission),'ok');
    closeMarket();
  }catch(e){ toast(e.message,'err'); }
}

function closeMarket(){ document.getElementById('mktOvl').classList.remove('open'); curMid=null; }

// ── Portfolio ─────────────────────────────────────────────────────────
async function renderPort(){
  var el=document.getElementById('portContent');
  if(!curU){ el.innerHTML='<div class="empty">Выберите участника</div>'; return; }
  var u=usrs[curU];
  try{
    var p=await api('/api/portfolio/'+curU);
    var comm=cfg.commission_pct;

    // ── Aggregate stats ──────────────────────────────────────────────
    var totalInvested=0, totalPositions=0, totalPnl=0, totalCurVal=0;
    p.positions.forEach(function(pos){
      var m=pos.market;
      var curP=m.last_price||m.mid_price;
      totalInvested+=pos.yes_cost+pos.no_cost;
      if(pos.yes_contracts>0||pos.no_contracts>0) totalPositions++;
      if(curP!=null){
        var yVal=pos.yes_contracts*curP/100;
        var nVal=pos.no_contracts*(100-curP)/100;
        totalCurVal+=yVal+nVal;
        totalPnl+=(yVal-pos.yes_cost)+(nVal-pos.no_cost);
      }
    });
    var pnlCls=totalPnl>0.01?'up':totalPnl<-0.01?'dn':'flat';

    var html=
      // Top summary bar
      '<div class="port-total-bar">'+
        '<div class="port-total-item">'+
          '<div class="port-total-lbl">Свободный баланс</div>'+
          '<div class="port-total-val" style="color:var(--yes)">'+fmtBal(u.balance)+'</div>'+
        '</div>'+
        '<div style="width:1px;background:var(--b1);align-self:stretch"></div>'+
        '<div class="port-total-item">'+
          '<div class="port-total-lbl">Вложено в позиции</div>'+
          '<div class="port-total-val" style="color:var(--purple)">'+fmtBal(totalInvested)+'</div>'+
        '</div>'+
        '<div style="width:1px;background:var(--b1);align-self:stretch"></div>'+
        '<div class="port-total-item">'+
          '<div class="port-total-lbl">Тек. стоимость</div>'+
          '<div class="port-total-val">'+( totalCurVal>0?fmtBal(totalCurVal):'—')+'</div>'+
        '</div>'+
        '<div style="width:1px;background:var(--b1);align-self:stretch"></div>'+
        '<div class="port-total-item">'+
          '<div class="port-total-lbl">Нереализ. P&L</div>'+
          '<div class="port-total-val '+pnlCls+'">'+(totalInvested>0?(totalPnl>=0?'+':'')+fmtBal(totalPnl):'—')+'</div>'+
        '</div>'+
        '<div style="width:1px;background:var(--b1);align-self:stretch"></div>'+
        '<div class="port-total-item">'+
          '<div class="port-total-lbl">Ордеров ждёт</div>'+
          '<div class="port-total-val" style="color:var(--gold)">'+p.pending_orders.length+'</div>'+
        '</div>'+
      '</div>';

    if(!p.positions.length&&!p.pending_orders.length){
      html+='<div class="empty" style="padding:32px 0">Нет позиций — откройте рынок и разместите ордер</div>';
      el.innerHTML=html; return;
    }

    // ── Position cards ───────────────────────────────────────────────
    p.positions.forEach(function(pos){
      var m=pos.market;
      var isR=m.status==='resolved';
      var curP=m.last_price||(m.best_bid&&m.best_ask?(m.best_bid+m.best_ask)/2:null);
      var invested=pos.yes_cost+pos.no_cost;

      // P&L calculation
      var curVal=null, pnl=null;
      if(curP!=null){
        var yVal=pos.yes_contracts*curP/100;
        var nVal=pos.no_contracts*(100-curP)/100;
        curVal=Math.round((yVal+nVal)*100)/100;
        pnl=Math.round((curVal-invested)*100)/100;
      }
      if(isR){
        // On resolved: actual outcome
        var win=m.outcome==='YES'?pos.yes_contracts*(1-comm):pos.no_contracts*(1-comm);
        pnl=Math.round((win-invested)*100)/100;
        curVal=Math.round(win*100)/100;
      }
      var pcls=pnl==null?'flat':pnl>0.01?'up':pnl<-0.01?'dn':'flat';
      var pnlTxt=pnl!=null?(pnl>=0?'+':'')+fmtBal(pnl):'—';
      var pnlPct=pnl!=null&&invested>0?' ('+(pnl/invested*100>=0?'+':'')+(pnl/invested*100).toFixed(1)+'%)':'';
      var statusDot=isR
        ?(m.outcome==='YES'?'<span style="color:var(--yes);font-weight:700">✓ YES WIN</span>':'<span style="color:var(--no);font-weight:700">✗ NO WIN</span>')
        :'<span style="color:var(--yes)">● Открыто</span>';

      html+='<div class="pcard">'+
        // Clickable header → open market
         '<div class="pcard-head" data-mid="'+m.id+'" onclick="openMarket(this.dataset.mid)">'+  
          '<div style="flex:1">'+
            '<div class="pcard-title">'+m.title+'</div>'+
            '<div class="pcard-meta"><span>'+m.category+'</span><span>·</span>'+statusDot+
              (curP!=null&&!isR?'<span>· цена '+Math.round(curP*10)/10+'¢</span>':'')+
            '</div>'+
          '</div>'+
          '<div style="text-align:right">'+
            '<div class="pcard-pnl '+pcls+'">'+pnlTxt+pnlPct+'</div>'+
            '<div style="font-size:9px;color:var(--text3);margin-top:2px">вложено '+fmtBal(invested)+'</div>'+
          '</div>'+
        '</div>'+
        '<div class="pcard-body">';

      // YES position row
      if(pos.yes_contracts>0){
        var yEntry=Math.round(pos.yes_cost/pos.yes_contracts*100*10)/10;
        var yNow=curP!=null?Math.round(curP*10)/10:null;
        var yPnl=curP!=null?Math.round((pos.yes_contracts*curP/100-pos.yes_cost)*100)/100:null;
        var yPcls=yPnl==null?'flat':yPnl>=0?'up':'dn';
        var bidForSell=m.best_bid;
        html+=
          '<div class="ppos">'+
            '<span class="pos-side yes" style="font-size:9px">YES</span>'+
            '<div><div class="ppos-lbl">Контракты</div><div class="ppos-val">'+pos.yes_contracts.toFixed(2)+'</div></div>'+
            '<div><div class="ppos-lbl">Вход / Сейчас</div><div class="ppos-val">'+yEntry+'¢'+(yNow?' / '+yNow+'¢':'')+'</div></div>'+
            '<div><div class="ppos-lbl">P&L</div><div class="ppos-val '+yPcls+'">'+(yPnl!=null?(yPnl>=0?'+':'')+fmtBal(yPnl):'—')+'</div></div>'+
            (bidForSell&&!isR
              ?'<button class="pos-sell-btn" data-mid="'+m.id+'" data-price="'+bidForSell+'" data-cont="'+pos.yes_contracts+'" data-type="1" onclick="portQuickSell(this.dataset.type,this.dataset.mid,+this.dataset.price,+this.dataset.cont)">Продать @ '+bidForSell+'¢</button>'
              :'<span></span>')+
          '</div>';
      }

      // NO position row
      if(pos.no_contracts>0){
        var nEntry=Math.round(pos.no_cost/pos.no_contracts*100*10)/10;
        var nNow=curP!=null?Math.round((100-curP)*10)/10:null;
        var nPnl=curP!=null?Math.round((pos.no_contracts*(100-curP)/100-pos.no_cost)*100)/100:null;
        var nPcls=nPnl==null?'flat':nPnl>=0?'up':'dn';
        var askForClose=m.best_ask;
        html+=
          '<div class="ppos">'+
            '<span class="pos-side no" style="font-size:9px">NO</span>'+
            '<div><div class="ppos-lbl">Контракты</div><div class="ppos-val">'+pos.no_contracts.toFixed(2)+'</div></div>'+
            '<div><div class="ppos-lbl">Вход / Сейчас</div><div class="ppos-val">'+nEntry+'¢NO'+(nNow?' / '+nNow+'¢':'')+' NO</div></div>'+
            '<div><div class="ppos-lbl">P&L</div><div class="ppos-val '+nPcls+'">'+(nPnl!=null?(nPnl>=0?'+':'')+fmtBal(nPnl):'—')+'</div></div>'+
            (askForClose&&!isR
              ?'<button class="pos-sell-btn" data-mid="'+m.id+'" data-price="'+askForClose+'" data-cont="'+pos.no_contracts+'" data-type="2" onclick="portQuickSell(this.dataset.type,this.dataset.mid,+this.dataset.price,+this.dataset.cont)">Закрыть @ '+askForClose+'¢</button>'
              :'<span></span>')+
          '</div>';
      }

      // Pending orders for this market
      var pendingHere=p.pending_orders.filter(function(o){ return o.market_id===m.id; });
      if(pendingHere.length){
        html+='<div style="margin-top:6px;font-size:9px;color:var(--text3);font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Открытые ордера</div>';
        pendingHere.forEach(function(o){
          var rem=o.contracts-o.filled;
          var sCl=o.side==='BUY'?'var(--yes)':'var(--no)';
          var sLbl=o.side==='BUY'?'▲ BUY YES':'▼ SELL YES';
          html+='<div class="pord-row">'+
            '<span style="color:'+sCl+';font-family:var(--mono);font-size:10px;font-weight:700">'+sLbl+'</span>'+
            '<span style="font-family:var(--mono)">@ '+o.price+'¢</span>'+
            '<span style="color:var(--text3)">'+rem.toFixed(1)+'/'+o.contracts.toFixed(1)+' конт.</span>'+
             '<button class="ord-cancel" style="margin-left:auto" data-oid="'+o.id+'" onclick="portCancelOrd(this.dataset.oid)">Отменить</button>'+  
          '</div>';
        });
      }

      html+='</div></div>'; // pcard-body + pcard
    });

    el.innerHTML=html;
  }catch(e){ el.innerHTML='<div class="empty">Ошибка загрузки</div>'; }
}

// Portfolio page quick actions
async function portQuickSell(type, mid, price, contracts){
  var prevMid=curMid;
  curMid=mid;
  await quickSell(type, price, contracts);
  curMid=prevMid;
  renderPort();
}
async function portCancelOrd(oid){
  try{
    var r=await api('/api/order/cancel','POST',{order_id:oid,user_id:curU});
    toast('Ордер отменён, возврат '+fmtBal(r.refund),'ok');
    renderPort();
  }catch(e){ toast(e.message,'err'); }
}

// ── Activity ──────────────────────────────────────────────────────────
function renderAct(){
  var el=document.getElementById('alist');
  if(!actLog.length){ el.innerHTML='<div class="empty">Нет событий</div>'; return; }
  el.innerHTML=''; actLog.slice(0,60).forEach(function(a){ el.appendChild(mkAct(a)); });
}
function prependAct(a){
  var el=document.getElementById('alist');
  if(el.querySelector('.empty')) el.innerHTML='';
  el.insertBefore(mkAct(a),el.firstChild);
  while(el.children.length>60) el.removeChild(el.lastChild);
}
function mkAct(a){
  var d=document.createElement('div'); d.className='ai';
  d.innerHTML='<div class="adot '+a.kind+'"></div><span style="flex:1">'+a.message+'</span><span class="ats">'+a.ts+'</span>';
  return d;
}

// ── User header & dropdown ────────────────────────────────────────────
function renderHdr(){
  var u=usrs[curU];
  document.getElementById('hName').textContent=u?u.name:'Выбрать';
  document.getElementById('hBal').textContent=u?fmtBal(u.balance):'';
  var av=document.getElementById('hAv');
  av.textContent=u?u.name[0].toUpperCase():'?';
  av.style.background=u?u.color:'#333';
  // Update no-user hint
  if(document.getElementById('panel-markets').classList.contains('active'))
    document.getElementById('noUserHint').className='no-user-hint'+(u?'':' visible');
}

function renderDD(){
  var el=document.getElementById('ddList'); el.innerHTML='';
  Object.values(usrs).forEach(function(u){
    var d=document.createElement('div');
    d.className='ddi'+(u.id===curU?' sel':'');
    d.innerHTML=
      '<div class="av" style="background:'+u.color+';width:24px;height:24px;font-size:9px">'+u.name[0].toUpperCase()+'</div>'+
      '<span class="diname">'+u.name+'</span>'+
      '<span class="dibal">'+fmtBal(u.balance)+'</span>';
    // Use click — now safe since dd is outside upill
    d.addEventListener('click',function(){
      curU=u.id;
      renderHdr(); renderDD(); closeDD();
      toast('Активен: '+u.name,'info');
    });
    el.appendChild(d);
  });
}

function showDD(){
  var pill=document.getElementById('upill');
  var dd=document.getElementById('dd');
  var r=pill.getBoundingClientRect();
  dd.style.top=(r.bottom+6)+'px';
  dd.style.right=(window.innerWidth-r.right)+'px';
  dd.classList.add('open');
  document.getElementById('upill').classList.add('open');
}
function closeDD(){ document.getElementById('dd').classList.remove('open'); document.getElementById('upill').classList.remove('open'); }
function toggleDD(){ document.getElementById('dd').classList.contains('open')?closeDD():showDD(); }

function showTab(n){
  document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
  document.getElementById('panel-'+n).classList.add('active');
  document.getElementById('tab-'+n).classList.add('active');
  if(n==='portfolio') renderPort();
  if(n==='activity')  renderAct();
  if(n==='markets')   document.getElementById('noUserHint').className='no-user-hint'+(curU?'':' visible');
}

function toast(msg,t){
  var d=document.createElement('div'); d.className='toast '+(t||'info'); d.textContent=msg;
  document.getElementById('toasts').appendChild(d); setTimeout(function(){ d.remove(); },4000);
}

// ── Event listeners ───────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════
// SIMULATION ENGINE
// ══════════════════════════════════════════════════════════════════
// ── Simulation engine ────────────────────────────────────────────────
var simTimer = null;
var simTick  = 0;
var simCenterPrice = {}; // mid → drifting center price
var simStats = {ticks:0, trades:0, volume:0};

var BOT_NAMES = ['Робот Феникс','Бот Арбитр','Алго Икс','Трейдер-7','Нейро Плюс','Сигма Бот','Дельта Про','Кванто Икс'];

function simLog(msg){
  var el=document.getElementById('simLog');
  if(!el) return;
  var d=document.createElement('div');
  d.innerHTML='<span style="color:var(--text3)">'+new Date().toTimeString().slice(0,8)+'</span> '+msg;
  el.insertBefore(d,el.firstChild);
  while(el.children.length>60) el.removeChild(el.lastChild);
}

function updateSimBotList(){
  var el=document.getElementById('simBotList');
  if(!el) return;
  var bots=Object.values(usrs);
  if(!bots.length){ el.innerHTML='<span>Нет участников</span>'; return; }
  el.innerHTML=bots.map(function(u){
    return '<div style="display:flex;align-items:center;gap:6px;padding:3px 0;border-bottom:1px solid var(--b1)">'+
      '<div class="av" style="background:'+u.color+';width:18px;height:18px;font-size:8px;flex-shrink:0">'+u.name[0]+'</div>'+
      '<span style="font-size:11px;flex:1">'+u.name+'</span>'+
      '<span style="font-family:var(--mono);font-size:10px;color:var(--yes)">'+fmtBal(u.balance)+'</span>'+
    '</div>';
  }).join('');
}

function updateSimMarketSel(){
  var sel=document.getElementById('simMarketSel');
  if(!sel) return;
  var cur=sel.value||'ALL';
  sel.innerHTML='<option value="ALL">🌐 Все открытые</option>';
  Object.values(mkts).filter(function(m){ return m.status==='open'; }).forEach(function(m){
    var opt=document.createElement('option');
    opt.value=m.id; opt.textContent=m.title.slice(0,38);
    sel.appendChild(opt);
  });
  sel.value=cur;
}

function getSimCenter(mid){
  if(!simCenterPrice[mid]){
    var m=mkts[mid];
    simCenterPrice[mid]=m?(m.last_price||m.mid_price||50):50;
  }
  return simCenterPrice[mid];
}

function updateSimStats(){
  document.getElementById('statTicks').textContent=simStats.ticks;
  document.getElementById('statTrades').textContent=simStats.trades;
  document.getElementById('statVol').textContent='$'+Math.round(simStats.volume);
}

async function doSimOrder(mid, bot){
  var m=mkts[mid];
  if(!m||m.status!=='open') return;

  var useDrift  = document.getElementById('simDrift').checked;
  var spread    = parseFloat(document.getElementById('simSpread').value)||4;
  var orderSize = parseFloat(document.getElementById('simOrderSize').value)||80;
  var aggr      = document.getElementById('simAggression').value;
  var ob        = m.orderbook||{bids:[],asks:[]};

  // Drift center price
  var center = getSimCenter(mid);
  if(useDrift){
    var target = parseFloat(document.getElementById('simCenterPrice').value)||50;
    var drift  = (Math.random()-0.48)*2.5 + (target-center)*0.04;
    center = Math.round(Math.min(94,Math.max(6, center+drift))*10)/10;
    simCenterPrice[mid]=center;
    document.getElementById('simCenterLbl').textContent='~'+center+'¢';
  }

  var isBuy = Math.random()>0.5;

  // Determine price based on aggression
  var price;
  if(aggr==='aggressive'){
    // Market order: cross the spread
    price = isBuy
      ? (ob.asks.length ? ob.asks[0].price   : Math.round(center+spread))
      : (ob.bids.length ? ob.bids[0].price   : Math.round(center-spread));
  } else if(aggr==='passive'){
    // Always post inside spread
    var delta = Math.random()*spread*0.8 + 1;
    price = isBuy
      ? Math.round(Math.min(99,Math.max(1,center-delta))*10)/10
      : Math.round(Math.min(99,Math.max(1,center+delta))*10)/10;
  } else {
    // Mixed: 40% aggressive, 60% passive
    var isAggr = Math.random()<0.4;
    if(isAggr){
      price = isBuy
        ? (ob.asks.length ? ob.asks[0].price : Math.round(center+2))
        : (ob.bids.length ? ob.bids[0].price : Math.round(center-2));
    } else {
      var d2 = Math.random()*spread + 0.5;
      price = isBuy
        ? Math.round(Math.min(99,Math.max(1,center-d2))*10)/10
        : Math.round(Math.min(99,Math.max(1,center+d2))*10)/10;
    }
  }
  price = Math.round(Math.min(99.9,Math.max(0.1,price))*10)/10;

  var maxDollars = Math.min(orderSize, Math.max(bot.balance*0.05, 5)); // max 5% balance OR orderSize
  var dollars    = Math.round(maxDollars*(0.6+Math.random()*0.4)*100)/100;
  if(dollars<1||dollars>bot.balance) return;

  try{
    var d=await api('/api/order','POST',{
      user_id:bot.id, market_id:mid,
      side:isBuy?'BUY':'SELL',
      price:price, dollars:dollars
    });
    var sideHtml = isBuy
      ? '<span style="color:var(--yes)">BUY</span>'
      : '<span style="color:var(--no)">SELL</span>';
    var mktName  = mkts[mid]?mkts[mid].title.slice(0,20):'?';
    var matched  = d.trades>0
      ? ' <span style="color:var(--yes)">✓ '+d.trades+' сд.</span>'
      : '';
    simLog('<b>'+bot.name+'</b> '+sideHtml+' @'+price+'¢ $'+dollars.toFixed(0)
      +' <span style="color:var(--text3)">'+mktName+'</span>'+matched);

    if(d.trades>0){
      simStats.trades+=d.trades;
      simStats.volume+=dollars;
    }
  }catch(e){ /* silent */ }
}

async function simStep(){
  simStats.ticks++;
  document.getElementById('simTick').textContent='тик #'+simStats.ticks;
  updateSimStats();

  // Pick market(s)
  var selMid=document.getElementById('simMarketSel').value;
  var openMkts=Object.values(mkts).filter(function(m){ return m.status==='open'; });
  if(!openMkts.length){
    simLog('<span style="color:var(--gold)">⚠ Нет открытых рынков</span>');
    return;
  }
  var targetMkts = selMid==='ALL' ? openMkts : openMkts.filter(function(m){ return m.id===selMid; });
  if(!targetMkts.length) return;

  // Pick random mid
  var m = targetMkts[Math.floor(Math.random()*targetMkts.length)];

  // Pick bot with enough balance
  var bots=Object.values(usrs).filter(function(u){ return u.balance>5; });
  if(!bots.length) return;
  var bot=bots[Math.floor(Math.random()*bots.length)];

  await doSimOrder(m.id, bot);
}

async function burstSim(){
  // Fire 10 orders rapidly for dramatic presentation effect
  var openMkts=Object.values(mkts).filter(function(m){ return m.status==='open'; });
  if(!openMkts.length){ toast('Нет открытых рынков','err'); return; }
  var bots=Object.values(usrs).filter(function(u){ return u.balance>5; });
  if(!bots.length){ toast('Нет участников','err'); return; }
  simLog('<span style="color:var(--gold)">⚡ Burst — 10 ордеров!</span>');
  for(var i=0;i<10;i++){
    var m=openMkts[Math.floor(Math.random()*openMkts.length)];
    var bot=bots[Math.floor(Math.random()*bots.length)];
    await doSimOrder(m.id, bot);
    await new Promise(function(r){ setTimeout(r,120); });
  }
}

function startSim(){
  if(simTimer) return;
  simStats={ticks:0,trades:0,volume:0};
  var interval=Math.max(300,(parseFloat(document.getElementById('simInterval').value)||1.5)*1000);
  simTimer=setInterval(simStep, interval);
  document.getElementById('simDot').classList.add('running');
  document.getElementById('simStatusTxt').textContent='Симуляция запущена';
  document.getElementById('btnSimStart').style.display='none';
  document.getElementById('btnSimStop').style.display='';
  simLog('<span style="color:var(--yes)">▶ Запущена · интервал '+interval+'мс</span>');
}

function stopSim(){
  if(simTimer){ clearInterval(simTimer); simTimer=null; }
  document.getElementById('simDot').classList.remove('running');
  document.getElementById('simStatusTxt').textContent='Симуляция остановлена';
  document.getElementById('btnSimStart').style.display='';
  document.getElementById('btnSimStop').style.display='none';
  simLog('<span style="color:var(--no)">■ Остановлена · '+simStats.trades+' сделок $'+Math.round(simStats.volume)+' объём</span>');
}

document.addEventListener('DOMContentLoaded',function(){
  // Dropdown — toggle on pill click, close on outside click
  document.getElementById('upill').addEventListener('click',function(){
    toggleDD();
  });
  document.addEventListener('click',function(e){
    var pill=document.getElementById('upill');
    var dd=document.getElementById('dd');
    if(!pill.contains(e.target) && !dd.contains(e.target)) closeDD();
  });

  // New user button in dropdown
  document.getElementById('btnNewU').addEventListener('click',function(){
    closeDD();
    document.getElementById('usrOvl').classList.add('open');
    setTimeout(function(){ document.getElementById('nuName').focus(); },100);
  });

  // Market modal
  document.getElementById('btnClose').addEventListener('click',closeMarket);
  document.getElementById('mktOvl').addEventListener('click',function(e){ if(e.target===this) closeMarket(); });

  // User modal
  document.getElementById('btnCloseU').addEventListener('click',function(){ document.getElementById('usrOvl').classList.remove('open'); });
  document.getElementById('usrOvl').addEventListener('click',function(e){ if(e.target===this) this.classList.remove('open'); });

  // Order form
  document.getElementById('ofBuy').addEventListener('click',function(){ selSide('BUY'); if(curOrderType==='market') updMktPreview(); });
  document.getElementById('ofSell').addEventListener('click',function(){ selSide('SELL'); if(curOrderType==='market') updMktPreview(); });
  document.getElementById('otLimit').addEventListener('click',function(){ setOrderType('limit'); });
  document.getElementById('obExpand').addEventListener('click',function(){
    obLevels = obLevels===10 ? 20 : 10;
    if(curMid&&mkts[curMid]) renderOB(mkts[curMid]);
  });
  document.getElementById('otMarket').addEventListener('click',function(){ setOrderType('market'); });
  document.getElementById('mktDollarInp').addEventListener('input',updMktPreview);
  document.querySelectorAll('.mkt-ap').forEach(function(b){
    b.addEventListener('click',function(){ document.getElementById('mktDollarInp').value=this.dataset.v; updMktPreview(); });
  });
  document.getElementById('mktApMax').addEventListener('click',function(){
    if(!curU) return;
    var u=usrs[curU];
    if(u){ document.getElementById('mktDollarInp').value=Math.floor(u.balance); updMktPreview(); }
  });
  document.getElementById('mktBtn').addEventListener('click',submitMarketOrder);
  document.getElementById('priceSlider').addEventListener('input',onSliderChange);
  document.getElementById('priceInp').addEventListener('input',onPriceInpChange);
  document.getElementById('priceInp').addEventListener('change',onPriceInpChange);
  document.getElementById('dollarInp').addEventListener('input',updPreview);
  document.querySelectorAll('.ap[data-v]').forEach(function(b){
    b.addEventListener('click',function(){ document.getElementById('dollarInp').value=this.dataset.v; updPreview(); });
  });
  // MAX button — fill max available balance
  document.getElementById('apMax').addEventListener('click',function(){
    if(!curU) return;
    var u=usrs[curU];
    if(u) { document.getElementById('dollarInp').value=Math.floor(u.balance); updPreview(); }
  });
  document.getElementById('subBtn').addEventListener('click',submitOrder);

  // Tabs
  document.querySelectorAll('.tab').forEach(function(t){
    t.addEventListener('click',function(){
      showTab(this.id.replace('tab-',''));
      if(this.id==='tab-simulate'){ updateSimBotList(); updateSimMarketSel(); }
    });
  });
  document.getElementById('btnSimStart').addEventListener('click',startSim);
  document.getElementById('btnSimStop').addEventListener('click',stopSim);
  document.getElementById('btnAddBot').addEventListener('click',async function(){
    var names=BOT_NAMES;
    var used=Object.values(usrs).map(function(u){ return u.name; });
    var avail=names.filter(function(n){ return used.indexOf(n)===-1; });
    var name=avail.length?avail[0]:'Бот-'+Math.floor(Math.random()*999);
    var bal=parseFloat(document.getElementById('simBotBal').value)||5000;
    try{
      var u=await api('/api/users','POST',{name:name,balance:bal});
      usrs[u.id]=u; updateSimBotList();
      toast('Бот '+name+' добавлен','ok');
    }catch(e){ toast(e.message,'err'); }
  });

  // Config
  document.getElementById('btnSaveCfg').addEventListener('click',async function(){
    var v=parseFloat(document.getElementById('cfgComm').value)||2;
    try{ await api('/api/config','POST',{commission_pct:v/100}); toast('Сохранено','ok'); }
    catch(e){ toast(e.message,'err'); }
  });

  // Reset
  document.getElementById('btnReset').addEventListener('click',function(){
    document.getElementById('resetOvl').classList.add('open');
  });
  document.getElementById('btnResetCancel').addEventListener('click',function(){
    document.getElementById('resetOvl').classList.remove('open');
  });
  document.getElementById('resetOvl').addEventListener('click',function(e){
    if(e.target===this) this.classList.remove('open');
  });
  document.getElementById('btnResetConfirm').addEventListener('click',async function(){
    var btn=this; btn.disabled=true; btn.textContent='Сброс...';
    try{
      await api('/api/reset','POST');
      document.getElementById('resetOvl').classList.remove('open');
    }catch(e){
      toast(e.message,'err');
      btn.disabled=false; btn.textContent='🔄 Сбросить';
    }
  });

  // Create market
  document.getElementById('btnCreate').addEventListener('click',async function(){
    var t=document.getElementById('nTitle').value.trim();
    if(!t){ toast('Введите вопрос','err'); return; }
    try{
      await api('/api/markets','POST',{title:t,desc:document.getElementById('nDesc').value.trim(),category:document.getElementById('nCat').value});
      document.getElementById('nTitle').value=''; document.getElementById('nDesc').value='';
      showTab('markets'); toast('Рынок создан!','ok');
    }catch(e){ toast(e.message,'err'); }
  });

  // Create user
  document.getElementById('btnCrtU').addEventListener('click',async function(){
    var name=document.getElementById('nuName').value.trim();
    if(!name){ toast('Введите имя','err'); return; }
    try{
      var u=await api('/api/users','POST',{name:name,balance:parseFloat(document.getElementById('nuBal').value)||5000});
      usrs[u.id]=u; curU=u.id; renderHdr(); renderDD();
      document.getElementById('usrOvl').classList.remove('open');
      toast('Добро пожаловать, '+u.name+'!','ok');
    }catch(e){ toast(e.message,'err'); }
  });

  // Enter key in user modal
  document.getElementById('nuName').addEventListener('keydown',function(e){
    if(e.key==='Enter') document.getElementById('btnCrtU').click();
  });
  document.getElementById('nTitle').addEventListener('keydown',function(e){
    if(e.key==='Enter') document.getElementById('btnCreate').click();
  });

  // ESC closes modals
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'){
      closeMarket();
      document.getElementById('usrOvl').classList.remove('open');
      closeDD();
    }
  });

  // Init price
  onPriceChange();
});

connect();
setTimeout(function(){ if(!Object.keys(mkts).length) loadHttp(); },2000);
</script>
</body>
</html>"""