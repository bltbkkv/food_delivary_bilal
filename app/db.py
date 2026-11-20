"""База онлайн-заказов: филиалы, меню (приходит с кассы), клиенты, заказы, бонусы, акции.

Деньги — целые сомы. Время — Бишкек (UTC+6), строки ISO.
Цены и состав заказа всегда считаются на сервере: клиенту верить нельзя.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import sqlite3
import threading

BISHKEK = dt.timezone(dt.timedelta(hours=6))
ACTIVE = ("new", "accepted", "ready")
STATUS_TEXT = {"new": "Ждёт подтверждения", "accepted": "Готовится", "ready": "Готов",
               "done": "Выдан", "rejected": "Отклонён", "cancelled": "Отменён"}
KINDS = ("delivery", "pickup", "table")
PAYS = ("cash", "transfer")

DEFAULTS = {
    "cashback_pct": "5",        # % бонусами с оплаченной суммы
    "max_spend_pct": "30",      # сколько % заказа можно оплатить бонусами
    "welcome_bonus": "100",     # за первый выполненный заказ
    "referral_bonus": "150",    # пригласившему, когда друг получил первый заказ
    "bonus_days": "90",         # бонусы сгорают, если клиент не заказывал столько дней
    "big_order": "3000",        # от этой суммы кассир сначала звонит клиенту
    "happy_from": "",           # «двойные бонусы»: с … по … (ЧЧ:ММ), пусто — выключено
    "happy_to": "",
    "happy_mult": "2",
    "no_show_limit": "2",       # столько раз не забрал — дальше только с переводом
    "brand": "Bilal",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    open_from TEXT NOT NULL DEFAULT '09:00',
    open_to TEXT NOT NULL DEFAULT '02:00',
    delivery INTEGER NOT NULL DEFAULT 1,
    delivery_fee INTEGER NOT NULL DEFAULT 0,
    free_from INTEGER NOT NULL DEFAULT 0,
    min_order INTEGER NOT NULL DEFAULT 0,
    qr_file TEXT,
    pay_note TEXT NOT NULL DEFAULT '',
    pos_token TEXT UNIQUE,
    pos_seen TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS products (
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    pos_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    image_key TEXT,
    img_file TEXT,
    sort INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (branch_id, pos_id)
);
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    ref_code TEXT NOT NULL UNIQUE,
    referred_by INTEGER REFERENCES clients(id),
    no_show INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_activity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    trusted INTEGER NOT NULL DEFAULT 0,      -- можно тратить бонусы (после выданного заказа с этого телефона)
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    client_id INTEGER REFERENCES clients(id),
    token TEXT,
    kind TEXT NOT NULL,
    table_no TEXT,
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    lat REAL, lon REAL,
    comment TEXT NOT NULL DEFAULT '',
    want_time TEXT NOT NULL DEFAULT '',
    pay TEXT NOT NULL DEFAULT 'cash',
    check_file TEXT,
    paid INTEGER NOT NULL DEFAULT 0,
    items TEXT NOT NULL,
    subtotal INTEGER NOT NULL,
    delivery_fee INTEGER NOT NULL DEFAULT 0,
    bonus_spent INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL,
    cashback_pct REAL NOT NULL DEFAULT 0,
    bonus_earned INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    reason TEXT NOT NULL DEFAULT '',
    need_call INTEGER NOT NULL DEFAULT 0,
    called INTEGER NOT NULL DEFAULT 0,
    pos_order_id INTEGER,
    pos_number INTEGER,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_branch ON orders(branch_id, status);
CREATE INDEX IF NOT EXISTS orders_client ON orders(client_id, id);
CREATE TABLE IF NOT EXISTS bonus (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    amount INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- earn, spend, refund, welcome, referral, manual, expire
    order_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bonus_client ON bonus(client_id);
CREATE TABLE IF NOT EXISTS promos (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_sessions (token TEXT PRIMARY KEY, created TEXT NOT NULL);
"""


class Error(Exception):
    """Понятная человеку причина — уходит в ответ API."""

    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.status = status


def now() -> dt.datetime:
    return dt.datetime.now(BISHKEK).replace(microsecond=0, tzinfo=None)


