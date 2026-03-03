# -*- coding: utf-8 -*-
"""
BetX2 — prediction market с ордербуком (Polymarket-style)
Run: uvicorn betx2_poly:app --host 0.0.0.0 --port 8000
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

users     = {}
markets   = {}
orders    = {}
trades    = {}
activity  = []
ws_clients: List[WebSocket] = []
CONFIG    = {"commission_pct": 0.02}
SERVICE_BALANCE = {"total": 0.0}
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

def sweep_crossed(mid):
    while True:
        open_orders = get_open_orders(mid)
        bids = sorted([o for o in open_orders if o["side"]=="BUY"],  key=lambda x: -x["price"])
        asks = sorted([o for o in open_orders if o["side"]=="SELL"], key=lambda x:  x["price"])
        if not bids or not asks: break
        best_bid = bids[0]
        best_ask = asks[0]
        if best_bid["price"] < best_ask["price"]: break
        if best_bid["user_id"] == best_ask["user_id"]: break
        exec_price = best_ask["price"]
        bid_rem = round(best_bid["contracts"] - best_bid["filled"], 4)
        ask_rem = round(best_ask["contracts"] - best_ask["filled"], 4)
        match_qty = round(min(bid_rem, ask_rem), 4)
        if match_qty < 0.0001: break
        buy_user  = users[best_bid["user_id"]]
        sell_user = users[best_ask["user_id"]]
        buyer_overpay = round((best_bid["price"] - exec_price) / 100 * match_qty, 4)
        if buyer_overpay > 0:
            buy_user["balance"] = round(buy_user["balance"] + buyer_overpay, 4)
        best_bid["filled"] = round(best_bid["filled"] + match_qty, 4)
        best_ask["filled"] = round(best_ask["filled"] + match_qty, 4)
        for o in [best_bid, best_ask]:
            if round(o["contracts"] - o["filled"], 4) < 0.0001:
                o["status"] = "filled"
        tid = gen_id()
        buyer_cost  = round(exec_price / 100 * match_qty, 4)
        seller_cost = round((100 - exec_price) / 100 * match_qty, 4)
        trades[tid] = {"id": tid, "market_id": mid, "price": exec_price, "contracts": match_qty,
                       "buyer_cost": buyer_cost, "seller_cost": seller_cost,
                       "buy_user_id": best_bid["user_id"], "sell_user_id": best_ask["user_id"],
                       "buy_user_name": buy_user["name"], "sell_user_name": sell_user["name"],
                       "created_at": now_ts()}
        markets[mid]["last_price"] = exec_price

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
                          and o["id"] != new_oid and o["user_id"] != new_ord["user_id"]]
            if not candidates: break
            opp = min(candidates, key=lambda x: x["price"])
            exec_price = opp["price"]
        else:
            candidates = [o for o in get_open_orders(mid)
                          if o["side"] == "BUY" and o["price"] >= new_ord["price"]
                          and o["id"] != new_oid and o["user_id"] != new_ord["user_id"]]
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
             "price":     exec_price, "contracts": match_qty,
             "buyer_cost":  buyer_cost, "seller_cost": seller_cost,
             "created_at": now_ts()}
        trades[tid] = t
        new_trades.append(t)
        buy_name  = users[buy_ord["user_id"]]["name"]
        sell_name = users[sell_ord["user_id"]]["name"]
        add_act("trade", f"Сделка @ {exec_price}¢ × {match_qty:.1f} | {buy_name} ↑ / {sell_name} ↓ | {markets[mid]['title'][:35]}")
    return new_trades

# ══════════════════════════════════════════════
# NET POSITION CALCULATION
# Нетто-позиция: YES контракты - проданные YES
# WAP при усреднении
# ══════════════════════════════════════════════
def calc_net_positions(user_id):
    """
    Возвращает нетто-позиции по каждому рынку.
    
    Логика:
    - BUY YES trade  → +contracts к YES позиции, усредняем WAP
    - SELL YES trade → -contracts от YES позиции (закрытие лонга)
    - BUY YES на закрытие NO → -contracts от NO позиции  
    - SELL YES на открытие NO → +contracts к NO позиции
    
    Упрощённо:
    - buyer (купил YES) = длинная позиция по YES
    - seller (купил NO) = длинная позиция по NO
    - Когда seller продаёт → закрывает NO позицию
    - Когда buyer продаёт → закрывает YES позицию
    """
    positions = {}  # mid -> {yes_qty, yes_wap, no_qty, no_wap}
    
    # Сортируем трейды по времени для правильного FIFO
    sorted_trades = sorted(trades.values(), key=lambda x: x["created_at"])
    
    for t in sorted_trades:
        mid = t["market_id"]
        if mid not in positions:
            positions[mid] = {
                "yes_qty": 0.0, "yes_wap": 0.0, "yes_cost": 0.0,
                "no_qty":  0.0, "no_wap":  0.0, "no_cost":  0.0,
            }
        p = positions[mid]
        qty = t["contracts"]
        price = t["price"]  # цена YES (0-100)
        
        if t["buy_user_id"] == user_id:
            # Мы купили YES контракты
            if p["yes_qty"] < -0.0001:
                # У нас была короткая YES позиция — закрываем её
                # (теоретически не должно быть в этой модели, но на всякий)
                close_qty = min(qty, abs(p["yes_qty"]))
                p["yes_qty"] = round(p["yes_qty"] + close_qty, 4)
                qty -= close_qty
            if qty > 0.0001:
                # Открываем/увеличиваем лонг YES
                new_cost = round(p["yes_cost"] + qty * price / 100, 4)
                p["yes_qty"] = round(p["yes_qty"] + qty, 4)
                p["yes_cost"] = new_cost
                p["yes_wap"] = round(p["yes_cost"] / p["yes_qty"] * 100, 2) if p["yes_qty"] > 0 else 0
                
        if t["sell_user_id"] == user_id:
            # Мы продали YES → купили NO контракты
            if p["no_qty"] < -0.0001:
                close_qty = min(qty, abs(p["no_qty"]))
                p["no_qty"] = round(p["no_qty"] + close_qty, 4)
                qty -= close_qty
            if qty > 0.0001:
                no_price = 100 - price  # цена NO
                new_cost = round(p["no_cost"] + qty * no_price / 100, 4)
                p["no_qty"] = round(p["no_qty"] + qty, 4)
                p["no_cost"] = new_cost
                p["no_wap"] = round(p["no_cost"] / p["no_qty"] * 100, 2) if p["no_qty"] > 0 else 0

    # Теперь учтём что продажа YES (SELL ордер) закрывает YES позицию
    # и покупка YES (BUY ордер) закрывает NO позицию
    # Пересчитаем с учётом направления: нужно трекать SELL-трейды как закрытие
    
    # Сбрасываем и пересчитываем правильно
    positions = {}
    
    for t in sorted_trades:
        mid = t["market_id"]
        if mid not in positions:
            positions[mid] = {
                "yes_qty": 0.0, "yes_wap": 0.0, "yes_cost": 0.0,
                "no_qty":  0.0, "no_wap":  0.0, "no_cost":  0.0,
                "realized_pnl": 0.0,
            }
        p = positions[mid]
        qty = t["contracts"]
        yes_price = t["price"]
        no_price = round(100 - yes_price, 1)
        
        if t["buy_user_id"] == user_id:
            # Купили YES контракты
            # Проверяем: есть ли у нас NO позиция? Если да — закрываем её
            if p["no_qty"] > 0.0001:
                close_qty = min(qty, p["no_qty"])
                # Реализованный PnL NO позиции при закрытии
                # NO позиция выигрывает если YES идёт вниз
                # Закрываем NO через покупку YES
                entry_no_price = p["no_wap"]  # цена NO при входе
                current_no_price = no_price    # текущая цена NO
                realized = round(close_qty * (current_no_price - entry_no_price) / 100, 4)
                p["realized_pnl"] = round(p["realized_pnl"] + realized, 4)
                
                p["no_cost"] = round(p["no_cost"] * (p["no_qty"] - close_qty) / p["no_qty"], 4) if p["no_qty"] > 0.0001 else 0
                p["no_qty"] = round(p["no_qty"] - close_qty, 4)
                if p["no_qty"] < 0.0001:
                    p["no_qty"] = 0.0; p["no_wap"] = 0.0; p["no_cost"] = 0.0
                else:
                    p["no_wap"] = round(p["no_cost"] / p["no_qty"] * 100, 2)
                qty -= close_qty
            
            if qty > 0.0001:
                # Открываем/увеличиваем YES лонг
                new_qty = round(p["yes_qty"] + qty, 4)
                new_cost = round(p["yes_cost"] + qty * yes_price / 100, 4)
                p["yes_qty"] = new_qty
                p["yes_cost"] = new_cost
                p["yes_wap"] = round(new_cost / new_qty * 100, 2)
        
        if t["sell_user_id"] == user_id:
            # Продали YES → открываем/увеличиваем NO, или закрываем YES лонг
            if p["yes_qty"] > 0.0001:
                close_qty = min(qty, p["yes_qty"])
                # Реализованный PnL YES позиции
                entry_yes_price = p["yes_wap"]
                current_yes_price = yes_price
                realized = round(close_qty * (current_yes_price - entry_yes_price) / 100, 4)
                p["realized_pnl"] = round(p["realized_pnl"] + realized, 4)
                
                p["yes_cost"] = round(p["yes_cost"] * (p["yes_qty"] - close_qty) / p["yes_qty"], 4) if p["yes_qty"] > 0.0001 else 0
                p["yes_qty"] = round(p["yes_qty"] - close_qty, 4)
                if p["yes_qty"] < 0.0001:
                    p["yes_qty"] = 0.0; p["yes_wap"] = 0.0; p["yes_cost"] = 0.0
                else:
                    p["yes_wap"] = round(p["yes_cost"] / p["yes_qty"] * 100, 2)
                qty -= close_qty
            
            if qty > 0.0001:
                # Открываем/увеличиваем NO лонг
                new_qty = round(p["no_qty"] + qty, 4)
                new_cost = round(p["no_cost"] + qty * no_price / 100, 4)
                p["no_qty"] = new_qty
                p["no_cost"] = new_cost
                p["no_wap"] = round(new_cost / new_qty * 100, 2)
    
    return positions

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

class CreateUser(BaseModel):  name: str; balance: float = 5000.0
class CreateMarket(BaseModel): title: str; desc: str = ""; category: str = "Другое"
class PlaceOrder(BaseModel):
    user_id: str; market_id: str
    side:  str; price: float; dollars: float
class CancelOrder(BaseModel): order_id: str; user_id: str
class ResolveMarket(BaseModel): market_id: str; outcome: str
class UpdateConfig(BaseModel): commission_pct: float
class ClosePosition(BaseModel):
    user_id: str; market_id: str
    side: str  # "YES" or "NO" — which position to close

@app.get("/api/state")
async def get_state():
    return {"users": list(users.values()),
            "markets": [market_snap(m) for m in markets],
            "activity": list(reversed(activity[-60:])),
            "config": CONFIG,
            "service_balance": SERVICE_BALANCE["total"]}

@app.post("/api/users")
async def create_user(body: CreateUser):
    uid = gen_id()
    u = {"id": uid, "name": body.name.strip()[:24],
         "balance": round(body.balance, 2), "color": COLORS[len(users) % len(COLORS)]}
    users[uid] = u
    ev = add_act("user", f"Новый участник: {u['name']}")
    await broadcast({"type": "users", "data": list(users.values())})
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
    await broadcast({"type": "markets", "data": [market_snap(m) for m in markets]})
    await broadcast({"type": "activity", "data": ev})
    return market_snap(mid)

@app.post("/api/order")
async def place_order(body: PlaceOrder):
    if body.user_id   not in users:   raise HTTPException(404, "Пользователь не найден")
    if body.market_id not in markets: raise HTTPException(404, "Рынок не найден")
    if markets[body.market_id]["status"] != "open": raise HTTPException(400, "Рынок закрыт")
    if body.side not in ("BUY","SELL"): raise HTTPException(400, "side: BUY или SELL")
    price = round(body.price * 10) / 10
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
    add_act("order", f"{u['name']} → {'YES (BUY)' if body.side=='BUY' else 'NO (SELL)'} @ {price}¢ × {contracts:.1f}c (${cost:.2f}) | {markets[body.market_id]['title'][:35]}")
    new_trades = do_match(body.market_id, oid)
    sweep_crossed(body.market_id)
    await broadcast({"type": "markets", "data": [market_snap(m) for m in markets]})
    await broadcast({"type": "users",   "data": list(users.values())})
    await broadcast({"type": "activity","data": activity[-1]})
    o = orders[oid]
    return {"order_id": oid, "contracts": contracts, "price": price,
            "cost": cost, "filled": o["filled"], "status": o["status"],
            "trades": len(new_trades)}

@app.post("/api/close_position")
async def close_position(body: ClosePosition):
    """
    Закрыть позицию по маркету.
    YES позиция закрывается через SELL по лучшему биду.
    NO позиция закрывается через BUY по лучшему аску.
    """
    if body.user_id not in users: raise HTTPException(404, "Пользователь не найден")
    if body.market_id not in markets: raise HTTPException(404, "Рынок не найден")
    if markets[body.market_id]["status"] != "open": raise HTTPException(400, "Рынок закрыт")
    
    positions = calc_net_positions(body.user_id)
    pos = positions.get(body.market_id)
    
    if not pos:
        raise HTTPException(400, "Нет позиции")
    
    ob = get_orderbook(body.market_id)
    
    if body.side == "YES":
        # Закрыть LONG YES = продать YES контракты по лучшему биду
        # Механика: SELL ордер требует залог (NO-сторону), но у нас уже есть YES контракты.
        # Мы используем dollars=qty*bid/100 как условный размер, contracts выводятся из этого.
        # Залог продавца = contracts*(100-price)/100 = qty*(100-bid)/100 будет списан,
        # но при матче seller_cost возвращается обратно через do_match.
        # Итого пользователь получает: bid/100 * qty (стоимость YES по биду).
        qty = pos["yes_qty"]
        if qty < 0.0001:
            raise HTTPException(400, "Нет YES позиции для закрытия")
        if not ob["bids"]:
            raise HTTPException(400, "Нет покупателей в стакане (bid)")
        price = ob["bids"][0]["price"]
        side = "SELL"
        contracts = round(qty, 4)
        # Залог продавца NO-стороны (будет возвращён при исполнении через seller_cost refund)
        cost = round(contracts * (100 - price) / 100, 4)
    else:
        # Закрыть LONG NO = купить YES контракты по лучшему аску
        # Механика: BUY ордер требует оплату YES-стороны.
        # Мы тратим qty * ask/100 на покупку YES — это закрывает NO позицию.
        qty = pos["no_qty"]
        if qty < 0.0001:
            raise HTTPException(400, "Нет NO позиции для закрытия")
        if not ob["asks"]:
            raise HTTPException(400, "Нет продавцов в стакане (ask)")
        price = ob["asks"][0]["price"]
        side = "BUY"
        contracts = round(qty, 4)
        cost = round(contracts * price / 100, 4)  # стоимость покупки YES по аску
    
    u = users[body.user_id]
    if contracts < 0.0001:
        raise HTTPException(400, f"Позиция слишком маленькая: {qty:.8f} контрактов")
    if u["balance"] < cost:
        raise HTTPException(400, f"Недостаточно средств: ${u['balance']:.2f}, нужно: ${cost:.4f}")
    u["balance"] = round(u["balance"] - cost, 4)
    
    oid = gen_id()
    orders[oid] = {"id": oid, "market_id": body.market_id, "user_id": body.user_id,
                   "side": side, "price": price, "contracts": contracts,
                   "filled": 0.0, "status": "open", "cost": cost, "created_at": now_ts()}
    
    new_trades = do_match(body.market_id, oid)
    sweep_crossed(body.market_id)
    
    o = orders[oid]
    filled = o["filled"]
    
    # Если ордер не исполнился — отменяем и возвращаем деньги
    if filled < 0.0001:
        o["status"] = "cancelled"
        u["balance"] = round(u["balance"] + cost, 4)
        raise HTTPException(400, "Ордер не исполнился — цена в стакане изменилась")
    
    # Частичный рефанд если заполнился частично
    if o["status"] == "open":
        rem = contracts - filled
        refund = round(rem * price / 100, 4) if side == "BUY" else round(rem * (100 - price) / 100, 4)
        o["status"] = "cancelled"
        u["balance"] = round(u["balance"] + refund, 4)
    
    add_act("trade", f"{u['name']} закрыл {'YES' if body.side=='YES' else 'NO'} @ {price}¢ × {filled:.2f} | {markets[body.market_id]['title'][:35]}")
    
    await broadcast({"type": "markets", "data": [market_snap(m) for m in markets]})
    await broadcast({"type": "users",   "data": list(users.values())})
    await broadcast({"type": "activity","data": activity[-1]})
    
    return {
        "ok": True,
        "filled": filled,
        "price": price,
        "trades": len(new_trades),
        "side": body.side,
    }

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
    await broadcast({"type": "markets", "data": [market_snap(m) for m in markets]})
    await broadcast({"type": "users",   "data": list(users.values())})
    return {"cancelled": True, "refund": refund}

@app.post("/api/resolve")
async def resolve_market(body: ResolveMarket):
    if body.market_id not in markets: raise HTTPException(404, "Рынок не найден")
    m = markets[body.market_id]
    if m["status"] == "resolved":    raise HTTPException(400, "Уже разрешён")
    if body.outcome not in ("YES","NO"): raise HTTPException(400, "outcome: YES или NO")
    m["status"] = "resolved"; m["outcome"] = body.outcome
    refunds = {}
    for o in orders.values():
        if o["market_id"] != body.market_id or o["status"] != "open": continue
        o["status"] = "cancelled"
        rem = o["contracts"] - o["filled"]
        if rem > 0:
            ref = round(rem * o["price"] / 100, 4) if o["side"] == "BUY" else round(rem * (100 - o["price"]) / 100, 4)
            users[o["user_id"]]["balance"] = round(users[o["user_id"]]["balance"] + ref, 4)
            refunds[o["user_id"]] = round(refunds.get(o["user_id"], 0) + ref, 4)
    payouts = {}
    comm = CONFIG["commission_pct"]
    total_commission = 0.0
    for t in trades.values():
        if t["market_id"] != body.market_id: continue
        winner_uid = t["buy_user_id"] if body.outcome == "YES" else t["sell_user_id"]
        contracts  = t["contracts"]
        gross = round(contracts * 1.0, 4)
        fee   = round(gross * comm, 4)
        net   = round(gross - fee, 4)
        payouts[winner_uid] = round(payouts.get(winner_uid, 0) + net, 4)
        total_commission = round(total_commission + fee, 4)
    for uid, amt in payouts.items():
        if uid in users:
            users[uid]["balance"] = round(users[uid]["balance"] + amt, 4)
    SERVICE_BALANCE["total"] = round(SERVICE_BALANCE["total"] + total_commission, 4)
    total_payout = sum(payouts.values())
    lbl = "ДА" if body.outcome == "YES" else "НЕТ"
    ev  = add_act("resolve", f"Рынок «{m['title'][:40]}» → {lbl} | выплаты: ${total_payout:.2f} | комиссия: ${total_commission:.2f}")
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

@app.get("/api/price_history/{market_id}")
async def get_price_history(market_id: str):
    ts = sorted([t for t in trades.values() if t["market_id"]==market_id],
                key=lambda x: x["created_at"])
    return [{"price": t["price"], "ts": t["created_at"]} for t in ts]

@app.get("/api/trades/{market_id}")
async def get_trades(market_id: str):
    ts = get_market_trades(market_id, 50)
    return [{**t,
        "buy_user_name":  users.get(t["buy_user_id"],  {}).get("name", "?"),
        "sell_user_name": users.get(t["sell_user_id"], {}).get("name", "?"),
    } for t in ts]

@app.post("/api/config")
async def update_config(body: UpdateConfig):
    CONFIG["commission_pct"] = max(0, min(0.2, body.commission_pct))
    await broadcast({"type": "config", "data": CONFIG})
    return CONFIG

@app.get("/api/portfolio/{user_id}")
async def get_portfolio(user_id: str):
    if user_id not in users: raise HTTPException(404, "Не найден")
    
    net_positions = calc_net_positions(user_id)
    pending = [o for o in orders.values() if o["user_id"] == user_id and o["status"] == "open"]
    
    result = []
    for mid, pos in net_positions.items():
        if mid not in markets:
            continue
        m_snap = market_snap(mid)
        cur_yes_price = None
        if m_snap["last_price"] is not None:
            cur_yes_price = m_snap["last_price"]
        elif m_snap["best_bid"] and m_snap["best_ask"]:
            cur_yes_price = round((m_snap["best_bid"] + m_snap["best_ask"]) / 2, 1)
        elif m_snap["best_bid"]:
            cur_yes_price = m_snap["best_bid"]
        elif m_snap["best_ask"]:
            cur_yes_price = m_snap["best_ask"]
        
        # Unrealized PnL
        yes_upnl = None
        no_upnl  = None
        if cur_yes_price is not None:
            if pos["yes_qty"] > 0.001:
                yes_upnl = round(pos["yes_qty"] * (cur_yes_price - pos["yes_wap"]) / 100, 4)
            if pos["no_qty"] > 0.001:
                cur_no_price = round(100 - cur_yes_price, 1)
                no_upnl = round(pos["no_qty"] * (cur_no_price - pos["no_wap"]) / 100, 4)
        
        result.append({
            "market": m_snap,
            "yes_qty":   round(pos["yes_qty"], 4),
            "yes_wap":   pos["yes_wap"],
            "yes_cost":  round(pos["yes_cost"], 4),
            "yes_upnl":  yes_upnl,
            "no_qty":    round(pos["no_qty"], 4),
            "no_wap":    pos["no_wap"],
            "no_cost":   round(pos["no_cost"], 4),
            "no_upnl":   no_upnl,
            "realized_pnl": round(pos["realized_pnl"], 4),
            "cur_yes_price": cur_yes_price,
        })
    
    return {"positions": result, "pending_orders": pending}

@app.post("/api/reset")
async def reset_state():
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
    ev = add_act("market", "🔄 Сброс выполнен")
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

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BetX2 — Prediction Markets</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0d0e12;
  --surface:#161820;
  --surface2:#1e2028;
  --surface3:#262830;
  --border:#2a2d3a;
  --border2:#343748;
  --yes:#00c07a;
  --yes-dim:rgba(0,192,122,.12);
  --yes-glow:rgba(0,192,122,.25);
  --no:#f23645;
  --no-dim:rgba(242,54,69,.12);
  --no-glow:rgba(242,54,69,.25);
  --accent:#5b6af0;
  --accent-dim:rgba(91,106,240,.15);
  --gold:#f5a623;
  --gold-dim:rgba(245,166,35,.12);
  --text:#e8eaf0;
  --text2:#8890a4;
  --text3:#4a5068;
  --mono:'DM Mono',monospace;
  --sans:'DM Sans',sans-serif;
  --r:10px;
  --r2:8px;
}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;display:flex;flex-direction:column}

/* ═══ TOP NAV ═══ */
.topnav{height:48px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:0;padding:0 16px;flex-shrink:0;z-index:100}
.logo{font-family:var(--mono);font-size:13px;font-weight:500;color:var(--text);letter-spacing:.5px;margin-right:24px;white-space:nowrap}
.logo span{color:var(--yes)}
.nav-tabs{display:flex;gap:0;height:100%;align-items:center}
.nav-tab{padding:0 14px;height:100%;display:flex;align-items:center;font-size:12px;font-weight:500;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;transition:.15s;letter-spacing:.2px;white-space:nowrap}
.nav-tab:hover{color:var(--text)}
.nav-tab.active{color:var(--text);border-bottom-color:var(--accent)}
.nav-sp{flex:1}
.svc-chip{display:flex;align-items:center;gap:6px;padding:5px 10px;background:var(--gold-dim);border:1px solid rgba(245,166,35,.2);border-radius:6px;font-size:11px;font-family:var(--mono);margin-right:10px}
.svc-chip span:first-child{color:var(--text3);font-size:10px}
.svc-chip span:last-child{color:var(--gold);font-weight:500}
.ws-dot{width:6px;height:6px;border-radius:50%;background:var(--text3);margin-right:10px;transition:.4s;flex-shrink:0}
.ws-dot.on{background:var(--yes);box-shadow:0 0 6px var(--yes)}
.upill{display:flex;align-items:center;gap:7px;padding:4px 10px 4px 6px;background:var(--surface2);border:1px solid var(--border);border-radius:20px;cursor:pointer;user-select:none;transition:.15s;position:relative}
.upill:hover{border-color:var(--border2)}
.uav{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:10px;color:#fff;flex-shrink:0}
.upill-name{font-size:12px;font-weight:600}
.upill-bal{font-family:var(--mono);font-size:10px;color:var(--yes)}
.upill-caret{color:var(--text3);font-size:8px;transition:.2s}
.upill.open .upill-caret{transform:rotate(180deg)}

.dd{position:fixed;min-width:200px;background:var(--surface2);border:1px solid var(--border2);border-radius:var(--r);padding:4px;z-index:9999;opacity:0;pointer-events:none;transform:translateY(-4px);transition:opacity .15s,transform .15s;box-shadow:0 16px 48px rgba(0,0,0,.8)}
.dd.open{opacity:1;pointer-events:all;transform:none}
.dd-sep{padding:6px 10px 3px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--text3)}
.dd-item{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:6px;cursor:pointer;transition:.1s}
.dd-item:hover,.dd-item.sel{background:var(--surface3)}
.dd-item .dav{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;color:#fff;flex-shrink:0}
.dd-item .dn{font-size:12px;font-weight:600;flex:1}
.dd-item .db{font-family:var(--mono);font-size:10px;color:var(--yes)}
.dd-new{display:flex;align-items:center;gap:7px;padding:7px 10px;border-radius:6px;cursor:pointer;color:var(--accent);font-size:12px;font-weight:600;border-top:1px solid var(--border);margin-top:3px;transition:.1s}
.dd-new:hover{background:var(--accent-dim)}

/* ═══ MAIN LAYOUT ═══ */
.main{flex:1;display:flex;overflow:hidden;min-height:0}
.panel{display:none;width:100%;overflow:hidden}
.panel.active{display:flex;flex-direction:column}

/* ═══ MARKETS LIST ═══ */
.markets-view{flex:1;overflow-y:auto;padding:16px;position:relative}
#panel-markets{position:relative}
.markets-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.mcard{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;cursor:pointer;transition:.18s}
.mcard:hover{border-color:var(--border2);background:var(--surface2);transform:translateY(-1px)}
.mcard.resolved{opacity:.5;cursor:default;filter:grayscale(.6)}
.mcard-body{padding:14px}
.mcard-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.mcard-cat{font-size:10px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--text3)}
.mcard-badge{font-size:9px;font-weight:700;padding:2px 7px;border-radius:20px;letter-spacing:.5px}
.mcard-badge.open{background:var(--yes-dim);color:var(--yes);border:1px solid var(--yes-glow)}
.mcard-badge.ry{background:var(--yes-dim);color:var(--yes);border:1px solid var(--yes-glow)}
.mcard-badge.rn{background:var(--no-dim);color:var(--no);border:1px solid var(--no-glow)}
.mcard-title{font-size:14px;font-weight:600;line-height:1.4;color:var(--text);margin-bottom:14px}
.prob-bar{height:4px;background:var(--surface3);border-radius:2px;margin-bottom:10px;overflow:hidden}
.prob-bar-fill{height:100%;border-radius:2px;transition:.4s;background:linear-gradient(90deg,var(--yes),#00e8a0)}
.prob-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.prob-yes,.prob-no{display:flex;align-items:baseline;gap:5px}
.prob-pct{font-family:var(--mono);font-size:22px;font-weight:500;line-height:1}
.prob-yes .prob-pct{color:var(--yes)}
.prob-no .prob-pct{color:var(--no)}
.prob-lbl{font-size:10px;font-weight:600;letter-spacing:.5px;text-transform:uppercase}
.prob-yes .prob-lbl{color:rgba(0,192,122,.6)}
.prob-no .prob-lbl{color:rgba(242,54,69,.6)}
.mcard-footer{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);font-family:var(--mono);padding-top:10px;border-top:1px solid var(--border)}

/* ═══ MARKET DETAIL ═══ */
.market-view{position:absolute;inset:0;display:none;flex-direction:column;overflow:hidden;background:var(--bg);z-index:10}
.market-view.open{display:flex}
.mv-header{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0;background:var(--surface);min-height:48px}
.mv-back{width:28px;height:28px;border-radius:6px;background:var(--surface2);border:1px solid var(--border);color:var(--text2);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0;transition:.1s}
.mv-back:hover{background:var(--surface3);color:var(--text)}
.mv-market-name{font-size:14px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mv-prob-big{display:flex;align-items:baseline;gap:6px;flex-shrink:0}
.mv-prob-big .pv{font-family:var(--mono);font-size:24px;font-weight:500;color:var(--text)}
.mv-prob-big .pc{font-size:11px;color:var(--text3)}
.mv-resolve-btn{padding:4px 10px;border-radius:6px;border:none;cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:500;transition:.15s}
.mv-resolve-btn.y{background:var(--yes-dim);color:var(--yes);border:1px solid var(--yes-glow)}
.mv-resolve-btn.n{background:var(--no-dim);color:var(--no);border:1px solid var(--no-glow)}
.mv-resolve-btn:hover{filter:brightness(1.15)}
.mv-resolved-banner{padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;font-family:var(--mono)}
.mv-resolved-banner.yes{background:var(--yes-dim);color:var(--yes);border:1px solid var(--yes-glow)}
.mv-resolved-banner.no{background:var(--no-dim);color:var(--no);border:1px solid var(--no-glow)}

.mv-content{flex:1;display:flex;flex-direction:row;overflow:hidden;min-height:0}
.mv-left{display:flex;flex-direction:column;overflow:hidden;min-width:0;flex:1;border-right:1px solid var(--border)}
.mv-chart{height:180px;flex-shrink:0;position:relative;background:var(--bg);border-bottom:1px solid var(--border);overflow:hidden}
.mv-chart canvas{position:absolute;inset:0;width:100%;height:100%}
.mv-chart-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--text3);font-family:var(--mono);pointer-events:none}
.chart-tabs{position:absolute;top:8px;right:8px;display:flex;gap:2px;z-index:2}
.chart-tab{padding:2px 7px;border-radius:4px;font-size:9px;font-family:var(--mono);font-weight:500;color:var(--text3);cursor:pointer;border:1px solid transparent;transition:.1s}
.chart-tab.active,.chart-tab:hover{background:var(--surface2);border-color:var(--border);color:var(--text)}
.mv-book-row{display:flex;flex:1;min-height:0;overflow:hidden}
.mv-ob{display:flex;flex-direction:column;width:300px;flex-shrink:0;border-right:1px solid var(--border);overflow:hidden}
.mv-trades{flex:1;display:flex;flex-direction:column;overflow:hidden}
.section-hdr{padding:8px 12px;border-bottom:1px solid var(--border);font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--text3);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.section-hdr-btn{font-size:9px;color:var(--accent);cursor:pointer;padding:2px 6px;border:1px solid var(--accent-dim);border-radius:4px;background:var(--accent-dim);font-family:var(--mono);font-weight:500;transition:.1s}
.section-hdr-btn:hover{filter:brightness(1.2)}
.ob-cols{display:grid;grid-template-columns:55px 1fr 1fr 1fr;padding:5px 12px;font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text3);font-family:var(--mono);flex-shrink:0}
.ob-asks-zone{overflow-y:auto;flex:1;display:flex;flex-direction:column-reverse;min-height:0}
.ob-bids-zone{overflow-y:auto;flex:1;min-height:0}
.ob-mid-line{padding:5px 12px;background:var(--surface2);border-top:1px solid var(--border);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;flex-shrink:0}
.ob-mid-price{font-size:14px;font-weight:500;color:var(--text)}
.ob-mid-spread{font-size:9px;color:var(--text3)}
.ob-row{display:grid;grid-template-columns:55px 1fr 1fr 1fr;padding:3px 12px;cursor:pointer;position:relative;transition:.08s;font-family:var(--mono);font-size:11px}
.ob-row:hover{background:var(--surface2)!important}
.ob-row .ob-depth{position:absolute;top:0;bottom:0;right:0;opacity:.08;pointer-events:none;transition:width .3s}
.ob-ask-row .ob-depth{background:var(--no)}
.ob-bid-row .ob-depth{background:var(--yes)}
.ob-ask-row{border-left:2px solid rgba(242,54,69,.3)}
.ob-bid-row{border-left:2px solid rgba(0,192,122,.3)}
.ob-ask-row:hover{border-left-color:var(--no)!important}
.ob-bid-row:hover{border-left-color:var(--yes)!important}
.ob-ask-row .ob-p{color:var(--no)}
.ob-bid-row .ob-p{color:var(--yes)}
.ob-col{text-align:right}
.ob-col:first-child{text-align:left}
@keyframes flash-ask{0%{background:rgba(242,54,69,.25)}100%{background:transparent}}
@keyframes flash-bid{0%{background:rgba(0,192,122,.25)}100%{background:transparent}}
.ob-row.flash-ask{animation:flash-ask .5s}
.ob-row.flash-bid{animation:flash-bid .5s}
.trades-scroll{flex:1;overflow-y:auto;min-height:0}
.trade-row{display:grid;grid-template-columns:50px 70px 1fr 50px;gap:6px;padding:4px 12px;font-family:var(--mono);font-size:10px;border-bottom:1px solid rgba(255,255,255,.03);align-items:center;transition:.08s}
.trade-row:hover{background:var(--surface2)}
.trade-p{font-size:12px;font-weight:500}
.trade-q{color:var(--text3)}
.trade-names{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text2)}
.trade-ts{color:var(--text3);text-align:right;font-size:9px}
.trades-cols{display:grid;grid-template-columns:50px 70px 1fr 50px;gap:6px;padding:5px 12px;font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text3);font-family:var(--mono);flex-shrink:0}

/* ═══ RIGHT: ORDER FORM ═══ */
.mv-right{width:280px;flex-shrink:0;display:flex;flex-direction:column;overflow-y:auto;background:var(--surface);border-left:1px solid var(--border)}
.of-wrap{padding:14px}
.of-outcome{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:14px}
.of-outcome-btn{padding:10px;border-radius:8px;border:2px solid var(--border);background:transparent;cursor:pointer;text-align:center;transition:.15s;font-family:var(--sans)}
.of-outcome-btn.yes.active{border-color:var(--yes);background:var(--yes-dim)}
.of-outcome-btn.no.active{border-color:var(--no);background:var(--no-dim)}
.of-outcome-btn:not(.active){opacity:.6}
.of-outcome-btn:hover:not(.active){opacity:.85;background:var(--surface2)}
.of-outcome-label{font-size:11px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;margin-bottom:3px}
.of-outcome-btn.yes .of-outcome-label{color:var(--yes)}
.of-outcome-btn.no .of-outcome-label{color:var(--no)}
.of-outcome-price{font-family:var(--mono);font-size:16px;font-weight:500;color:var(--text)}
.of-outcome-odds{font-size:10px;color:var(--text2);font-family:var(--mono)}
.of-type{display:flex;gap:4px;margin-bottom:12px}
.of-type-btn{flex:1;padding:5px;border-radius:6px;border:1px solid var(--border);background:transparent;cursor:pointer;font-size:10px;font-weight:600;color:var(--text2);font-family:var(--mono);letter-spacing:.5px;transition:.1s;text-align:center}
.of-type-btn.active{background:var(--surface2);border-color:var(--border2);color:var(--text)}
.of-type-btn:hover:not(.active){color:var(--text);border-color:var(--border2)}
.of-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text3);margin-bottom:6px}
.of-amt-row{display:flex;align-items:center;background:var(--surface2);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:8px;transition:.15s}
.of-amt-row:focus-within{border-color:var(--border2)}
.of-amt-prefix{padding:0 10px;font-family:var(--mono);font-size:14px;font-weight:500;color:var(--text2)}
.of-amt-inp{flex:1;background:transparent;border:none;outline:none;font-family:var(--mono);font-size:16px;font-weight:500;color:var(--text);padding:10px 0}
.of-amt-inp::-webkit-outer-spin-button,.of-amt-inp::-webkit-inner-spin-button{-webkit-appearance:none}
.of-quick{display:flex;gap:4px;margin-bottom:14px;flex-wrap:wrap}
.of-q{padding:3px 8px;border-radius:20px;background:var(--surface2);border:1px solid var(--border);font-size:10px;font-weight:600;cursor:pointer;color:var(--text2);font-family:var(--mono);transition:.1s}
.of-q:hover{background:var(--surface3);color:var(--text)}
.of-price-row{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.of-price-inp{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:8px;outline:none;font-family:var(--mono);font-size:15px;font-weight:500;color:var(--text);padding:9px 11px;transition:.15s;-moz-appearance:textfield}
.of-price-inp::-webkit-outer-spin-button,.of-price-inp::-webkit-inner-spin-button{-webkit-appearance:none}
.of-price-inp:focus{border-color:var(--border2)}
.of-price-unit{font-size:11px;color:var(--text3);white-space:nowrap}
.of-slider{width:100%;-webkit-appearance:none;height:3px;border-radius:2px;background:var(--surface3);outline:none;margin-bottom:4px;cursor:pointer}
.of-slider.yes::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--yes);cursor:pointer}
.of-slider.no::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--no);cursor:pointer}
.of-slider-marks{display:flex;justify-content:space-between;font-size:8px;font-family:var(--mono);color:var(--text3);margin-bottom:10px}
.of-summary{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:12px}
.of-sum-row{display:flex;justify-content:space-between;align-items:center;padding:3px 0;font-size:11px}
.of-sum-row .sl{color:var(--text2)}
.of-sum-row .sv{font-family:var(--mono);font-weight:500}
.of-sum-divider{height:1px;background:var(--border);margin:6px 0}
.of-sum-payout{display:flex;justify-content:space-between;align-items:center;padding:3px 0}
.of-sum-payout .sl{font-weight:600;color:var(--text);font-size:12px}
.of-sum-payout .sv{font-family:var(--mono);font-size:16px;font-weight:500;color:var(--yes)}
.of-sum-profit{font-size:10px;color:var(--text3);text-align:right;font-family:var(--mono)}
.of-market-info{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px}
.of-market-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-size:11px}
.of-market-row:last-child{margin-bottom:0}
.of-mkt-warn{font-size:10px;color:var(--gold);padding:6px 8px;background:var(--gold-dim);border-radius:6px;margin-bottom:8px;border:1px solid rgba(245,166,35,.15)}
.of-submit{width:100%;padding:12px;border-radius:8px;border:none;cursor:pointer;font-family:var(--sans);font-size:13px;font-weight:700;transition:.18s;letter-spacing:.3px}
.of-submit.yes{background:var(--yes);color:#001a0d}
.of-submit.no{background:var(--no);color:#fff}
.of-submit:hover{filter:brightness(1.08)}
.of-submit:disabled{opacity:.4;pointer-events:none}

/* Bottom tabs */
.of-btabs{border-top:1px solid var(--border);margin-top:14px;flex-shrink:0}
.of-btabs-row{display:flex}
.of-btab{flex:1;padding:8px 6px;font-size:10px;font-weight:600;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;text-align:center;transition:.12s;letter-spacing:.3px}
.of-btab.active{color:var(--text);border-bottom-color:var(--accent)}
.of-btab:hover:not(.active){color:var(--text2)}
.of-bpanel{display:none;padding:10px 14px;overflow-y:auto;max-height:240px}
.of-bpanel.active{display:block}

/* ─── POSITION CARD (новый стиль) ─── */
.pos-card{background:var(--surface2);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:8px}
.pos-card-header{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border)}
.pos-badge{font-size:9px;font-weight:700;padding:3px 8px;border-radius:20px;letter-spacing:1px;text-transform:uppercase}
.pos-badge.long{background:var(--yes-dim);color:var(--yes);border:1px solid var(--yes-glow)}
.pos-badge.short{background:var(--no-dim);color:var(--no);border:1px solid var(--no-glow)}
.pos-pnl-chip{margin-left:auto;font-family:var(--mono);font-size:12px;font-weight:600}
.pos-pnl-chip.up{color:var(--yes)}.pos-pnl-chip.dn{color:var(--no)}.pos-pnl-chip.flat{color:var(--text3)}
.pos-grid{display:grid;grid-template-columns:1fr 1fr;gap:0;padding:0}
.pos-cell{padding:8px 12px;border-right:1px solid var(--border);border-bottom:1px solid var(--border)}
.pos-cell:nth-child(even){border-right:none}
.pos-cell:nth-last-child(-n+2){border-bottom:none}
.pos-cell-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.pos-cell-val{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--text)}
.pos-cell-val.up{color:var(--yes)}.pos-cell-val.dn{color:var(--no)}
.pos-close-row{padding:10px 12px;border-top:1px solid var(--border)}
.pos-close-btn{width:100%;padding:7px;border-radius:6px;border:1px solid var(--border2);background:transparent;color:var(--text2);font-size:10px;font-family:var(--mono);font-weight:500;cursor:pointer;transition:.15s;text-align:center}
.pos-close-btn:hover:not(:disabled){border-color:var(--no);color:var(--no);background:var(--no-dim)}
.pos-close-btn:disabled{opacity:.35;cursor:not-allowed}
.pos-no-data{font-size:10px;color:var(--text3);font-family:var(--mono);padding:4px 0}

/* Open orders */
.ord-entry{display:flex;align-items:center;gap:6px;padding:6px 0;border-bottom:1px solid var(--border);font-size:10px;font-family:var(--mono)}
.ord-entry:last-child{border:none}
.ord-cancel-btn{margin-left:auto;padding:2px 7px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--text3);font-size:9px;cursor:pointer;font-family:var(--mono);transition:.1s}
.ord-cancel-btn:hover{border-color:var(--no);color:var(--no)}

/* ═══ PORTFOLIO ═══ */
.port-view{flex:1;overflow-y:auto;padding:16px}
.port-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.port-stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px}
.port-stat-lbl{font-size:9px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--text3);margin-bottom:6px}
.port-stat-val{font-family:var(--mono);font-size:22px;font-weight:500}

/* Portfolio position card */
.pcard{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;margin-bottom:10px}
.pcard-hd{padding:12px 14px;display:flex;align-items:center;gap:10px;cursor:pointer;border-bottom:1px solid var(--border);transition:.1s}
.pcard-hd:hover{background:var(--surface2)}
.pcard-title{font-size:13px;font-weight:600;flex:1;line-height:1.35}
.pcard-pnl{font-family:var(--mono);font-size:13px;font-weight:500;white-space:nowrap}
.pcard-pnl.up{color:var(--yes)}.pcard-pnl.dn{color:var(--no)}.pcard-pnl.flat{color:var(--text3)}
.pcard-bd{padding:12px 14px}

/* Position legs in portfolio */
.leg{border-radius:8px;padding:12px;margin-bottom:8px;border-left:3px solid transparent}
.leg.long{background:rgba(0,192,122,.05);border-left-color:var(--yes)}
.leg.short{background:rgba(242,54,69,.05);border-left-color:var(--no)}
.leg-badge{display:inline-flex;padding:2px 8px;border-radius:20px;font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:8px}
.leg-badge.long{background:var(--yes-dim);color:var(--yes)}
.leg-badge.short{background:var(--no-dim);color:var(--no)}
.leg-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}
.leg-col .fl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:2px}
.leg-col .fv{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--text)}
.leg-col .fv.up{color:var(--yes)}.leg-col .fv.dn{color:var(--no)}
.leg-close{width:100%;padding:7px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--text2);cursor:pointer;font-size:10px;font-family:var(--mono);font-weight:500;transition:.15s}
.leg-close:hover:not(:disabled){border-color:var(--no);color:var(--no)}
.leg-close:disabled{opacity:.3;cursor:not-allowed}
.leg-settled{font-family:var(--mono);font-size:11px;font-weight:500;padding:3px 0}

/* ═══ SIMULATE ═══ */
.sim-view{flex:1;overflow-y:auto;padding:16px}
.sim-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
@media(max-width:640px){.sim-grid{grid-template-columns:1fr}}
.sim-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px}
.sim-card-title{font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--text3);margin-bottom:12px}
.sim-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;font-size:12px;color:var(--text2)}
.sim-inp{background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-family:var(--mono);font-size:12px;padding:4px 8px;outline:none;width:120px;text-align:right}
.sim-inp:focus{border-color:var(--border2)}
.sim-inp.sm{width:75px}
.sim-status{display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:8px;font-size:11px;font-family:var(--mono);margin-bottom:12px}
.sim-dot{width:6px;height:6px;border-radius:50%;background:var(--text3);flex-shrink:0}
.sim-dot.run{background:var(--yes);box-shadow:0 0 6px var(--yes);animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.sim-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.sim-stat{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 10px;text-align:center}
.sim-stat-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px}
.sim-stat-val{font-family:var(--mono);font-size:15px;font-weight:500}
.sim-actions{display:flex;gap:8px;margin-bottom:10px}
.sim-btn{flex:1;padding:9px;border-radius:8px;border:none;cursor:pointer;font-size:11px;font-weight:600;font-family:var(--mono);transition:.15s}
.sim-btn.start{background:var(--accent);color:#fff}
.sim-btn.stop{background:var(--no-dim);color:var(--no);border:1px solid var(--no-glow)}
.sim-btn.burst{background:var(--gold-dim);color:var(--gold);border:1px solid rgba(245,166,35,.2)}
.sim-btn:hover{filter:brightness(1.1)}
.sim-log{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 10px;height:90px;overflow-y:auto;font-size:10px;font-family:var(--mono);line-height:1.8}
.sim-log-entry{animation:fi .2s}
@keyframes fi{from{opacity:0}to{opacity:1}}
.sim-toggle{display:flex;align-items:center;gap:8px}
.sim-switch{position:relative;width:32px;height:18px;flex-shrink:0}
.sim-switch input{opacity:0;width:0;height:0}
.sim-slider-sw{position:absolute;cursor:pointer;inset:0;background:var(--border2);border-radius:20px;transition:.3s}
.sim-slider-sw:before{content:'';position:absolute;width:12px;height:12px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
.sim-switch input:checked+.sim-slider-sw{background:var(--accent)}
.sim-switch input:checked+.sim-slider-sw:before{transform:translateX(14px)}
.sim-bots{max-height:140px;overflow-y:auto;margin-bottom:8px}
.sim-bot-row{display:flex;align-items:center;gap:7px;padding:4px 0;border-bottom:1px solid var(--border);font-size:11px}
.sim-bot-row:last-child{border:none}
.sim-bot-av{width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:700;color:#fff;flex-shrink:0}

/* ═══ ACTIVITY ═══ */
.act-view{flex:1;overflow-y:auto;padding:16px}
.act-list{display:flex;flex-direction:column;gap:4px}
.act-item{display:flex;align-items:flex-start;gap:8px;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:8px;font-size:11px;animation:fi .18s}
.act-dot{width:5px;height:5px;border-radius:50%;flex-shrink:0;margin-top:4px}
.act-dot.trade{background:var(--yes)}.act-dot.order{background:var(--accent)}.act-dot.resolve{background:var(--gold)}.act-dot.market{background:var(--text2)}.act-dot.user{background:var(--no)}
.act-ts{font-size:9px;color:var(--text3);margin-left:auto;font-family:var(--mono);white-space:nowrap;padding-left:8px}

/* ═══ FORMS ═══ */
.form-view{flex:1;overflow-y:auto;padding:16px}
.fcard{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:20px;max-width:480px}
.fcard-title{font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--text3);margin-bottom:16px}
.fld{margin-bottom:12px}
.fld label{display:block;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text3);margin-bottom:5px}
.fld input,.fld textarea,.fld select{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-family:var(--sans);font-size:13px;padding:9px 12px;outline:none;transition:.15s}
.fld input:focus,.fld textarea:focus,.fld select:focus{border-color:var(--border2)}
.fld textarea{resize:vertical;min-height:60px}
.fld select option{background:var(--surface2)}
.btn-primary{padding:10px 20px;border-radius:8px;border:none;cursor:pointer;font-size:12px;font-weight:600;background:var(--accent);color:#fff;font-family:var(--sans);transition:.15s}
.btn-primary:hover{filter:brightness(1.1)}
.btn-danger{width:100%;margin-top:14px;padding:10px;border-radius:8px;border:1px solid rgba(242,54,69,.3);background:rgba(242,54,69,.07);color:var(--no);cursor:pointer;font-size:11px;font-weight:600;font-family:var(--mono);transition:.15s}
.btn-danger:hover{background:rgba(242,54,69,.15);border-color:var(--no)}
.svc-balance-box{background:var(--gold-dim);border:1px solid rgba(245,166,35,.2);border-radius:8px;padding:14px;margin-bottom:16px}
.svc-balance-box .bl{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:rgba(245,166,35,.7);margin-bottom:4px}
.svc-balance-box .bv{font-family:var(--mono);font-size:28px;font-weight:500;color:var(--gold)}

/* ═══ MODAL ═══ */
.modal-ovl{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:500;display:none;align-items:center;justify-content:center}
.modal-ovl.open{display:flex}
.modal-box{background:var(--surface);border:1px solid var(--border2);border-radius:14px;width:100%;max-width:360px;animation:pop .2s cubic-bezier(.34,1.56,.64,1)}
@keyframes pop{from{transform:scale(.95);opacity:0}to{transform:scale(1);opacity:1}}
.modal-hdr{display:flex;align-items:center;padding:14px 16px;border-bottom:1px solid var(--border)}
.modal-hdr-title{font-size:14px;font-weight:600;flex:1}
.modal-close{width:26px;height:26px;border-radius:6px;background:var(--surface2);border:1px solid var(--border);color:var(--text2);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:12px;transition:.1s}
.modal-close:hover{background:var(--surface3);color:var(--text)}
.modal-body{padding:16px}

/* ═══ TOASTS ═══ */
.toasts{position:fixed;bottom:16px;right:16px;z-index:999;display:flex;flex-direction:column;gap:5px;pointer-events:none}
.toast{padding:8px 13px;border-radius:8px;font-size:11px;font-weight:600;max-width:300px;animation:ti .22s cubic-bezier(.34,1.56,.64,1);box-shadow:0 8px 32px rgba(0,0,0,.8);font-family:var(--mono)}
@keyframes ti{from{transform:translateX(30px);opacity:0}to{transform:none;opacity:1}}
.toast.ok{background:var(--yes);color:#001a0d}.toast.err{background:var(--no);color:#fff}.toast.info{background:var(--surface2);color:var(--text);border:1px solid var(--border2)}

::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
::-webkit-scrollbar-thumb:hover{background:var(--text3)}

.no-user-bar{background:var(--accent-dim);border-bottom:1px solid rgba(91,106,240,.2);padding:6px 16px;font-size:11px;color:var(--accent);font-weight:500;display:none;flex-shrink:0}
.no-user-bar.show{display:block}
</style>
</head>
<body>

<nav class="topnav">
  <div class="logo">bet<span>X</span>2</div>
  <div class="nav-tabs" id="navTabs">
    <div class="nav-tab active" data-panel="markets">Markets</div>
    <div class="nav-tab" data-panel="portfolio">Portfolio</div>
    <div class="nav-tab" data-panel="activity">Activity</div>
    <div class="nav-tab" data-panel="simulate">Simulate</div>
    <div class="nav-tab" data-panel="create">+ Create</div>
    <div class="nav-tab" data-panel="config">Settings</div>
  </div>
  <div class="nav-sp"></div>
  <div class="svc-chip"><span>House</span><span id="svcBal">$0.00</span></div>
  <div class="ws-dot" id="wsDot"></div>
  <div class="upill" id="upill">
    <div class="uav" id="hAv" style="background:#333">?</div>
    <span class="upill-name" id="hName">Select user</span>
    <span class="upill-bal" id="hBal"></span>
    <span class="upill-caret">▾</span>
  </div>
</nav>

<div class="dd" id="dd">
  <div class="dd-sep">Participants</div>
  <div id="ddList"></div>
  <div class="dd-new" id="btnNewU">＋ New participant</div>
</div>

<div class="no-user-bar" id="noUserBar">👆 Select a participant in the top right to start trading</div>

<div class="main">

  <!-- MARKETS -->
  <div class="panel active" id="panel-markets">
    <div class="markets-view">
      <div class="markets-grid" id="mgrid"><div style="color:var(--text3);font-family:var(--mono);font-size:11px;padding:20px">Loading…</div></div>
    </div>
    <div class="market-view" id="marketView">
      <div class="mv-header">
        <button class="mv-back" id="btnBack">←</button>
        <div style="font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--text3);flex-shrink:0" id="dCat"></div>
        <div style="width:1px;height:12px;background:var(--border);flex-shrink:0;margin:0 8px"></div>
        <div class="mv-market-name" id="dTitle"></div>
        <div class="mv-prob-big" id="dPriceChip">
          <span class="pv" id="dPrice">—</span>
          <span class="pc" id="dOdds"></span>
        </div>
        <div id="dResArea" style="flex-shrink:0;display:flex;align-items:center;gap:6px"></div>
      </div>

      <div class="mv-content">
      <div class="mv-left">
        <div class="mv-chart" id="mvChart">
          <canvas id="priceCanvas"></canvas>
          <div class="mv-chart-empty" id="chartEmpty">No trades yet — run simulation to generate data</div>
          <div class="chart-tabs"><div class="chart-tab active">ALL</div></div>
        </div>
        <div class="mv-book-row">
          <div class="mv-ob">
            <div class="section-hdr">
              <span>Order Book</span>
              <button class="section-hdr-btn" id="obModeBtn" onclick="toggleOBMode()">ODDS</button>
            </div>
            <div class="ob-cols">
              <span id="obPriceLbl">PRICE</span>
              <span class="ob-col">QTY</span>
              <span class="ob-col">VALUE</span>
              <span class="ob-col">TOTAL</span>
            </div>
            <div class="ob-asks-zone"><div id="obAsks"></div></div>
            <div class="ob-mid-line" id="obMidLine">
              <span class="ob-mid-price" id="obMidP">—</span>
              <span class="ob-mid-spread" id="obMidS"></span>
              <span id="obExpandBtn" class="section-hdr-btn" style="margin-left:auto" onclick="toggleOBLevels()">▼ 20</span>
            </div>
            <div class="ob-bids-zone"><div id="obBids"></div></div>
          </div>
          <div class="mv-trades">
            <div class="section-hdr">Recent Trades</div>
            <div class="trades-cols">
              <span>PRICE</span><span>QTY</span><span>PARTICIPANTS</span><span style="text-align:right">TIME</span>
            </div>
            <div class="trades-scroll" id="tradesScroll"></div>
          </div>
        </div>
      </div>

      <!-- RIGHT: order form -->
      <div class="mv-right">
        <div class="of-wrap" id="ofWrap">
          <div class="of-outcome" id="ofOutcome">
            <button class="of-outcome-btn yes active" id="ofYes">
              <div class="of-outcome-label">YES</div>
              <div class="of-outcome-price" id="ofYesPrice">—</div>
              <div class="of-outcome-odds" id="ofYesOdds"></div>
            </button>
            <button class="of-outcome-btn no" id="ofNo">
              <div class="of-outcome-label">NO</div>
              <div class="of-outcome-price" id="ofNoPrice">—</div>
              <div class="of-outcome-odds" id="ofNoOdds"></div>
            </button>
          </div>

          <div class="of-type">
            <button class="of-type-btn active" id="otLimit">Limit</button>
            <button class="of-type-btn" id="otMarket">Market</button>
          </div>

          <!-- LIMIT FORM -->
          <div id="limitSection">
            <div class="of-label">Price</div>
            <div class="of-price-row">
              <input type="number" class="of-price-inp" id="priceInp" min="0.1" max="99.9" step="0.1" value="50">
              <span class="of-price-unit">¢ per share</span>
            </div>
            <input type="range" class="of-slider yes" id="priceSlider" min="1" max="99" step="1" value="50">
            <div class="of-slider-marks"><span>1¢</span><span>25¢</span><span>50¢</span><span>75¢</span><span>99¢</span></div>
            <div class="of-label">Amount</div>
            <div class="of-amt-row">
              <span class="of-amt-prefix">$</span>
              <input type="number" class="of-amt-inp" id="dollarInp" value="100" min="1">
            </div>
            <div class="of-quick">
              <button class="of-q" data-v="50">$50</button>
              <button class="of-q" data-v="100">$100</button>
              <button class="of-q" data-v="200">$200</button>
              <button class="of-q" data-v="500">$500</button>
              <button class="of-q" id="qMax">MAX</button>
            </div>
            <div class="of-summary" id="ofSummary">
              <div class="of-sum-row"><span class="sl">Avg price</span><span class="sv" id="sumPrice">—</span></div>
              <div class="of-sum-row"><span class="sl">Shares</span><span class="sv" id="sumShares">—</span></div>
              <div class="of-sum-row"><span class="sl">Est. cost</span><span class="sv" id="sumCost">—</span></div>
              <div class="of-sum-divider"></div>
              <div class="of-sum-payout">
                <span class="sl">Max payout</span>
                <span class="sv" id="sumPayout">—</span>
              </div>
              <div class="of-sum-profit" id="sumProfit">—</div>
            </div>
            <button class="of-submit yes" id="subBtn">Buy YES</button>
          </div>

          <!-- MARKET FORM -->
          <div id="marketSection" style="display:none">
            <div class="of-market-info" id="mktInfo">
              <div class="of-market-row">
                <span style="color:var(--text2)">Best Ask (Buy YES)</span>
                <span style="font-family:var(--mono);font-weight:500;color:var(--yes)" id="mktBestAsk">—</span>
              </div>
              <div class="of-market-row">
                <span style="color:var(--text2)">Best Bid (Buy NO)</span>
                <span style="font-family:var(--mono);font-weight:500;color:var(--no)" id="mktBestBid">—</span>
              </div>
            </div>
            <div class="of-label">Amount</div>
            <div class="of-amt-row">
              <span class="of-amt-prefix">$</span>
              <input type="number" class="of-amt-inp" id="mktDollarInp" value="100" min="1">
            </div>
            <div class="of-quick">
              <button class="of-q mktq" data-v="50">$50</button>
              <button class="of-q mktq" data-v="100">$100</button>
              <button class="of-q mktq" data-v="200">$200</button>
              <button class="of-q" id="mktqMax">MAX</button>
            </div>
            <div class="of-summary">
              <div class="of-sum-row"><span class="sl">Exec price</span><span class="sv" id="mktPvPrice" style="color:var(--yes)">—</span></div>
              <div class="of-sum-row"><span class="sl">Odds</span><span class="sv" id="mktPvOdds">—</span></div>
              <div class="of-sum-row"><span class="sl">Shares</span><span class="sv" id="mktPvShares">—</span></div>
              <div class="of-sum-divider"></div>
              <div class="of-sum-payout">
                <span class="sl">Max payout</span>
                <span class="sv" id="mktPvPayout">—</span>
              </div>
              <div class="of-sum-profit" id="mktPvProfit">—</div>
            </div>
            <div id="mktWarnBox"></div>
            <button class="of-submit yes" id="mktBtn">Buy YES @ market</button>
          </div>
        </div>

        <!-- Bottom tabs -->
        <div class="of-btabs">
          <div class="of-btabs-row">
            <div class="of-btab active" data-btab="position">Position</div>
            <div class="of-btab" data-btab="openorders">Orders</div>
            <div class="of-btab" data-btab="history">Trades</div>
          </div>
          <div class="of-bpanel active" id="bpanel-position"></div>
          <div class="of-bpanel" id="bpanel-openorders"></div>
          <div class="of-bpanel" id="bpanel-history"></div>
        </div>
      </div>
      </div>
    </div>
  </div>

  <!-- PORTFOLIO -->
  <div class="panel" id="panel-portfolio">
    <div class="port-view" id="portView"><div style="color:var(--text3);font-size:11px;padding:20px;font-family:var(--mono)">Select a participant to view portfolio</div></div>
  </div>

  <!-- ACTIVITY -->
  <div class="panel" id="panel-activity">
    <div class="act-view">
      <div class="act-list" id="actList"><div style="color:var(--text3);font-size:11px;padding:20px;font-family:var(--mono)">No events yet</div></div>
    </div>
  </div>

  <!-- SIMULATE -->
  <div class="panel" id="panel-simulate">
    <div class="sim-view">
      <div class="sim-status">
        <div class="sim-dot" id="simDot"></div>
        <span id="simStatusTxt" style="flex:1">Simulation stopped</span>
        <span style="font-size:10px;color:var(--text3);font-family:var(--mono)" id="simTickLbl"></span>
      </div>
      <div class="sim-stats">
        <div class="sim-stat"><div class="sim-stat-lbl">Ticks</div><div class="sim-stat-val" id="sTicks">0</div></div>
        <div class="sim-stat"><div class="sim-stat-lbl">Trades</div><div class="sim-stat-val" style="color:var(--yes)" id="sTrades">0</div></div>
        <div class="sim-stat"><div class="sim-stat-lbl">Volume</div><div class="sim-stat-val" style="color:var(--accent)" id="sVol">$0</div></div>
      </div>
      <div class="sim-grid">
        <div class="sim-card">
          <div class="sim-card-title">Parameters</div>
          <div class="sim-row"><span>Market</span><select id="simMarketSel" class="sim-inp"><option value="ALL">All open</option></select></div>
          <div class="sim-row"><span>Interval (sec)</span><input type="number" class="sim-inp sm" id="simInterval" value="1.5" min="0.3" max="10" step="0.1"></div>
          <div class="sim-row"><span>Order size ($)</span><input type="number" class="sim-inp sm" id="simOrderSize" value="80" min="5" max="1000"></div>
          <div class="sim-row"><span>Aggression</span>
            <select id="simAggression" class="sim-inp">
              <option value="passive">Passive (limit)</option>
              <option value="mixed" selected>Mixed</option>
              <option value="aggressive">Aggressive (market)</option>
            </select>
          </div>
          <div class="sim-row"><span>Price spread (¢)</span><input type="number" class="sim-inp sm" id="simSpread" value="4" min="1" max="20"></div>
          <div class="sim-row"><span>Price drift</span>
            <div class="sim-toggle">
              <label class="sim-switch"><input type="checkbox" id="simDrift" checked><span class="sim-slider-sw"></span></label>
              <span style="font-size:10px;color:var(--text3);font-family:var(--mono)" id="simCenterLbl"></span>
            </div>
          </div>
          <div class="sim-row" id="simCenterRow"><span>Target price (¢)</span><input type="number" class="sim-inp sm" id="simCenterPrice" value="50" min="5" max="95"></div>
        </div>
        <div class="sim-card">
          <div class="sim-card-title">Bot Participants</div>
          <div class="sim-bots" id="simBotList"></div>
          <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:4px">
            <div class="sim-row"><span>Bot balance ($)</span><input type="number" class="sim-inp sm" id="simBotBal" value="5000" min="500"></div>
            <button class="btn-primary" id="btnAddBot" style="width:100%;margin-top:8px;font-size:11px;padding:8px">+ Add Bot</button>
          </div>
        </div>
      </div>
      <div class="sim-actions">
        <button class="sim-btn start" id="btnSimStart">▶ Start</button>
        <button class="sim-btn stop" id="btnSimStop" style="display:none">■ Stop</button>
        <button class="sim-btn burst" id="btnSimBurst">⚡ Burst ×10</button>
      </div>
      <div class="sim-log" id="simLog"><span style="color:var(--text3)">Simulation log will appear here…</span></div>
    </div>
  </div>

  <!-- CREATE -->
  <div class="panel" id="panel-create">
    <div class="form-view">
      <div class="fcard">
        <div class="fcard-title">New Market</div>
        <div class="fld"><label>Question</label><input type="text" id="nTitle" placeholder="Will X happen before Y?" maxlength="200"></div>
        <div class="fld"><label>Resolution criteria</label><textarea id="nDesc" placeholder="When is this considered resolved…"></textarea></div>
        <div class="fld"><label>Category</label>
          <select id="nCat">
            <option>Sport</option><option>Crypto</option><option>Politics</option>
            <option>Technology</option><option>Finance</option><option>Other</option>
          </select>
        </div>
        <button class="btn-primary" id="btnCreate">Create Market</button>
      </div>
    </div>
  </div>

  <!-- CONFIG -->
  <div class="panel" id="panel-config">
    <div class="form-view">
      <div class="fcard">
        <div class="svc-balance-box">
          <div class="bl">House Balance</div>
          <div class="bv" id="svcBalCfg">$0.00</div>
        </div>
        <div class="fcard-title">Settings</div>
        <div class="fld"><label>Commission on winnings (%)</label><input type="number" id="cfgComm" min="0" max="20" step="0.1" value="2" style="width:140px"></div>
        <button class="btn-primary" id="btnSaveCfg">Save</button>
        <button class="btn-danger" id="btnReset">⚠ Reset all data</button>
      </div>
    </div>
  </div>

</div>

<!-- MODALS -->
<div class="modal-ovl" id="usrModal">
  <div class="modal-box">
    <div class="modal-hdr">
      <div class="modal-hdr-title">New Participant</div>
      <button class="modal-close" id="btnCloseU">✕</button>
    </div>
    <div class="modal-body">
      <div class="fld"><label>Name</label><input type="text" id="nuName" placeholder="Name" maxlength="24"></div>
      <div class="fld"><label>Starting balance ($)</label><input type="number" id="nuBal" value="5000" min="100"></div>
      <button class="btn-primary" style="width:100%" id="btnCrtU">Create</button>
    </div>
  </div>
</div>

<div class="modal-ovl" id="resetModal">
  <div class="modal-box">
    <div class="modal-hdr">
      <div class="modal-hdr-title">Reset all data?</div>
      <button class="modal-close" id="btnResetCancel">✕</button>
    </div>
    <div class="modal-body">
      <div style="font-size:12px;color:var(--text2);line-height:1.6;margin-bottom:16px">This will delete all participants, markets, orders, trades and balances. Initial seed data will be recreated.</div>
      <div style="display:flex;gap:8px">
        <button class="btn-primary" style="flex:1;background:var(--surface2);color:var(--text2);border:1px solid var(--border)" id="btnResetNo">Cancel</button>
        <button class="btn-primary" style="flex:1;background:var(--no)" id="btnResetYes">Reset</button>
      </div>
    </div>
  </div>
</div>

<div class="toasts" id="toasts"></div>

<script>
// ════════════════════════════════════════════════════
// STATE
// ════════════════════════════════════════════════════
var mkts={}, usrs={}, cfg={commission_pct:.02}, curU=null, curMid=null;
var curOutcome='YES';
var curOrderType='limit';
var obMode='price';
var obLevels=10;
var actLog=[];
var prevOB={bids:{},asks:{}};
var simTimer=null, simStats={ticks:0,trades:0,volume:0};
var simCenterPrice={};
var BOT_NAMES=['Phoenix Bot','Algo X','Arbitrage Bot','Sigma Bot','Delta Pro','Quant X','Neuron Bot','Wave Bot'];

// ════════════════════════════════════════════════════
// HELPERS
// ════════════════════════════════════════════════════
function fmt(v){return '$'+(v||0).toFixed(2)}
function fmtV(v){if(v>=1e6)return '$'+(v/1e6).toFixed(1)+'M';if(v>=1000)return '$'+(v/1000).toFixed(1)+'k';return '$'+Math.round(v||0)}
function updSvc(v){var s=fmt(v);document.getElementById('svcBal').textContent=s;document.getElementById('svcBalCfg').textContent=s}

async function api(path,method,body){
  var r=await fetch(path,{method:method||'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  var d=await r.json();if(!r.ok)throw new Error(d.detail||'Error');return d;
}

function toast(msg,t){
  var d=document.createElement('div');d.className='toast '+(t||'info');d.textContent=msg;
  document.getElementById('toasts').appendChild(d);setTimeout(()=>d.remove(),3800);
}

// ════════════════════════════════════════════════════
// WEBSOCKET
// ════════════════════════════════════════════════════
function connect(){
  var ws;
  try{ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');}
  catch(e){loadHttp();return;}
  ws.onopen=()=>{document.getElementById('wsDot').className='ws-dot on'};
  ws.onclose=()=>{document.getElementById('wsDot').className='ws-dot';setTimeout(connect,2000)};
  ws.onerror=()=>loadHttp();
  ws.onmessage=e=>{
    var m=JSON.parse(e.data);
    if(m.type==='init'){applyState(m);return}
    if(m.type==='markets'){
      (m.data||[]).forEach(mk=>mkts[mk.id]=mk);
      renderMarkets();updateSimMarketSel();
      if(curMid&&mkts[curMid]){renderOB(mkts[curMid]);updateFormPrices();if(curOrderType==='market')updMktPrev();loadTrades();loadPositions();drawChart();}
    }
    if(m.type==='users'){
      (m.data||[]).forEach(u=>usrs[u.id]=u);
      renderHdr();renderDD();updateSimBots();
      if(document.getElementById('panel-portfolio').classList.contains('active'))renderPort();
      if(curMid)loadPositions();
    }
    if(m.type==='activity'){actLog.unshift(m.data);prependAct(m.data);}
    if(m.type==='service_balance')updSvc(m.data);
    if(m.type==='config'){cfg=m.data;document.getElementById('cfgComm').value=(cfg.commission_pct*100).toFixed(1);}
    if(m.type==='reset'){
      stopSim();mkts={};usrs={};actLog=[];curU=null;curMid=null;
      localStorage.removeItem('betx2_curU');
      (m.users||[]).forEach(u=>usrs[u.id]=u);
      (m.markets||[]).forEach(mk=>mkts[mk.id]=mk);
      actLog=m.activity||[];updSvc(m.service_balance||0);
      renderMarkets();renderHdr();renderDD();renderAct();
      document.getElementById('portView').innerHTML='<div style="color:var(--text3);font-size:11px;padding:20px;font-family:var(--mono)">Select a participant</div>';
      prevOB={bids:{},asks:{}};
      toast('Data reset','info');
    }
  };
}
async function loadHttp(){try{applyState(await api('/api/state'));}catch(e){}}

function applyState(d){
  (d.users||[]).forEach(u=>usrs[u.id]=u);
  (d.markets||[]).forEach(mk=>mkts[mk.id]=mk);
  actLog=d.activity||[];if(d.config)cfg=d.config;
  document.getElementById('cfgComm').value=(cfg.commission_pct*100).toFixed(1);
  updSvc(d.service_balance||0);
  renderMarkets();renderHdr();renderDD();renderAct();
  if(!curU){
    var saved=localStorage.getItem('betx2_curU');
    if(saved&&usrs[saved]){curU=saved;renderHdr();renderDD();}
  }
}

// ════════════════════════════════════════════════════
// MARKETS GRID
// ════════════════════════════════════════════════════
function renderMarkets(){
  var g=document.getElementById('mgrid');
  var list=Object.values(mkts);
  if(!list.length){g.innerHTML='<div style="color:var(--text3);font-size:11px;padding:20px;font-family:var(--mono)">No markets</div>';return;}
  g.innerHTML='';
  list.forEach(m=>{
    var isR=m.status==='resolved';
    var lp=m.last_price;
    var yesP=lp!=null?lp:(m.best_ask!=null?m.best_ask:(m.best_bid!=null?100-m.best_bid:50));
    var noP=Math.round((100-yesP)*10)/10;
    var bCls=isR?(m.outcome==='YES'?'ry':'rn'):'open';
    var bTxt=isR?(m.outcome==='YES'?'YES WIN':'NO WIN'):'Open';
    var card=document.createElement('div');
    card.className='mcard'+(isR?' resolved':'');
    card.innerHTML=
      '<div class="mcard-body">'+
        '<div class="mcard-top"><span class="mcard-cat">'+m.category+'</span><span class="mcard-badge '+bCls+'">'+bTxt+'</span></div>'+
        '<div class="mcard-title">'+m.title+'</div>'+
        '<div class="prob-bar"><div class="prob-bar-fill" style="width:'+yesP+'%"></div></div>'+
        '<div class="prob-row">'+
          '<div class="prob-yes"><span class="prob-pct">'+yesP+'¢</span><span class="prob-lbl">YES</span></div>'+
          '<div class="prob-no"><span class="prob-pct">'+noP+'¢</span><span class="prob-lbl">NO</span></div>'+
        '</div>'+
        '<div class="mcard-footer"><span>'+m.trade_count+' trades · '+fmtV(m.volume)+'</span><span style="font-family:var(--mono);font-size:9px">'+m.created_at+'</span></div>'+
      '</div>';
    if(!isR)card.addEventListener('click',()=>openMarket(m.id));
    g.appendChild(card);
  });
}

// ════════════════════════════════════════════════════
// MARKET DETAIL
// ════════════════════════════════════════════════════
function openMarket(mid){
  if(!curU){toast('Select a participant first','err');return;}
  curMid=mid;
  document.getElementById('mgrid').style.display='none';
  document.getElementById('marketView').classList.add('open');
  document.querySelectorAll('.of-btab').forEach(t=>t.onclick=()=>switchBTab(t.dataset.btab));
  selOutcome('YES');setOrderType('limit');
  refreshDetail();
}

function closeMarket(){
  document.getElementById('marketView').classList.remove('open');
  document.getElementById('mgrid').style.display='';
  curMid=null;
}

function switchBTab(tab){
  document.querySelectorAll('.of-btab').forEach(t=>t.classList.toggle('active',t.dataset.btab===tab));
  document.querySelectorAll('.of-bpanel').forEach(p=>p.classList.toggle('active',p.id==='bpanel-'+tab));
}

async function refreshDetail(){
  if(!curMid)return;
  var m=mkts[curMid];
  document.getElementById('dCat').textContent=m.category;
  document.getElementById('dTitle').textContent=m.title;
  var lp=m.last_price||(m.best_bid&&m.best_ask?Math.round((m.best_bid+m.best_ask)/2*10)/10:null);
  document.getElementById('dPrice').textContent=lp!=null?lp+'¢':'—';
  document.getElementById('dOdds').textContent=lp!=null?(100/lp).toFixed(2)+'×':'';
  var isR=m.status==='resolved';
  document.getElementById('ofWrap').style.opacity=isR?'0.4':'1';
  document.getElementById('ofWrap').style.pointerEvents=isR?'none':'all';
  var ra=document.getElementById('dResArea');ra.innerHTML='';
  if(isR){
    ra.innerHTML='<div class="mv-resolved-banner '+(m.outcome||'').toLowerCase()+'">'+( m.outcome==='YES'?'✓ YES WIN':'✗ NO WIN')+'</div>';
  } else {
    ra.innerHTML=
      '<span style="font-size:9px;color:var(--text3);margin-right:4px">Resolve:</span>'+
      '<button class="mv-resolve-btn y" onclick="doResolve(\'YES\')">YES</button>'+
      '<button class="mv-resolve-btn n" onclick="doResolve(\'NO\')">NO</button>';
  }
  updateFormPrices();renderOB(m);
  await loadTrades();await loadPositions();drawChart();
}

function updateFormPrices(){
  if(!curMid)return;
  var m=mkts[curMid];
  var ba=m.best_ask,bb=m.best_bid;
  document.getElementById('ofYesPrice').textContent=ba!=null?ba+'¢':'—';
  document.getElementById('ofYesOdds').textContent=ba!=null?'×'+(100/ba).toFixed(2):'';
  document.getElementById('ofNoPrice').textContent=bb!=null?(100-bb)+'¢':'—';
  document.getElementById('ofNoOdds').textContent=bb!=null?'×'+(100/(100-bb)).toFixed(2):'';
}

// ════════════════════════════════════════════════════
// ORDERBOOK
// ════════════════════════════════════════════════════
function toggleOBMode(){
  obMode=obMode==='price'?'odds':'price';
  var btn=document.getElementById('obModeBtn'),lbl=document.getElementById('obPriceLbl');
  if(btn){btn.textContent=obMode==='price'?'ODDS':'PRICE';btn.style.color=obMode==='odds'?'var(--gold)':'var(--accent)';}
  if(lbl)lbl.textContent=obMode==='price'?'PRICE':'ODDS';
  if(curMid&&mkts[curMid])renderOB(mkts[curMid]);
}
function toggleOBLevels(){
  obLevels=obLevels===10?20:10;
  var b=document.getElementById('obExpandBtn');if(b)b.textContent=obLevels===10?'▼ 20':'▲ 10';
  if(curMid&&mkts[curMid])renderOB(mkts[curMid]);
}

function renderOB(m){
  var ob=m.orderbook||{bids:[],asks:[]};
  var asksEl=document.getElementById('obAsks'),bidsEl=document.getElementById('obBids');
  var allD=ob.asks.concat(ob.bids).map(r=>r.dollars);
  var maxD=allD.length?Math.max(...allD):1;

  function mkRow(r,side){
    var cls='ob-row '+(side==='ask'?'ob-ask-row':'ob-bid-row');
    var clickSide=side==='ask'?'YES':'NO';
    var bar=Math.round(r.dollars/maxD*100);
    var prev=side==='ask'?prevOB.asks[r.price]:prevOB.bids[r.price];
    var flash=(prev!==undefined&&Math.abs(prev-r.dollars)>0.01)?(side==='ask'?' flash-ask':' flash-bid'):'';
    var pDisp=obMode==='odds'
      ?(side==='ask'?(100/r.price).toFixed(2)+'×':(100/(100-r.price)).toFixed(2)+'×')
      :r.price+'¢';
    return '<div class="'+cls+flash+'" onclick="clickOB(\''+clickSide+'\','+r.price+')">'+
      '<div class="ob-depth" style="width:'+bar+'%"></div>'+
      '<span class="ob-p ob-col">'+pDisp+'</span>'+
      '<span class="ob-col">'+r.contracts.toFixed(1)+'</span>'+
      '<span class="ob-col">$'+r.dollars.toFixed(0)+'</span>'+
      '<span class="ob-col">$'+r.cumul.toFixed(0)+'</span>'+
    '</div>';
  }
  var asksSlice=ob.asks.slice(0,obLevels);
  var ac=0;var asksC=asksSlice.map(r=>{ac=Math.round((ac+r.dollars)*100)/100;return{...r,cumul:ac}});
  var asksRev=[...asksC].reverse();
  var bidsSlice=ob.bids.slice(0,obLevels);
  var bc=0;var bidsC=bidsSlice.map(r=>{bc=Math.round((bc+r.dollars)*100)/100;return{...r,cumul:bc}});

  asksEl.innerHTML=asksRev.length
    ?'<div style="padding:2px 12px;font-size:8px;color:var(--no);font-family:var(--mono);font-weight:700;letter-spacing:1px;opacity:.65">SELLERS · click to BUY</div>'+asksRev.map(r=>mkRow(r,'ask')).join('')
    :'<div style="padding:8px 12px;font-size:10px;color:var(--text3)">No sellers</div>';
  bidsEl.innerHTML=bidsC.length
    ?bidsC.map(r=>mkRow(r,'bid')).join('')+'<div style="padding:2px 12px;font-size:8px;color:var(--yes);font-family:var(--mono);font-weight:700;letter-spacing:1px;opacity:.65">BUYERS · click to SELL</div>'
    :'<div style="padding:8px 12px;font-size:10px;color:var(--text3)">No buyers</div>';

  var lp=m.last_price;
  document.getElementById('obMidP').textContent=lp!=null?lp+'¢':(m.best_bid&&m.best_ask?'~'+Math.round((m.best_bid+m.best_ask)/2*10)/10+'¢':'—');
  document.getElementById('obMidS').textContent=m.spread!=null?'spread: '+m.spread+'¢':'';
  prevOB.asks={};prevOB.bids={};
  ob.asks.forEach(r=>prevOB.asks[r.price]=r.dollars);
  ob.bids.forEach(r=>prevOB.bids[r.price]=r.dollars);
}

function clickOB(outcome,price){
  selOutcome(outcome);
  document.getElementById('priceInp').value=price;
  document.getElementById('priceSlider').value=Math.round(price);
  updPreview();
}

// ════════════════════════════════════════════════════
// TRADES LIST
// ════════════════════════════════════════════════════
async function loadTrades(){
  var el=document.getElementById('tradesScroll');
  var bt=document.getElementById('bpanel-history');
  try{
    var ts=await api('/api/trades/'+curMid);
    if(!ts.length){
      el.innerHTML='<div style="padding:12px;font-size:10px;color:var(--text3);font-family:var(--mono)">No trades</div>';
      bt.innerHTML='<div style="font-size:10px;color:var(--text3);font-family:var(--mono)">No trades</div>';
      return;
    }
    var lastP=null;
    var rows=ts.map(t=>{
      var up=!lastP||t.price>=lastP;lastP=t.price;
      var pc=up?'var(--yes)':'var(--no)';
      return '<div class="trade-row">'+
        '<span class="trade-p" style="color:'+pc+'">'+t.price+'¢</span>'+
        '<span class="trade-q">'+t.contracts.toFixed(1)+'</span>'+
        '<span class="trade-names">'+t.buy_user_name+' / '+t.sell_user_name+'</span>'+
        '<span class="trade-ts">'+t.created_at+'</span>'+
      '</div>';
    }).join('');
    el.innerHTML=rows;
    bt.innerHTML='<div style="font-size:9px;color:var(--text3);font-family:var(--mono);margin-bottom:6px">'+ts.length+' trades</div>'+
      ts.map(t=>{
        return '<div class="ord-entry">'+
          '<span style="color:var(--yes);font-weight:600">'+t.price+'¢</span>'+
          '<span style="color:var(--text3)">×'+t.contracts.toFixed(2)+'</span>'+
          '<span style="color:var(--text2);overflow:hidden;text-overflow:ellipsis">'+t.buy_user_name+' / '+t.sell_user_name+'</span>'+
          '<span style="color:var(--text3);margin-left:auto">'+t.created_at+'</span>'+
        '</div>';
      }).join('');
  }catch(e){}
}

// ════════════════════════════════════════════════════
// POSITION PANEL — правильная нетто-логика
// ════════════════════════════════════════════════════
async function loadPositions(){
  if(!curU||!curMid)return;
  var posEl=document.getElementById('bpanel-position');
  var ordEl=document.getElementById('bpanel-openorders');
  
  try{
    var data=await api('/api/portfolio/'+curU);
    var m=mkts[curMid];
    var isR=m&&m.status==='resolved';
    
    // Найти позицию по текущему рынку
    var pos=data.positions.find(x=>x.market&&x.market.id===curMid);
    
    // Рендер позиций
    if(!pos||(pos.yes_qty<0.001&&pos.no_qty<0.001)){
      posEl.innerHTML='<div class="pos-no-data">No open position</div>';
    } else {
      var html='';
      var ob=m?m.orderbook||{bids:[],asks:[]}:{bids:[],asks:[]};
      var curYesP=pos.cur_yes_price;
      
      // YES позиция (лонг YES)
      if(pos.yes_qty>=0.001){
        var upnl=pos.yes_upnl;
        var pnlCls=upnl==null?'flat':upnl>0.01?'up':upnl<-0.01?'dn':'flat';
        var bestBid=ob.bids&&ob.bids.length?ob.bids[0].price:null;
        var resolved=isR&&m.outcome;
        
        html+='<div class="pos-card">'+
          '<div class="pos-card-header">'+
            '<span class="pos-badge long">LONG YES</span>'+
            '<span class="pos-pnl-chip '+pnlCls+'">'+(upnl!=null?(upnl>=0?'+':'')+fmt(upnl):'—')+'</span>'+
          '</div>'+
          '<div class="pos-grid">'+
            '<div class="pos-cell"><div class="pos-cell-lbl">Size</div><div class="pos-cell-val">'+pos.yes_qty.toFixed(2)+'</div></div>'+
            '<div class="pos-cell"><div class="pos-cell-lbl">Entry (WAP)</div><div class="pos-cell-val">'+pos.yes_wap.toFixed(1)+'¢</div></div>'+
            '<div class="pos-cell"><div class="pos-cell-lbl">Current</div><div class="pos-cell-val">'+(curYesP!=null?curYesP+'¢':'—')+'</div></div>'+
            '<div class="pos-cell"><div class="pos-cell-lbl">Cost</div><div class="pos-cell-val">'+fmt(pos.yes_cost)+'</div></div>'+
          '</div>'+
          (resolved?
            '<div class="pos-close-row"><div class="leg-settled" style="color:'+(m.outcome==='YES'?'var(--yes)':'var(--no)')+'">'+( m.outcome==='YES'?'✓ WIN — awaiting payout':'✗ LOSS')+'</div></div>'
          :
            '<div class="pos-close-row">'+
              '<button class="pos-close-btn"'+(bestBid?'':' disabled')+' onclick="doClosePosition(\'YES\')" id="closeBtnYes">'+
                (bestBid?'Close LONG @ '+bestBid+'¢ (market)':'No buyers in book')+
              '</button>'+
            '</div>'
          )+
        '</div>';
      }
      
      // NO позиция (лонг NO)
      if(pos.no_qty>=0.001){
        var nupnl=pos.no_upnl;
        var npnlCls=nupnl==null?'flat':nupnl>0.01?'up':nupnl<-0.01?'dn':'flat';
        var curNoP=curYesP!=null?Math.round((100-curYesP)*10)/10:null;
        var bestAsk=ob.asks&&ob.asks.length?ob.asks[0].price:null;
        var resolved=isR&&m.outcome;
        
        html+='<div class="pos-card">'+
          '<div class="pos-card-header">'+
            '<span class="pos-badge short">LONG NO</span>'+
            '<span class="pos-pnl-chip '+npnlCls+'">'+(nupnl!=null?(nupnl>=0?'+':'')+fmt(nupnl):'—')+'</span>'+
          '</div>'+
          '<div class="pos-grid">'+
            '<div class="pos-cell"><div class="pos-cell-lbl">Size</div><div class="pos-cell-val">'+pos.no_qty.toFixed(2)+'</div></div>'+
            '<div class="pos-cell"><div class="pos-cell-lbl">Entry (WAP)</div><div class="pos-cell-val">'+pos.no_wap.toFixed(1)+'¢</div></div>'+
            '<div class="pos-cell"><div class="pos-cell-lbl">Current</div><div class="pos-cell-val">'+(curNoP!=null?curNoP+'¢':'—')+'</div></div>'+
            '<div class="pos-cell"><div class="pos-cell-lbl">Cost</div><div class="pos-cell-val">'+fmt(pos.no_cost)+'</div></div>'+
          '</div>'+
          (resolved?
            '<div class="pos-close-row"><div class="leg-settled" style="color:'+(m.outcome==='NO'?'var(--yes)':'var(--no)')+'">'+( m.outcome==='NO'?'✓ WIN — awaiting payout':'✗ LOSS')+'</div></div>'
          :
            '<div class="pos-close-row">'+
              '<button class="pos-close-btn"'+(bestAsk?'':' disabled')+' onclick="doClosePosition(\'NO\')" id="closeBtnNO">'+
                (bestAsk?'Close LONG NO @ '+(100-bestAsk)+'¢ (market)':'No sellers in book')+
              '</button>'+
            '</div>'
          )+
        '</div>';
      }
      
      // Реализованный PnL если есть
      if(pos.realized_pnl&&Math.abs(pos.realized_pnl)>0.001){
        var rCls=pos.realized_pnl>0?'up':'dn';
        html+='<div style="padding:6px 2px;font-size:10px;font-family:var(--mono);color:var(--text3)">'+
          'Realized P&L: <span class="pos-cell-val '+rCls+'">'+(pos.realized_pnl>=0?'+':'')+fmt(pos.realized_pnl)+'</span>'+
        '</div>';
      }
      
      posEl.innerHTML=html;
    }
    
    // Open orders
    var pend=data.pending_orders.filter(o=>o.market_id===curMid);
    if(!pend.length){
      ordEl.innerHTML='<div class="pos-no-data">No open orders</div>';
    } else {
      ordEl.innerHTML=pend.map(o=>{
        var rem=o.contracts-o.filled;var iB=o.side==='BUY';
        return '<div class="ord-entry">'+
          '<span style="color:'+(iB?'var(--yes)':'var(--no)')+';font-weight:600;font-size:10px">'+(iB?'BUY YES':'SELL YES')+'</span>'+
          '<span>@ '+o.price+'¢</span>'+
          '<span style="color:var(--text3)">'+rem.toFixed(1)+'c</span>'+
          '<button class="ord-cancel-btn" data-oid="'+o.id+'" onclick="cancelOrd(this.dataset.oid)">✕</button>'+
        '</div>';
      }).join('');
    }
  }catch(e){posEl.innerHTML='<div class="pos-no-data" style="color:var(--no)">Error loading position</div>';}
}

// ════ CLOSE POSITION ════
var _closingLock=false;
async function doClosePosition(side){
  if(!curU||!curMid||_closingLock)return;
  _closingLock=true;
  // Блочим кнопки
  document.querySelectorAll('.pos-close-btn').forEach(b=>b.disabled=true);
  try{
    var r=await api('/api/close_position','POST',{user_id:curU,market_id:curMid,side});
    toast('Closed '+side+' @ '+r.price+'¢ · '+r.filled.toFixed(2)+' shares','ok');
    await loadTrades();await loadPositions();drawChart();
  }catch(e){
    toast(e.message,'err');
  }finally{
    _closingLock=false;
    // Разблокируем кнопки через небольшую задержку (ждём обновления UI)
    setTimeout(()=>loadPositions(),100);
  }
}

async function cancelOrd(oid){
  try{var r=await api('/api/order/cancel','POST',{order_id:oid,user_id:curU});toast('Cancelled · refund '+fmt(r.refund),'ok');await loadPositions();}
  catch(e){toast(e.message,'err');}
}

// ════════════════════════════════════════════════════
// ORDER FORM
// ════════════════════════════════════════════════════
function selOutcome(o){
  curOutcome=o;
  document.getElementById('ofYes').className='of-outcome-btn yes'+(o==='YES'?' active':'');
  document.getElementById('ofNo').className='of-outcome-btn no'+(o==='NO'?' active':'');
  var slider=document.getElementById('priceSlider');
  slider.className='of-slider '+(o==='YES'?'yes':'no');
  var sub=document.getElementById('subBtn'),mBtn=document.getElementById('mktBtn');
  sub.className='of-submit '+(o==='YES'?'yes':'no');
  sub.textContent=o==='YES'?'Buy YES':'Buy NO';
  mBtn.className='of-submit '+(o==='YES'?'yes':'no');
  updPreview();
  if(curOrderType==='market')updMktPrev();
}

function setOrderType(t){
  curOrderType=t;
  document.getElementById('otLimit').className='of-type-btn'+(t==='limit'?' active':'');
  document.getElementById('otMarket').className='of-type-btn'+(t==='market'?' active':'');
  document.getElementById('limitSection').style.display=t==='limit'?'block':'none';
  document.getElementById('marketSection').style.display=t==='market'?'block':'none';
  if(t==='market')updMktPrev();
}

function getPrice(){return Math.round(Math.min(99.9,Math.max(0.1,parseFloat(document.getElementById('priceInp').value)||50))*10)/10}

function updPreview(){
  if(!curMid)return;
  var price=getPrice();
  var dollars=parseFloat(document.getElementById('dollarInp').value)||0;
  var isYes=curOutcome==='YES';
  var payPer=isYes?price/100:(100-price)/100;
  var shares=dollars>0?Math.round(dollars/payPer*100)/100:0;
  var winNet=Math.round(shares*(1-cfg.commission_pct)*100)/100;
  var profit=Math.round((winNet-dollars)*100)/100;
  var odds=(isYes?100/price:100/(100-price)).toFixed(2);
  var priceLbl=isYes?price.toFixed(1)+'¢ YES':(100-price).toFixed(1)+'¢ NO';
  document.getElementById('sumPrice').textContent=priceLbl;
  document.getElementById('sumShares').textContent=shares.toFixed(2);
  document.getElementById('sumCost').textContent=fmt(dollars)+' (×'+odds+')';
  document.getElementById('sumPayout').textContent=fmt(winNet);
  document.getElementById('sumProfit').style.color=profit>=0?'var(--yes)':'var(--no)';
  document.getElementById('sumProfit').textContent='Profit: '+(profit>=0?'+':'')+fmt(profit)+' · '+odds+'× odds';
}

function updMktPrev(){
  if(!curMid)return;
  var m=mkts[curMid];
  var ob=m.orderbook||{bids:[],asks:[]};
  var isYes=curOutcome==='YES';
  var dollars=parseFloat(document.getElementById('mktDollarInp').value)||0;
  var comm=cfg.commission_pct;
  var bestAsk=ob.asks&&ob.asks.length?ob.asks[0].price:null;
  var bestBid=ob.bids&&ob.bids.length?ob.bids[0].price:null;
  document.getElementById('mktBestAsk').textContent=bestAsk!=null?bestAsk+'¢':'no sellers';
  document.getElementById('mktBestBid').textContent=bestBid!=null?bestBid+'¢':'no buyers';
  var execP=isYes?bestAsk:bestBid;
  var btn=document.getElementById('mktBtn');
  var warnEl=document.getElementById('mktWarnBox');warnEl.innerHTML='';
  if(!execP){
    document.getElementById('mktPvPrice').textContent='—';
    document.getElementById('mktPvOdds').textContent='—';
    document.getElementById('mktPvShares').textContent='—';
    document.getElementById('mktPvPayout').textContent='—';
    document.getElementById('mktPvProfit').textContent='';
    btn.textContent='No liquidity';btn.disabled=true;
    warnEl.innerHTML='<div class="of-mkt-warn">No '+(isYes?'sellers (ASK)':'buyers (BID)')+' in the book</div>';
    return;
  }
  var levels=isYes?[...ob.asks]:[...ob.bids].reverse();
  var rem=dollars,totalC=0,totalD=0;
  for(var lvl of levels){
    var cpp=isYes?lvl.price/100:(100-lvl.price)/100;
    var maxD=lvl.contracts*cpp;
    var use=Math.min(rem,maxD);
    totalC+=use/cpp;totalD+=use;rem-=use;
    if(rem<0.01)break;
  }
  var avgP=totalC>0?Math.round(totalD/totalC*100):execP;
  var slip=isYes?avgP-execP:execP-avgP;
  var winNet=Math.round(totalC*(1-comm)*100)/100;
  var profit=Math.round((winNet-totalD)*100)/100;
  var odds=(isYes?100/execP:100/(100-execP)).toFixed(2);
  document.getElementById('mktPvPrice').textContent=(totalC>0&&slip>0.5?'~'+Math.round(avgP):execP)+'¢';
  document.getElementById('mktPvPrice').style.color=isYes?'var(--yes)':'var(--no)';
  document.getElementById('mktPvOdds').textContent=odds+'×';
  document.getElementById('mktPvShares').textContent=totalC.toFixed(2);
  document.getElementById('mktPvPayout').textContent=fmt(winNet);
  document.getElementById('mktPvProfit').textContent='Profit: '+(profit>=0?'+':'')+fmt(profit);
  document.getElementById('mktPvProfit').style.color=profit>=0?'var(--yes)':'var(--no)';
  btn.textContent=(isYes?'Buy YES':'Buy NO')+' @ '+execP+'¢ (×'+odds+')';
  btn.className='of-submit '+(isYes?'yes':'no');
  btn.disabled=!dollars||dollars<1;
  if(rem>0.5)warnEl.innerHTML='<div class="of-mkt-warn">⚠ Book only covers '+fmt(Math.round(totalD*100)/100)+' of '+fmt(dollars)+'</div>';
  if(slip>1)warnEl.innerHTML+='<div class="of-mkt-warn">⚠ Slippage ~'+Math.round(slip*10)/10+'¢</div>';
}

async function submitOrder(){
  var price=getPrice();
  var dollars=parseFloat(document.getElementById('dollarInp').value);
  if(!dollars||dollars<1){toast('Minimum $1','err');return;}
  var side=curOutcome==='YES'?'BUY':'SELL';
  var btn=document.getElementById('subBtn');btn.disabled=true;
  try{
    var d=await api('/api/order','POST',{user_id:curU,market_id:curMid,side,price,dollars});
    toast(d.trades>0?'Order filled! Trades: '+d.trades:'Order in book','ok');
    await loadTrades();await loadPositions();drawChart();
  }catch(e){toast(e.message,'err');}
  finally{btn.disabled=false;}
}

async function submitMktOrder(){
  if(!curMid||!curU)return;
  var m=mkts[curMid];var ob=m.orderbook||{bids:[],asks:[]};
  var isYes=curOutcome==='YES';
  var dollars=parseFloat(document.getElementById('mktDollarInp').value);
  if(!dollars||dollars<1){toast('Minimum $1','err');return;}
  var execP=isYes?(ob.asks&&ob.asks.length?ob.asks[0].price:null):(ob.bids&&ob.bids.length?ob.bids[0].price:null);
  if(!execP){toast('No liquidity','err');return;}
  var side=isYes?'BUY':'SELL';
  var btn=document.getElementById('mktBtn');btn.disabled=true;
  try{
    var d=await api('/api/order','POST',{user_id:curU,market_id:curMid,side,price:execP,dollars});
    toast(d.trades>0?'Market order filled @ '+execP+'¢! Trades: '+d.trades:'Order in book (book changed)',d.trades>0?'ok':'info');
    await loadTrades();await loadPositions();drawChart();updMktPrev();
  }catch(e){toast(e.message,'err');}
  finally{btn.disabled=false;}
}

async function doResolve(outcome){
  if(!confirm('Resolve market as «'+outcome+'»?'))return;
  try{
    var d=await api('/api/resolve','POST',{market_id:curMid,outcome});
    toast('Market resolved! Payouts: '+fmt(d.total_payout)+' · Fee: '+fmt(d.total_commission),'ok');
    closeMarket();
  }catch(e){toast(e.message,'err');}
}

// ════════════════════════════════════════════════════
// CHART
// ════════════════════════════════════════════════════
var chartData=[];
async function drawChart(){
  var canvas=document.getElementById('priceCanvas');
  var empty=document.getElementById('chartEmpty');
  if(!canvas||!curMid)return;
  try{chartData=await api('/api/price_history/'+curMid);}catch(e){chartData=[];}
  var m=mkts[curMid];
  var W=canvas.parentElement.clientWidth||600,H=canvas.parentElement.clientHeight||200;
  canvas.width=W;canvas.height=H;
  var ctx=canvas.getContext('2d');
  ctx.clearRect(0,0,W,H);
  if(!chartData.length){if(empty)empty.style.display='flex';drawEmptyGrid(ctx,W,H,m);return;}
  if(empty)empty.style.display='none';
  var prices=chartData.map(t=>t.price);
  var minP=Math.max(0,Math.min(...prices)-8),maxP=Math.min(100,Math.max(...prices)+8);
  var range=maxP-minP||10;
  var pad={l:36,r:12,t:12,b:28};
  var cW=W-pad.l-pad.r,cH=H-pad.t-pad.b;
  function cx(i){return pad.l+i/(chartData.length-1||1)*cW}
  function cy(p){return pad.t+(1-(p-minP)/range)*cH}
  var gridStep=range>40?10:range>20?5:2;
  for(var g=Math.ceil(minP/gridStep)*gridStep;g<=maxP;g+=gridStep){
    var gy=cy(g);
    ctx.strokeStyle='rgba(255,255,255,.05)';ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(pad.l,gy);ctx.lineTo(W-pad.r,gy);ctx.stroke();
    ctx.fillStyle='rgba(255,255,255,.25)';ctx.font='9px DM Mono,monospace';
    ctx.textAlign='right';ctx.fillText(g+'%',pad.l-4,gy+3);
  }
  if(m.best_bid&&m.best_ask){
    var yA=cy(m.best_ask),yB=cy(m.best_bid);
    ctx.fillStyle='rgba(91,106,240,.06)';
    ctx.fillRect(pad.l,Math.min(yA,yB),cW,Math.abs(yB-yA)||1);
  }
  var grad=ctx.createLinearGradient(0,pad.t,0,H-pad.b);
  grad.addColorStop(0,'rgba(0,192,122,.25)');grad.addColorStop(1,'rgba(0,192,122,.0)');
  ctx.beginPath();
  chartData.forEach((t,i)=>{
    var x=cx(i),y=cy(t.price);
    if(i===0){ctx.moveTo(x,H-pad.b);ctx.lineTo(x,y);}else ctx.lineTo(x,y);
  });
  ctx.lineTo(cx(chartData.length-1),H-pad.b);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
  ctx.strokeStyle='#00c07a';ctx.lineWidth=1.5;ctx.lineJoin='round';ctx.lineCap='round';
  ctx.beginPath();
  chartData.forEach((t,i)=>{var x=cx(i),y=cy(t.price);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});
  ctx.stroke();
  var lastI=chartData.length-1;var lx=cx(lastI),ly=cy(prices[lastI]),lp=prices[lastI];
  ctx.fillStyle='#00c07a';ctx.beginPath();ctx.arc(lx,ly,3.5,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='rgba(0,192,122,.9)';ctx.font='bold 10px DM Mono,monospace';
  ctx.textAlign=lx>W-80?'right':'left';
  ctx.fillText(lp+'¢ '+(100/lp).toFixed(2)+'×',lx+(lx>W-80?-8:8),ly>12?ly-5:ly+12);
  ctx.fillStyle='rgba(255,255,255,.25)';ctx.font='8px DM Mono,monospace';ctx.textAlign='center';
  var step=Math.max(1,Math.floor(chartData.length/6));
  for(var i=0;i<chartData.length;i+=step){ctx.fillText(chartData[i].ts,cx(i),H-6);}
}

function drawEmptyGrid(ctx,W,H,m){
  var pad={l:36,r:12,t:12,b:28};
  [25,50,75].forEach(p=>{
    var y=pad.t+(1-p/100)*(H-pad.t-pad.b);
    ctx.strokeStyle='rgba(255,255,255,.04)';ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();
    ctx.fillStyle='rgba(255,255,255,.2)';ctx.font='9px DM Mono,monospace';ctx.textAlign='right';
    ctx.fillText(p+'%',pad.l-4,y+3);
  });
}

// ════════════════════════════════════════════════════
// PORTFOLIO
// ════════════════════════════════════════════════════
async function renderPort(){
  var el=document.getElementById('portView');
  if(!curU){el.innerHTML='<div style="color:var(--text3);font-size:11px;padding:20px;font-family:var(--mono)">Select a participant</div>';return;}
  var u=usrs[curU];
  try{
    var p=await api('/api/portfolio/'+curU);
    var comm=cfg.commission_pct;
    var totalInvested=0,totalUpnl=0,totalRealized=0;
    p.positions.forEach(pos=>{
      totalInvested+=pos.yes_cost+pos.no_cost;
      if(pos.yes_upnl!=null)totalUpnl+=pos.yes_upnl;
      if(pos.no_upnl!=null)totalUpnl+=pos.no_upnl;
      totalRealized+=pos.realized_pnl||0;
    });
    var pC=totalUpnl>0.01?'var(--yes)':totalUpnl<-0.01?'var(--no)':'var(--text3)';
    var rC=totalRealized>0.01?'var(--yes)':totalRealized<-0.01?'var(--no)':'var(--text3)';
    
    var html=
      '<div class="port-summary">'+
        '<div class="port-stat"><div class="port-stat-lbl">Cash Balance</div><div class="port-stat-val" style="color:var(--yes)">'+fmt(u.balance)+'</div></div>'+
        '<div class="port-stat"><div class="port-stat-lbl">Invested</div><div class="port-stat-val" style="color:var(--accent)">'+fmt(totalInvested)+'</div></div>'+
        '<div class="port-stat"><div class="port-stat-lbl">Unrealized P&L</div><div class="port-stat-val" style="color:'+pC+'">'+(totalInvested>0?(totalUpnl>=0?'+':'')+fmt(totalUpnl):'—')+'</div></div>'+
        '<div class="port-stat"><div class="port-stat-lbl">Realized P&L</div><div class="port-stat-val" style="color:'+rC+'">'+(Math.abs(totalRealized)>0.001?(totalRealized>=0?'+':'')+fmt(totalRealized):'—')+'</div></div>'+
        '<div class="port-stat"><div class="port-stat-lbl">Open Orders</div><div class="port-stat-val" style="color:var(--gold)">'+p.pending_orders.length+'</div></div>'+
      '</div>';

    if(!p.positions.length&&!p.pending_orders.length){
      html+='<div style="color:var(--text3);font-size:11px;font-family:var(--mono)">No positions</div>';
      el.innerHTML=html;return;
    }

    p.positions.forEach(pos=>{
      var m=pos.market,isR=m.status==='resolved';
      var curYesP=pos.cur_yes_price;
      var curNoP=curYesP!=null?Math.round((100-curYesP)*10)/10:null;
      var totalPnl=((pos.yes_upnl||0)+(pos.no_upnl||0)+(pos.realized_pnl||0));
      var pCls=totalPnl>0.01?'up':totalPnl<-0.01?'dn':'flat';

      html+='<div class="pcard">'+
        '<div class="pcard-hd" data-mid="'+m.id+'" onclick="jumpToMarket(this.dataset.mid)">'+
          '<div style="flex:1">'+
            '<div class="pcard-title">'+m.title+'</div>'+
            '<div style="font-size:10px;color:var(--text3);margin-top:2px">'+m.category+
            ' · '+(isR?(m.outcome==='YES'?'<span style="color:var(--yes)">YES WIN</span>':'<span style="color:var(--no)">NO WIN</span>'):'<span style="color:var(--yes)">● Open</span>')+
            (curYesP!=null&&!isR?' · '+curYesP+'¢ YES':'')+
            '</div>'+
          '</div>'+
          '<div style="text-align:right">'+
            '<div class="pcard-pnl '+pCls+'">'+(totalPnl>=0?'+':'')+fmt(totalPnl)+'</div>'+
            '<div style="font-size:9px;color:var(--text3);margin-top:2px">inv. '+fmt(pos.yes_cost+pos.no_cost)+'</div>'+
          '</div>'+
        '</div>'+
        '<div class="pcard-bd">';

      if(pos.yes_qty>=0.001){
        var yPnl=isR?(m.outcome==='YES'?Math.round((pos.yes_qty*(1-comm)-pos.yes_cost)*100)/100:Math.round(-pos.yes_cost*100)/100):pos.yes_upnl;
        var yC=yPnl==null?'':yPnl>0.01?' up':yPnl<-0.01?' dn':'';
        var bid=m.best_bid;
        html+='<div class="leg long">'+
          '<div class="leg-badge long">LONG YES</div>'+
          '<div class="leg-grid">'+
            '<div class="leg-col"><div class="fl">Size</div><div class="fv">'+pos.yes_qty.toFixed(2)+'</div></div>'+
            '<div class="leg-col"><div class="fl">Entry (WAP)</div><div class="fv">'+pos.yes_wap.toFixed(1)+'¢</div></div>'+
            '<div class="leg-col"><div class="fl">Current</div><div class="fv">'+(curYesP!=null?curYesP+'¢':'—')+'</div></div>'+
            '<div class="leg-col"><div class="fl">P&L</div><div class="fv'+yC+'">'+(yPnl!=null?(yPnl>=0?'+':'')+fmt(yPnl):'—')+'</div></div>'+
          '</div>'+
          (!isR?
            '<button class="leg-close"'+(bid?'':' disabled')+' data-mid="'+m.id+'" data-side="YES" onclick="portClosePos(this.dataset.mid,this.dataset.side,this)">'+
              (bid?'Close LONG @ '+bid+'¢ (market)':'No buyers')+
            '</button>'
          :'<div class="leg-settled" style="color:'+(m.outcome==='YES'?'var(--yes)':'var(--no)')+'">'+( m.outcome==='YES'?'✓ WIN':'✗ LOSS')+'</div>')+
        '</div>';
      }

      if(pos.no_qty>=0.001){
        var nPnl=isR?(m.outcome==='NO'?Math.round((pos.no_qty*(1-comm)-pos.no_cost)*100)/100:Math.round(-pos.no_cost*100)/100):pos.no_upnl;
        var nC=nPnl==null?'':nPnl>0.01?' up':nPnl<-0.01?' dn':'';
        var ask=m.best_ask;
        html+='<div class="leg short">'+
          '<div class="leg-badge short">LONG NO</div>'+
          '<div class="leg-grid">'+
            '<div class="leg-col"><div class="fl">Size</div><div class="fv">'+pos.no_qty.toFixed(2)+'</div></div>'+
            '<div class="leg-col"><div class="fl">Entry (WAP)</div><div class="fv">'+pos.no_wap.toFixed(1)+'¢</div></div>'+
            '<div class="leg-col"><div class="fl">Current NO</div><div class="fv">'+(curNoP!=null?curNoP+'¢':'—')+'</div></div>'+
            '<div class="leg-col"><div class="fl">P&L</div><div class="fv'+nC+'">'+(nPnl!=null?(nPnl>=0?'+':'')+fmt(nPnl):'—')+'</div></div>'+
          '</div>'+
          (!isR?
            '<button class="leg-close"'+(ask?'':' disabled')+' data-mid="'+m.id+'" data-side="NO" onclick="portClosePos(this.dataset.mid,this.dataset.side,this)">'+
              (ask?'Close LONG NO @ '+(100-ask)+'¢ (market)':'No sellers')+
            '</button>'
          :'<div class="leg-settled" style="color:'+(m.outcome==='NO'?'var(--yes)':'var(--no)')+'">'+( m.outcome==='NO'?'✓ WIN':'✗ LOSS')+'</div>')+
        '</div>';
      }

      if(pos.realized_pnl&&Math.abs(pos.realized_pnl)>0.001){
        var rClss=pos.realized_pnl>0?'var(--yes)':'var(--no)';
        html+='<div style="font-size:10px;font-family:var(--mono);color:var(--text3);padding:4px 0">'+
          'Realized P&L: <span style="color:'+rClss+';font-weight:600">'+(pos.realized_pnl>=0?'+':'')+fmt(pos.realized_pnl)+'</span></div>';
      }

      // Pending orders
      var pendH=p.pending_orders.filter(o=>o.market_id===m.id);
      if(pendH.length){
        html+='<div style="margin-top:6px;font-size:9px;color:var(--text3);font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Open Orders</div>';
        pendH.forEach(o=>{
          var rem=o.contracts-o.filled,iB=o.side==='BUY';
          html+='<div class="ord-entry">'+
            '<span style="color:'+(iB?'var(--yes)':'var(--no)')+';font-weight:600">'+(iB?'BUY YES':'SELL YES')+'</span>'+
            '<span>@ '+o.price+'¢</span>'+
            '<span style="color:var(--text3)">'+rem.toFixed(1)+'sh</span>'+
            '<button class="ord-cancel-btn" data-oid="'+o.id+'" onclick="portCancelOrd(this.dataset.oid)">Cancel</button>'+
          '</div>';
        });
      }

      html+='</div></div>';
    });

    el.innerHTML=html;
  }catch(e){el.innerHTML='<div style="color:var(--no);font-size:11px;padding:20px">Error loading portfolio: '+e.message+'</div>';}
}

async function portClosePos(mid,side,btn){
  if(!curU||!mid)return;
  btn.disabled=true;btn.textContent='Closing…';
  var prevMid=curMid;curMid=mid;
  try{
    var r=await api('/api/close_position','POST',{user_id:curU,market_id:mid,side});
    toast('Closed '+side+' @ '+r.price+'¢ · '+r.filled.toFixed(2)+' shares','ok');
    renderPort();
  }catch(e){toast(e.message,'err');btn.disabled=false;}
  curMid=prevMid;
}

async function portCancelOrd(oid){
  try{var r=await api('/api/order/cancel','POST',{order_id:oid,user_id:curU});toast('Cancelled · refund '+fmt(r.refund),'ok');renderPort();}
  catch(e){toast(e.message,'err');}
}

function jumpToMarket(mid){
  showTab('markets');
  setTimeout(()=>openMarket(mid),50);
}

// ════════════════════════════════════════════════════
// ACTIVITY
// ════════════════════════════════════════════════════
function renderAct(){
  var el=document.getElementById('actList');
  if(!actLog.length){el.innerHTML='<div style="color:var(--text3);font-size:11px;padding:20px;font-family:var(--mono)">No events</div>';return;}
  el.innerHTML='';actLog.slice(0,80).forEach(a=>el.appendChild(mkActEl(a)));
}
function prependAct(a){
  var el=document.getElementById('actList');
  if(el.querySelector('[style]'))el.innerHTML='';
  el.insertBefore(mkActEl(a),el.firstChild);
  while(el.children.length>80)el.removeChild(el.lastChild);
}
function mkActEl(a){
  var d=document.createElement('div');d.className='act-item';
  d.innerHTML='<div class="act-dot '+a.kind+'"></div><span style="flex:1">'+a.message+'</span><span class="act-ts">'+a.ts+'</span>';
  return d;
}

// ════════════════════════════════════════════════════
// USER HEADER
// ════════════════════════════════════════════════════
function renderHdr(){
  var u=usrs[curU];
  document.getElementById('hName').textContent=u?u.name:'Select user';
  document.getElementById('hBal').textContent=u?fmt(u.balance):'';
  var av=document.getElementById('hAv');av.textContent=u?u.name[0].toUpperCase():'?';av.style.background=u?u.color:'#333';
  document.getElementById('noUserBar').className='no-user-bar'+(curU?'':' show');
}
function renderDD(){
  var el=document.getElementById('ddList');el.innerHTML='';
  Object.values(usrs).forEach(u=>{
    var d=document.createElement('div');d.className='dd-item'+(u.id===curU?' sel':'');
    d.innerHTML='<div class="dav" style="background:'+u.color+'">'+u.name[0].toUpperCase()+'</div><span class="dn">'+u.name+'</span><span class="db">'+fmt(u.balance)+'</span>';
    d.addEventListener('click',()=>{curU=u.id;localStorage.setItem('betx2_curU',curU);renderHdr();renderDD();closeDD();toast('Active: '+u.name,'info');});
    el.appendChild(d);
  });
}
function showDD(){var p=document.getElementById('upill'),dd=document.getElementById('dd'),r=p.getBoundingClientRect();dd.style.top=(r.bottom+5)+'px';dd.style.right=(window.innerWidth-r.right)+'px';dd.classList.add('open');p.classList.add('open');}
function closeDD(){document.getElementById('dd').classList.remove('open');document.getElementById('upill').classList.remove('open');}
function toggleDD(){document.getElementById('dd').classList.contains('open')?closeDD():showDD();}

// ════════════════════════════════════════════════════
// SIMULATION
// ════════════════════════════════════════════════════
function updateSimBots(){
  var el=document.getElementById('simBotList');if(!el)return;
  var bots=Object.values(usrs);
  if(!bots.length){el.innerHTML='<div style="color:var(--text3);font-size:10px">No participants</div>';return;}
  el.innerHTML=bots.map(u=>'<div class="sim-bot-row"><div class="sim-bot-av" style="background:'+u.color+'">'+u.name[0]+'</div><span style="flex:1">'+u.name+'</span><span style="font-family:var(--mono);font-size:10px;color:var(--yes)">'+fmt(u.balance)+'</span></div>').join('');
}
function updateSimMarketSel(){
  var sel=document.getElementById('simMarketSel');if(!sel)return;
  var cur=sel.value||'ALL';
  sel.innerHTML='<option value="ALL">All open</option>';
  Object.values(mkts).filter(m=>m.status==='open').forEach(m=>{
    var o=document.createElement('option');o.value=m.id;o.textContent=m.title.slice(0,36);sel.appendChild(o);
  });
  sel.value=cur;
}
function getSimCenter(mid){if(!simCenterPrice[mid]){var m=mkts[mid];simCenterPrice[mid]=m?(m.last_price||m.mid_price||50):50;}return simCenterPrice[mid];}
function updSimStats(){document.getElementById('sTicks').textContent=simStats.ticks;document.getElementById('sTrades').textContent=simStats.trades;document.getElementById('sVol').textContent='$'+Math.round(simStats.volume);}
function simLog(msg){var el=document.getElementById('simLog');if(!el)return;var d=document.createElement('div');d.className='sim-log-entry';d.innerHTML='<span style="color:var(--text3)">'+new Date().toTimeString().slice(0,8)+'</span> '+msg;el.insertBefore(d,el.firstChild);while(el.children.length>60)el.removeChild(el.lastChild);}

async function doSimOrder(mid,bot){
  var m=mkts[mid];if(!m||m.status!=='open')return;
  var drift=document.getElementById('simDrift').checked;
  var spread=parseFloat(document.getElementById('simSpread').value)||4;
  var orderSize=parseFloat(document.getElementById('simOrderSize').value)||80;
  var aggr=document.getElementById('simAggression').value;
  var ob=m.orderbook||{bids:[],asks:[]};
  var center=getSimCenter(mid);
  if(drift){var target=parseFloat(document.getElementById('simCenterPrice').value)||50;var d2=(Math.random()-.48)*2.5+(target-center)*.04;center=Math.round(Math.min(94,Math.max(6,center+d2))*10)/10;simCenterPrice[mid]=center;document.getElementById('simCenterLbl').textContent='~'+center+'¢';}
  var isBuy=Math.random()>.5;
  var price;
  if(aggr==='aggressive'){price=isBuy?(ob.asks.length?ob.asks[0].price:Math.round(center+spread)):(ob.bids.length?ob.bids[0].price:Math.round(center-spread));}
  else if(aggr==='passive'){var delta=Math.random()*spread*.8+1;price=isBuy?Math.round(Math.min(99,Math.max(1,center-delta))*10)/10:Math.round(Math.min(99,Math.max(1,center+delta))*10)/10;}
  else{var isA=Math.random()<.4;if(isA){price=isBuy?(ob.asks.length?ob.asks[0].price:Math.round(center+2)):(ob.bids.length?ob.bids[0].price:Math.round(center-2));}else{var d3=Math.random()*spread+1;price=isBuy?Math.round(Math.min(99,Math.max(1,center-d3))*10)/10:Math.round(Math.min(99,Math.max(1,center+d3))*10)/10;}}
  price=Math.round(Math.min(99.9,Math.max(0.1,price))*10)/10;
  var maxD=Math.min(orderSize,Math.max(bot.balance*.05,5));
  var dollars=Math.round(maxD*(.6+Math.random()*.4)*100)/100;
  if(dollars<1||dollars>bot.balance)return;
  try{
    var r=await api('/api/order','POST',{user_id:bot.id,market_id:mid,side:isBuy?'BUY':'SELL',price,dollars});
    var sideHtml=isBuy?'<span style="color:var(--yes)">BUY</span>':'<span style="color:var(--no)">SELL</span>';
    var matched=r.trades>0?' <span style="color:var(--yes)">✓ '+r.trades+'</span>':'';
    simLog('<b>'+bot.name+'</b> '+sideHtml+' @'+price+'¢ $'+dollars.toFixed(0)+' <span style="color:var(--text3)">'+m.title.slice(0,20)+'</span>'+matched);
    if(r.trades>0){simStats.trades+=r.trades;simStats.volume+=dollars;}
  }catch(e){}
}

async function simStep(){
  simStats.ticks++;document.getElementById('simTickLbl').textContent='tick #'+simStats.ticks;updSimStats();
  var selMid=document.getElementById('simMarketSel').value;
  var open=Object.values(mkts).filter(m=>m.status==='open');
  if(!open.length){simLog('<span style="color:var(--gold)">No open markets</span>');return;}
  var targets=selMid==='ALL'?open:open.filter(m=>m.id===selMid);
  if(!targets.length)return;
  var m=targets[Math.floor(Math.random()*targets.length)];
  var bots=Object.values(usrs).filter(u=>u.balance>5);
  if(!bots.length)return;
  var bot=bots[Math.floor(Math.random()*bots.length)];
  await doSimOrder(m.id,bot);
}

function startSim(){
  if(simTimer)return;
  simStats={ticks:0,trades:0,volume:0};
  var interval=Math.max(300,(parseFloat(document.getElementById('simInterval').value)||1.5)*1000);
  simTimer=setInterval(simStep,interval);
  document.getElementById('simDot').className='sim-dot run';
  document.getElementById('simStatusTxt').textContent='Simulation running';
  document.getElementById('btnSimStart').style.display='none';
  document.getElementById('btnSimStop').style.display='';
  simLog('<span style="color:var(--yes)">▶ Started · interval '+interval+'ms</span>');
}
function stopSim(){
  if(simTimer){clearInterval(simTimer);simTimer=null;}
  document.getElementById('simDot').className='sim-dot';
  document.getElementById('simStatusTxt').textContent='Simulation stopped';
  document.getElementById('btnSimStart').style.display='';
  document.getElementById('btnSimStop').style.display='none';
  simLog('<span style="color:var(--no)">■ Stopped · '+simStats.trades+' trades $'+Math.round(simStats.volume)+'</span>');
}
async function burstSim(){
  var open=Object.values(mkts).filter(m=>m.status==='open');
  if(!open.length){toast('No open markets','err');return;}
  var bots=Object.values(usrs).filter(u=>u.balance>5);
  if(!bots.length){toast('No participants','err');return;}
  simLog('<span style="color:var(--gold)">⚡ Burst — 10 orders!</span>');
  for(var i=0;i<10;i++){
    var m=open[Math.floor(Math.random()*open.length)];
    var bot=bots[Math.floor(Math.random()*bots.length)];
    await doSimOrder(m.id,bot);
    await new Promise(r=>setTimeout(r,100));
  }
}

// ════════════════════════════════════════════════════
// TAB NAVIGATION
// ════════════════════════════════════════════════════
function showTab(name){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  var tab=document.querySelector('.nav-tab[data-panel="'+name+'"]');if(tab)tab.classList.add('active');
  if(name==='portfolio')renderPort();
  if(name==='activity')renderAct();
  if(name==='simulate'){updateSimBots();updateSimMarketSel();}
}

// ════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('.nav-tab').forEach(t=>{t.addEventListener('click',()=>showTab(t.dataset.panel));});
  document.getElementById('upill').addEventListener('click',toggleDD);
  document.addEventListener('click',e=>{if(!document.getElementById('upill').contains(e.target)&&!document.getElementById('dd').contains(e.target))closeDD();});
  document.getElementById('btnNewU').addEventListener('click',()=>{closeDD();document.getElementById('usrModal').classList.add('open');setTimeout(()=>document.getElementById('nuName').focus(),80);});
  document.getElementById('btnBack').addEventListener('click',closeMarket);
  document.getElementById('ofYes').addEventListener('click',()=>{selOutcome('YES');if(curOrderType==='market')updMktPrev();});
  document.getElementById('ofNo').addEventListener('click',()=>{selOutcome('NO');if(curOrderType==='market')updMktPrev();});
  document.getElementById('otLimit').addEventListener('click',()=>setOrderType('limit'));
  document.getElementById('otMarket').addEventListener('click',()=>setOrderType('market'));
  document.getElementById('priceSlider').addEventListener('input',e=>{document.getElementById('priceInp').value=e.target.value;updPreview();});
  document.getElementById('priceInp').addEventListener('input',e=>{document.getElementById('priceSlider').value=Math.round(parseFloat(e.target.value)||50);updPreview();});
  document.getElementById('dollarInp').addEventListener('input',updPreview);
  document.querySelectorAll('.of-q[data-v]').forEach(b=>b.addEventListener('click',()=>{document.getElementById('dollarInp').value=b.dataset.v;updPreview();}));
  document.getElementById('qMax').addEventListener('click',()=>{if(curU&&usrs[curU]){document.getElementById('dollarInp').value=Math.floor(usrs[curU].balance);updPreview();}});
  document.getElementById('subBtn').addEventListener('click',submitOrder);
  document.getElementById('mktDollarInp').addEventListener('input',updMktPrev);
  document.querySelectorAll('.mktq').forEach(b=>b.addEventListener('click',()=>{document.getElementById('mktDollarInp').value=b.dataset.v;updMktPrev();}));
  document.getElementById('mktqMax').addEventListener('click',()=>{if(curU&&usrs[curU]){document.getElementById('mktDollarInp').value=Math.floor(usrs[curU].balance);updMktPrev();}});
  document.getElementById('mktBtn').addEventListener('click',submitMktOrder);
  document.getElementById('btnCloseU').addEventListener('click',()=>document.getElementById('usrModal').classList.remove('open'));
  document.getElementById('usrModal').addEventListener('click',e=>{if(e.target===e.currentTarget)e.currentTarget.classList.remove('open');});
  document.getElementById('nuName').addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('btnCrtU').click();});
  document.getElementById('btnCrtU').addEventListener('click',async()=>{
    var name=document.getElementById('nuName').value.trim();
    if(!name){toast('Enter a name','err');return;}
    try{
      var u=await api('/api/users','POST',{name,balance:parseFloat(document.getElementById('nuBal').value)||5000});
      usrs[u.id]=u;curU=u.id;localStorage.setItem('betx2_curU',curU);
      renderHdr();renderDD();
      document.getElementById('usrModal').classList.remove('open');
      toast('Welcome, '+u.name+'!','ok');
    }catch(e){toast(e.message,'err');}
  });
  document.getElementById('btnSimStart').addEventListener('click',startSim);
  document.getElementById('btnSimStop').addEventListener('click',stopSim);
  document.getElementById('btnSimBurst').addEventListener('click',burstSim);
  document.getElementById('btnAddBot').addEventListener('click',async()=>{
    var used=Object.values(usrs).map(u=>u.name);
    var avail=BOT_NAMES.filter(n=>!used.includes(n));
    var name=avail.length?avail[0]:'Bot-'+Math.floor(Math.random()*999);
    var bal=parseFloat(document.getElementById('simBotBal').value)||5000;
    try{var u=await api('/api/users','POST',{name,balance:bal});usrs[u.id]=u;updateSimBots();toast('Bot '+name+' added','ok');}
    catch(e){toast(e.message,'err');}
  });
  document.getElementById('btnCreate').addEventListener('click',async()=>{
    var t=document.getElementById('nTitle').value.trim();
    if(!t){toast('Enter a question','err');return;}
    try{await api('/api/markets','POST',{title:t,desc:document.getElementById('nDesc').value.trim(),category:document.getElementById('nCat').value});document.getElementById('nTitle').value='';document.getElementById('nDesc').value='';showTab('markets');toast('Market created!','ok');}
    catch(e){toast(e.message,'err');}
  });
  document.getElementById('btnSaveCfg').addEventListener('click',async()=>{
    var v=parseFloat(document.getElementById('cfgComm').value)||2;
    try{await api('/api/config','POST',{commission_pct:v/100});toast('Saved','ok');}
    catch(e){toast(e.message,'err');}
  });
  document.getElementById('btnReset').addEventListener('click',()=>document.getElementById('resetModal').classList.add('open'));
  document.getElementById('btnResetCancel').addEventListener('click',()=>document.getElementById('resetModal').classList.remove('open'));
  document.getElementById('btnResetNo').addEventListener('click',()=>document.getElementById('resetModal').classList.remove('open'));
  document.getElementById('resetModal').addEventListener('click',e=>{if(e.target===e.currentTarget)e.currentTarget.classList.remove('open');});
  document.getElementById('btnResetYes').addEventListener('click',async function(){
    this.disabled=true;this.textContent='Resetting…';
    try{await api('/api/reset','POST');document.getElementById('resetModal').classList.remove('open');}
    catch(e){toast(e.message,'err');this.disabled=false;this.textContent='Reset';}
  });
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'){closeMarket();document.querySelectorAll('.modal-ovl').forEach(m=>m.classList.remove('open'));closeDD();}
  });
  connect();
  setTimeout(()=>{if(!Object.keys(mkts).length)loadHttp();},2000);
});
</script>
</body>
</html>"""