def phone_norm(raw) -> str:
    """0555 123 456 / +996 555 123456 / 555123456 → 996555123456."""
    d = re.sub(r"\D", "", str(raw or ""))
    if d.startswith("0") and len(d) == 10:
        d = "996" + d[1:]
    elif len(d) == 9:
        d = "996" + d
    if not re.fullmatch(r"996\d{9}", d):
        raise Error("Проверьте номер телефона: 0555 123 456")
    return d


def phone_view(p: str) -> str:
    return f"0{p[3:6]} {p[6:9]} {p[9:]}" if p and len(p) == 12 else (p or "")


def hm(s: str) -> int | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s or "").strip())
    return int(m[1]) * 60 + int(m[2]) if m and int(m[1]) < 24 and int(m[2]) < 60 else None


def in_window(t: dt.datetime, start: str, end: str) -> bool:
    """Время внутри окна; окно может переходить через полночь (09:00–02:00)."""
    a, b = hm(start), hm(end)
    if a is None or b is None or a == b:
        return a is not None and a == b  # 00:00–00:00 = круглосуточно
    m = t.hour * 60 + t.minute
    return a <= m < b if a < b else (m >= a or m < b)


class DB:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    # ---------- низкий уровень ----------
    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def x(self, sql, args=()) -> int:
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    def tx(self):
        """with db.tx(): … — несколько изменений разом (или ни одного)."""
        db = self

        class T:
            def __enter__(self):
                db.lock.acquire()
                db.db.execute("BEGIN IMMEDIATE")

            def __exit__(self, et, *_):
                try:
                    db.db.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    db.lock.release()
        return T()

    # ---------- настройки ----------
    def setting(self, key: str) -> str:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else DEFAULTS.get(key, "")

    def num(self, key: str) -> float:
        try:
            return float(self.setting(key) or 0)
        except ValueError:
            return float(DEFAULTS.get(key) or 0)

    def settings(self) -> dict:
        out = dict(DEFAULTS)
        out.update({r["key"]: r["value"] for r in self.q("SELECT * FROM settings")})
        return out

    def save_settings(self, data: dict):
        for k, v in data.items():
            if k not in DEFAULTS:
                continue
            v = str(v if v is not None else "").strip()[:40]
            if k.startswith("happy_") and k != "happy_mult" and v and hm(v) is None:
                raise Error("Время акции в формате ЧЧ:ММ, например 14:00")
            if k not in ("brand", "happy_from", "happy_to"):
                try:
                    if float(v or 0) < 0:
                        raise ValueError
                except ValueError:
                    raise Error(f"Неверное число: {v}")
            self.x("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (k, v))

    # ---------- филиалы ----------
    def branches(self, all_=False):
        return self.q("SELECT * FROM branches " + ("" if all_ else "WHERE active=1 ") + "ORDER BY name")

    def branch(self, bid):
        return self.one("SELECT * FROM branches WHERE id=?", (bid,))

    def branch_by_token(self, token: str):
        if not token:
            return None
        return self.one("SELECT * FROM branches WHERE pos_token=? AND active=1", (token,))

    BRANCH_FIELDS = {"name": str, "address": str, "phone": str, "open_from": str, "open_to": str,
                     "delivery": int, "delivery_fee": int, "free_from": int, "min_order": int,
                     "pay_note": str, "paused": int, "active": int}

    def save_branch(self, data: dict) -> int:
        vals = {}
        for k, typ in self.BRANCH_FIELDS.items():
            if k in data:
                v = data[k]
                try:
                    vals[k] = str(v or "").strip()[:200] if typ is str else max(0, int(float(v or 0)))
                except ValueError:
                    raise Error(f"Неверное число в поле {k}")
        for k in ("open_from", "open_to"):
            if k in vals and hm(vals[k]) is None:
                raise Error("Часы работы в формате ЧЧ:ММ, например 09:00")
        bid = data.get("id")
        if not bid:
            if not vals.get("name"):
                raise Error("Введите название филиала")
            vals["pos_token"] = secrets.token_urlsafe(24)
            cols = ",".join(vals)
            return self.x(f"INSERT INTO branches({cols}) VALUES ({','.join('?' * len(vals))})", tuple(vals.values()))
        if "name" in vals and not vals["name"]:
            raise Error("Введите название филиала")
        if vals:
            self.x(f"UPDATE branches SET {','.join(k + '=?' for k in vals)} WHERE id=?", (*vals.values(), int(bid)))
        return int(bid)

    def new_pos_token(self, bid) -> str:
        t = secrets.token_urlsafe(24)
        self.x("UPDATE branches SET pos_token=? WHERE id=?", (t, bid))
        return t

    def is_open(self, b, t: dt.datetime | None = None) -> bool:
        return bool(b["active"]) and not b["paused"] and in_window(t or now(), b["open_from"], b["open_to"])

    # ---------- меню (с кассы) ----------
    def sync_menu(self, bid: int, products: list) -> list[int]:
        """Касса прислала свои блюда. Возвращает, для каких блюд нужна новая фотография."""
        need, seen = [], set()
        with self.tx():
            old = {r["pos_id"]: r for r in self.db.execute("SELECT * FROM products WHERE branch_id=?", (bid,))}
            for i, p in enumerate(products[:2000]):
                try:
                    pid, price = int(p["id"]), int(round(float(p.get("price") or 0)))
                except (KeyError, TypeError, ValueError):
                    continue
                name = str(p.get("name") or "").strip()[:100]
                if not name or price <= 0:
                    continue
                seen.add(pid)
                key = str(p.get("image_key") or "")[:200] or None
                cat = str(p.get("category") or "").strip()[:50] or "Меню"
                active = 1 if p.get("active", True) else 0
                sort = int(p.get("sort") or i)
                o = old.get(pid)
                if o:
                    self.db.execute("UPDATE products SET name=?, category=?, price=?, active=?, image_key=?, sort=? "
                                    "WHERE branch_id=? AND pos_id=?", (name, cat, price, active, key, sort, bid, pid))
                    if key and (key != o["image_key"] or not o["img_file"]):
                        need.append(pid)
                    if not key and o["img_file"]:
                        self.db.execute("UPDATE products SET img_file=NULL WHERE branch_id=? AND pos_id=?", (bid, pid))
                else:
                    self.db.execute("INSERT INTO products(branch_id,pos_id,name,category,price,active,image_key,sort) "
                                    "VALUES (?,?,?,?,?,?,?,?)", (bid, pid, name, cat, price, active, key, sort))
                    if key:
                        need.append(pid)
            for pid in set(old) - seen:  # на кассе блюдо удалили — на сайте прячем
                self.db.execute("UPDATE products SET active=0 WHERE branch_id=? AND pos_id=?", (bid, pid))
        return need

    def set_image(self, bid, pid, key, fname):
        self.x("UPDATE products SET img_file=?, image_key=? WHERE branch_id=? AND pos_id=?", (fname, key, bid, pid))

    def menu(self, bid):
        return self.q("SELECT * FROM products WHERE branch_id=? AND active=1 ORDER BY sort, name", (bid,))

    # ---------- клиенты ----------
    def client_by_token(self, token):
        if not token:
            return None, None
        t = self.one("SELECT * FROM tokens WHERE token=?", (str(token)[:64],))
        if not t:
            return None, None
        return self.one("SELECT * FROM clients WHERE id=?", (t["client_id"],)), t

    def client_by_phone(self, phone):
        return self.one("SELECT * FROM clients WHERE phone=?", (phone,))

    def client_by_ref(self, code):
        return self.one("SELECT * FROM clients WHERE ref_code=?", (str(code or "")[:20].upper(),)) if code else None

    def _new_client(self, phone, name, ref) -> int:
        t = now().isoformat()
        for _ in range(20):
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
            if not self.db.execute("SELECT 1 FROM clients WHERE ref_code=?", (code,)).fetchone():
                break
        friend = self.db.execute("SELECT id FROM clients WHERE ref_code=?",
                                 (str(ref or "")[:20].upper(),)).fetchone() if ref else None
        return self.db.execute("INSERT INTO clients(phone,name,ref_code,referred_by,created,last_activity) "
                               "VALUES (?,?,?,?,?,?)", (phone, name, code, friend["id"] if friend else None, t, t)).lastrowid

    def balance(self, cid) -> int:
        r = self.one("SELECT COALESCE(SUM(amount),0) s FROM bonus WHERE client_id=?", (cid,))
        return int(r["s"])

    def expire_bonus(self, client):
        """Бонусы сгорают, если клиент давно не заказывал (срок — в настройках)."""
        days = int(self.num("bonus_days"))
        if not client or days <= 0:
            return
        last = dt.datetime.fromisoformat(client["last_activity"])
        bal = self.balance(client["id"])
        if bal > 0 and now() - last > dt.timedelta(days=days):
            self.x("INSERT INTO bonus(client_id,amount,kind,note,created) VALUES (?,?,?,?,?)",
                   (client["id"], -bal, "expire", f"не было заказов {days} дней", now().isoformat()))

    def add_bonus(self, cid, amount, kind, order_id=None, note=""):
        self.x("INSERT INTO bonus(client_id,amount,kind,order_id,note,created) VALUES (?,?,?,?,?,?)",
               (cid, int(amount), kind, order_id, str(note)[:200], now().isoformat()))

    def bonus_history(self, cid, limit=30):
        return self.q("SELECT * FROM bonus WHERE client_id=? ORDER BY id DESC LIMIT ?", (cid, limit))

    def cashback_pct(self, t: dt.datetime | None = None) -> float:
        pct = self.num("cashback_pct")
        a, b = self.setting("happy_from"), self.setting("happy_to")
        if a and b and in_window(t or now(), a, b):
            pct *= self.num("happy_mult") or 1
        return pct

    # ---------- заказ ----------
    def create_order(self, data: dict, token: str | None) -> dict:
        b = self.branch(int(data.get("branch_id") or 0))
        if not b or not b["active"]:
            raise Error("Выберите филиал")
        if not self.is_open(b):
            raise Error(f"Филиал сейчас не принимает заказы. Часы работы: {b['open_from']}–{b['open_to']}")
        kind = data.get("kind")
        if kind not in KINDS:
            raise Error("Выберите: доставка, самовывоз или в зале")
        if kind == "delivery" and not b["delivery"]:
            raise Error("Этот филиал не делает доставку — выберите самовывоз")
        table_no = str(data.get("table_no") or "").strip()[:10] if kind == "table" else None
        if kind == "table" and not table_no:
            raise Error("Не указан стол — отсканируйте QR на столе ещё раз")
        name = str(data.get("name") or "").strip()[:60]
        raw_phone = str(data.get("phone") or "").strip()
        phone = phone_norm(raw_phone) if (raw_phone or kind != "table") else ""
        if kind != "table" and not name:
            raise Error("Как к вам обращаться?")
        address = str(data.get("address") or "").strip()[:300]
        if kind == "delivery" and len(address) < 5:
            raise Error("Укажите адрес доставки")
        pay = data.get("pay") if data.get("pay") in PAYS else "cash"
        if kind == "table":
            pay = "cash"  # в зале — оплата на кассе

        # состав: цены берём из меню, а не из браузера
        menu = {p["pos_id"]: p for p in self.menu(b["id"])}
        items, subtotal = [], 0
        for it in (data.get("items") or [])[:60]:
            try:
                pid, qty = int(it.get("id")), int(it.get("qty"))
            except (TypeError, ValueError, AttributeError):
                continue
            p = menu.get(pid)
            if not p:
                raise Error("Некоторых блюд уже нет в меню — обновите страницу")
            if not 1 <= qty <= 50:
                raise Error("Слишком большое количество")
            items.append({"id": pid, "name": p["name"], "price": p["price"], "qty": qty})
            subtotal += p["price"] * qty
        if not items:
            raise Error("Корзина пуста")
        if subtotal < b["min_order"] and kind == "delivery":
            raise Error(f"Минимальный заказ на доставку — {b['min_order']} сом")
        fee = b["delivery_fee"] if kind == "delivery" and not (b["free_from"] and subtotal >= b["free_from"]) else 0

        with self.tx():
            client, tok = None, None
            if phone:
                client = self.db.execute("SELECT * FROM clients WHERE phone=?", (phone,)).fetchone()
                if not client:
                    client = self.db.execute("SELECT * FROM clients WHERE id=?",
                                             (self._new_client(phone, name, data.get("ref")),)).fetchone()
                elif name and name != client["name"]:
                    self.db.execute("UPDATE clients SET name=? WHERE id=?", (name, client["id"]))
                t = self.db.execute("SELECT * FROM tokens WHERE token=?", (str(token or "")[:64],)).fetchone()
                if t and t["client_id"] == client["id"]:
                    tok = t
                else:  # новое устройство или другой номер — новый ключ (бонусы тратить пока нельзя)
                    nt = secrets.token_urlsafe(24)
                    self.db.execute("INSERT INTO tokens(token,client_id,created) VALUES (?,?,?)",
                                    (nt, client["id"], now().isoformat()))
                    tok = self.db.execute("SELECT * FROM tokens WHERE token=?", (nt,)).fetchone()
                active = self.db.execute("SELECT COUNT(*) FROM orders WHERE client_id=? AND status IN "
                                         "('new','accepted','ready')", (client["id"],)).fetchone()[0]
                if active >= 3:
                    raise Error("У вас уже 3 активных заказа — дождитесь их выдачи")
                if pay == "cash" and kind != "table" and client["no_show"] >= int(self.num("no_show_limit") or 99):
                    raise Error("Для этого номера доступна только оплата переводом заранее")

            spent = 0
            if data.get("use_bonus") and client and tok and tok["trusted"]:
                bal = int(self.db.execute("SELECT COALESCE(SUM(amount),0) FROM bonus WHERE client_id=?",
                                          (client["id"],)).fetchone()[0])
                spent = max(0, min(bal, int(subtotal * self.num("max_spend_pct") / 100)))
            total = subtotal - spent + fee
            t = now().isoformat()
            for _ in range(20):
                code = "".join(secrets.choice("23456789ABCDEFGHJKMNPQRSTUVWXYZ") for _ in range(8))
                if not self.db.execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
                    break
            oid = self.db.execute(
                "INSERT INTO orders(code,branch_id,client_id,token,kind,table_no,name,phone,address,lat,lon,comment,"
                "want_time,pay,items,subtotal,delivery_fee,bonus_spent,total,cashback_pct,need_call,created,updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, b["id"], client["id"] if client else None, tok["token"] if tok else None, kind, table_no,
                 name, phone, address if kind == "delivery" else "", _coord(data.get("lat"), 90),
                 _coord(data.get("lon"), 180), str(data.get("comment") or "").strip()[:300],
                 str(data.get("want_time") or "").strip()[:20], pay, json.dumps(items, ensure_ascii=False),
                 subtotal, fee, spent, total, self.cashback_pct() if client else 0,
                 1 if total >= self.num("big_order") and kind != "table" else 0, t, t)).lastrowid
            if spent:
                self.db.execute("INSERT INTO bonus(client_id,amount,kind,order_id,note,created) VALUES (?,?,?,?,?,?)",
                                (client["id"], -spent, "spend", oid, "", t))
            if client:
                self.db.execute("UPDATE clients SET last_activity=? WHERE id=?", (t, client["id"]))
        return {"code": code, "token": tok["token"] if tok else None, "id": oid}

    def order(self, oid):
        return self.one("SELECT * FROM orders WHERE id=?", (oid,))

    def order_by_code(self, code):
        return self.one("SELECT * FROM orders WHERE code=?", (str(code or "")[:20],))

    def client_orders(self, cid, limit=10):
        return self.q("SELECT * FROM orders WHERE client_id=? ORDER BY id DESC LIMIT ?", (cid, limit))

    def branch_active(self, bid):
        return self.q("SELECT * FROM orders WHERE branch_id=? AND (status IN ('new','accepted','ready') "
                      "OR (status IN ('done','rejected','cancelled') AND updated >= ?)) ORDER BY id",
                      (bid, (now() - dt.timedelta(hours=1)).isoformat()))

    def set_check(self, oid, fname):
        self.x("UPDATE orders SET check_file=?, updated=? WHERE id=?", (fname, now().isoformat(), oid))

    def _close(self, o, status, reason=""):
        """Отклонён / отменён: вернуть потраченные бонусы."""
        t = now().isoformat()
        self.db.execute("UPDATE orders SET status=?, reason=?, updated=? WHERE id=?", (status, reason[:200], t, o["id"]))
        if o["bonus_spent"] and o["client_id"]:
            self.db.execute("INSERT INTO bonus(client_id,amount,kind,order_id,note,created) VALUES (?,?,?,?,?,?)",
                            (o["client_id"], o["bonus_spent"], "refund", o["id"], "заказ не состоялся", t))

    def _done(self, o):
        """Выдан: кэшбэк, бонус за первый заказ, бонус пригласившему; телефон теперь «проверенный»."""
        t = now().isoformat()
        earned = 0
        if o["client_id"]:
            base = max(0, o["total"] - o["delivery_fee"])
            earned = int(base * o["cashback_pct"] / 100)
            if earned:
                self.db.execute("INSERT INTO bonus(client_id,amount,kind,order_id,note,created) VALUES (?,?,?,?,?,?)",
                                (o["client_id"], earned, "earn", o["id"], f"{o['cashback_pct']:g}%", t))
            first = not self.db.execute("SELECT 1 FROM orders WHERE client_id=? AND status='done' AND id<>?",
                                        (o["client_id"], o["id"])).fetchone()
            if first:
                w = int(self.num("welcome_bonus"))
                if w:
                    self.db.execute("INSERT INTO bonus(client_id,amount,kind,order_id,note,created) "
                                    "VALUES (?,?,?,?,?,?)", (o["client_id"], w, "welcome", o["id"], "первый заказ", t))
                c = self.db.execute("SELECT * FROM clients WHERE id=?", (o["client_id"],)).fetchone()
                rb = int(self.num("referral_bonus"))
                if c["referred_by"] and rb:
                    self.db.execute("INSERT INTO bonus(client_id,amount,kind,order_id,note,created) "
                                    "VALUES (?,?,?,?,?,?)", (c["referred_by"], rb, "referral", o["id"],
                                                             f"друг {c['name'] or phone_view(c['phone'])}", t))
            if o["token"]:
                self.db.execute("UPDATE tokens SET trusted=1 WHERE token=?", (o["token"],))
            self.db.execute("UPDATE clients SET last_activity=? WHERE id=?", (t, o["client_id"]))
        self.db.execute("UPDATE orders SET status='done', bonus_earned=?, updated=? WHERE id=?", (earned, t, o["id"]))

    def pos_action(self, bid: int, oid: int, action: str, data: dict) -> dict:
        """Кнопки кассира: принять, отклонить, позвонил, оплата получена, готов, выдан, не забрал."""
        with self.tx():
            o = self.db.execute("SELECT * FROM orders WHERE id=? AND branch_id=?", (oid, bid)).fetchone()
            if not o:
                raise Error("Заказ не найден", 404)
            st, t = o["status"], now().isoformat()
            if action == "called":
                self.db.execute("UPDATE orders SET called=1, updated=? WHERE id=?", (t, oid))
            elif action == "paid":
                self.db.execute("UPDATE orders SET paid=?, updated=? WHERE id=?", (0 if data.get("undo") else 1, t, oid))
            elif action == "accept":
                if st != "new":
                    raise Error("Заказ уже обработан")
                if o["need_call"] and not o["called"]:
                    raise Error("Большой заказ — сначала позвоните клиенту")
                self.db.execute("UPDATE orders SET status='accepted', pos_order_id=?, pos_number=?, updated=? "
                                "WHERE id=?", (_int(data.get("pos_order_id")), _int(data.get("pos_number")), t, oid))
            elif action == "ready":
                if st not in ("accepted", "ready"):
                    raise Error("Сначала примите заказ")
                self.db.execute("UPDATE orders SET status='ready', updated=? WHERE id=?", (t, oid))
            elif action == "done":
                if st not in ("accepted", "ready"):
                    raise Error("Сначала примите заказ")
                self._done(o)
            elif action in ("reject", "cancel"):
                if st not in ACTIVE:
                    raise Error("Заказ уже закрыт")
                self._close(o, "rejected" if action == "reject" else "cancelled", str(data.get("reason") or ""))
            elif action == "no_show":
                if st not in ACTIVE:
                    raise Error("Заказ уже закрыт")
                self._close(o, "cancelled", "клиент не забрал заказ")
                if o["client_id"]:
                    self.db.execute("UPDATE clients SET no_show=no_show+1 WHERE id=?", (o["client_id"],))
            else:
                raise Error("Неизвестное действие")
        return dict(self.order(oid))

    def client_cancel(self, code, token) -> None:
        with self.tx():
            o = self.db.execute("SELECT * FROM orders WHERE code=?", (str(code or "")[:20],)).fetchone()
            if not o or not token or o["token"] != token:
                raise Error("Заказ не найден", 404)
            if o["status"] != "new":
                raise Error("Заказ уже готовят — отменить можно только по телефону филиала")
            self._close(o, "cancelled", "отменён клиентом")

    def pos_statuses(self, bid, statuses: list):
        """Касса сообщает, что стало с принятыми заказами: «вызван» → готов, закрыт/оплачен → выдан, отменён."""
        for s in statuses[:200]:
            try:
                oid, pst = int(s["id"]), str(s["pos_status"])
            except (KeyError, TypeError, ValueError):
                continue
            o = self.one("SELECT * FROM orders WHERE id=? AND branch_id=?", (oid, bid))
            if not o or o["status"] not in ("accepted", "ready"):
                continue
            try:
                if pst == "called" and o["status"] == "accepted":
                    self.pos_action(bid, oid, "ready", {})
                elif pst == "ready":
                    self.pos_action(bid, oid, "done", {})
                elif pst == "cancelled":
                    self.pos_action(bid, oid, "cancel", {"reason": "отменён на кассе"})
            except Error:
                pass

    def touch_pos(self, bid):
        self.x("UPDATE branches SET pos_seen=? WHERE id=?", (now().isoformat(), bid))

    # ---------- акции ----------
    def promos(self, all_=False):
        return self.q("SELECT * FROM promos " + ("" if all_ else "WHERE active=1 ") + "ORDER BY sort, id")

    def save_promo(self, data):
        title = str(data.get("title") or "").strip()[:80]
        if data.get("delete") and data.get("id"):
            self.x("DELETE FROM promos WHERE id=?", (int(data["id"]),))
            return
        if not title:
            raise Error("Введите заголовок акции")
        text, active = str(data.get("text") or "").strip()[:300], 1 if data.get("active", True) else 0
        if data.get("id"):
            self.x("UPDATE promos SET title=?, text=?, active=? WHERE id=?", (title, text, active, int(data["id"])))
        else:
            self.x("INSERT INTO promos(title,text,active) VALUES (?,?,?)", (title, text, active))

    # ---------- админ ----------
    def stats(self, days: int):
        since = (now() - dt.timedelta(days=days)).isoformat()
        rows = self.q("SELECT b.name, COUNT(o.id) n, COALESCE(SUM(o.total),0) s, "
                      "SUM(o.kind='delivery') d, SUM(o.kind='pickup') p, SUM(o.kind='table') t "
                      "FROM branches b LEFT JOIN orders o ON o.branch_id=b.id AND o.status='done' AND o.created>=? "
                      "WHERE b.active=1 GROUP BY b.id ORDER BY b.name", (since,))
        bonus = self.one("SELECT COALESCE(SUM(CASE WHEN amount>0 AND kind<>'refund' THEN amount END),0) given, "
                         "COALESCE(-SUM(CASE WHEN kind='spend' THEN amount END),0) - "
                         "COALESCE(SUM(CASE WHEN kind='refund' THEN amount END),0) spent FROM bonus WHERE created>=?",
                         (since,))
        new_clients = self.one("SELECT COUNT(*) c FROM clients WHERE created>=?", (since,))["c"]
        refs = self.one("SELECT COUNT(*) c FROM bonus WHERE kind='referral' AND created>=?", (since,))["c"]
        return {"branches": [dict(r) for r in rows], "bonus_given": bonus["given"], "bonus_spent": bonus["spent"],
                "new_clients": new_clients, "referrals": refs}

    def find_clients(self, q: str):
        q = str(q or "").strip()
        digits = re.sub(r"\D", "", q)
        if digits.startswith("0"):
            digits = digits[1:]
        rows = self.q("SELECT c.*, (SELECT COALESCE(SUM(amount),0) FROM bonus WHERE client_id=c.id) bal, "
                      "(SELECT COUNT(*) FROM orders WHERE client_id=c.id AND status='done') n "
                      "FROM clients c WHERE (?='' OR c.phone LIKE ? OR c.name LIKE ?) ORDER BY c.last_activity DESC "
                      "LIMIT 50", (q, f"%{digits or '#'}%", f"%{q}%"))
        return rows

    def new_admin_session(self) -> str:
        t = secrets.token_urlsafe(32)
        self.x("INSERT INTO admin_sessions(token,created) VALUES (?,?)", (t, now().isoformat()))
        return t

    def admin_ok(self, token) -> bool:
        if not token:
            return False
        r = self.one("SELECT created FROM admin_sessions WHERE token=?", (str(token)[:64],))
        return bool(r and now() - dt.datetime.fromisoformat(r["created"]) < dt.timedelta(days=30))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coord(v, lim):
    try:
        f = float(v)
        return f if -lim <= f <= lim else None
    except (TypeError, ValueError):
        return None
