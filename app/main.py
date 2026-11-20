"""Сервер онлайн-заказов. Без внешних библиотек: стандартный Python + SQLite.

Три стороны:
  • сайт клиента (/)              — меню, корзина, заказ, статус, бонусы, «пригласи друга»;
  • касса в филиале (/api/pos/*)   — сама забирает новые заказы и присылает меню (ключ кассы);
  • владелец (/admin)              — филиалы, правила бонусов, акции, клиенты, статистика.

Запуск: python -m app.main  (порт из PORT, база в DATA_DIR)
"""
from __future__ import annotations

import base64
import datetime as dt
import binascii
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .db import DB, STATUS_TEXT, Error, now, phone_view

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))
IMG, CHECKS = DATA / "img", DATA / "checks"
SECRET = os.getenv("SECRET", "") or "dev-secret-change-me"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
MAX_BODY = 4 * 1024 * 1024
MAX_IMAGE = 3 * 1024 * 1024

db: DB = None  # создаётся в make_db()


def make_db(path: str | None = None) -> DB:
    global db
    DATA.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)
    CHECKS.mkdir(parents=True, exist_ok=True)
    db = DB(path or str(DATA / "orders.db"))
    return db


# ---------- защита от спама ----------
_hits: dict[tuple, list] = {}
_hits_lock = threading.Lock()


def limit(key: tuple, n: int, seconds: int):
    t = time.time()
    with _hits_lock:
        arr = [x for x in _hits.get(key, []) if t - x < seconds]
        if len(arr) >= n:
            raise Error("Слишком много попыток — подождите несколько минут", 429)
        arr.append(t)
        _hits[key] = arr
        if len(_hits) > 5000:
            _hits.clear()


def sign(name: str) -> str:
    return hmac.new(SECRET.encode(), name.encode(), hashlib.sha256).hexdigest()[:20]


def save_image(data_url: str, folder: Path, prefix: str) -> str:
    """Картинка из браузера/кассы (base64) → файл. Только JPEG/PNG/WebP."""
    s = str(data_url or "")
    if "," in s[:100]:
        s = s.split(",", 1)[1]
    try:
        raw = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        raise Error("Не удалось прочитать фото")
    if not raw or len(raw) > MAX_IMAGE:
        raise Error("Фото слишком большое — до 3 МБ")
    if raw[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = "webp"
    else:
        raise Error("Нужна фотография (JPG или PNG)")
    name = f"{prefix}_{secrets.token_hex(8)}.{ext}"
    (folder / name).write_bytes(raw)
    return name


# ---------- как заказ видят клиент и касса ----------
def order_public(o) -> dict:
    b = db.branch(o["branch_id"])
    return {"code": o["code"], "status": o["status"], "status_text": STATUS_TEXT[o["status"]],
            "reason": o["reason"], "kind": o["kind"], "table_no": o["table_no"], "items": json.loads(o["items"]),
            "subtotal": o["subtotal"], "delivery_fee": o["delivery_fee"], "bonus_spent": o["bonus_spent"],
            "total": o["total"], "bonus_earned": o["bonus_earned"], "pay": o["pay"], "paid": bool(o["paid"]),
            "has_check": bool(o["check_file"]), "number": o["pos_number"], "created": o["created"],
            "address": o["address"], "want_time": o["want_time"],
            "cashback": int(max(0, o["total"] - o["delivery_fee"]) * o["cashback_pct"] / 100),
            "branch": {"id": b["id"], "name": b["name"], "phone": b["phone"], "address": b["address"],
                       "qr": f"/qr/{b['qr_file']}" if b["qr_file"] else None, "pay_note": b["pay_note"]}}


def order_pos(o) -> dict:
    d = order_public(o)
    d.update(id=o["id"], name=o["name"], phone=phone_view(o["phone"]), phone_raw=o["phone"],
             comment=o["comment"], lat=o["lat"], lon=o["lon"], need_call=bool(o["need_call"]),
             called=bool(o["called"]), pos_order_id=o["pos_order_id"],
             check_url=f"/chk/{o['check_file']}?s={sign(o['check_file'])}" if o["check_file"] else None)
    if o["client_id"]:
        c = db.one("SELECT no_show, (SELECT COUNT(*) FROM orders WHERE client_id=? AND status='done') n "
                   "FROM clients WHERE id=?", (o["client_id"], o["client_id"]))
        d.update(no_show=c["no_show"], orders_before=c["n"])
    return d


def branch_public(b) -> dict:
    return {"id": b["id"], "name": b["name"], "address": b["address"], "phone": b["phone"],
            "open": db.is_open(b), "open_from": b["open_from"], "open_to": b["open_to"],
            "paused": bool(b["paused"]), "delivery": bool(b["delivery"]), "delivery_fee": b["delivery_fee"],
            "free_from": b["free_from"], "min_order": b["min_order"], "has_qr": bool(b["qr_file"]),
            "pay_note": b["pay_note"]}


def rules() -> dict:
    s = db.settings()
    return {"cashback_pct": db.cashback_pct(), "base_pct": float(s["cashback_pct"] or 0),
            "max_spend_pct": float(s["max_spend_pct"] or 0), "welcome_bonus": int(float(s["welcome_bonus"] or 0)),
            "referral_bonus": int(float(s["referral_bonus"] or 0)), "happy_from": s["happy_from"],
            "happy_to": s["happy_to"], "happy_mult": float(s["happy_mult"] or 1)}


# ---------- обработчики ----------
API = {}


def route(path, who="public"):
    def deco(fn):
        API[path] = (fn, who)
        return fn
    return deco


@route("/api/public")
def api_public(req, body):
    return {"brand": db.setting("brand"), "branches": [branch_public(b) for b in db.branches()],
            "promos": [{"id": p["id"], "title": p["title"], "text": p["text"]} for p in db.promos()],
            "rules": rules()}


@route("/api/menu")
def api_menu(req, body):
    b = db.branch(int(body.get("branch_id") or 0))
    if not b or not b["active"]:
        raise Error("Филиал не найден", 404)
    return {"branch": branch_public(b),
            "products": [{"id": p["pos_id"], "name": p["name"], "category": p["category"], "price": p["price"],
                          "img": f"/img/{p['img_file']}" if p["img_file"] else None} for p in db.menu(b["id"])]}


@route("/api/me")
def api_me(req, body):
    client, tok = db.client_by_token(body.get("token"))
    if not client:
        return {"client": None}
    db.expire_bonus(client)
    hist = [{"amount": r["amount"], "kind": r["kind"], "note": r["note"], "created": r["created"]}
            for r in db.bonus_history(client["id"])]
    return {"client": {"name": client["name"], "phone": phone_view(client["phone"]),
                       "balance": db.balance(client["id"]), "trusted": bool(tok["trusted"]),
                       "ref_code": client["ref_code"]},
            "orders": [order_public(o) for o in db.client_orders(client["id"])], "bonus": hist}


@route("/api/order/new")
def api_order_new(req, body):
    limit(("order", req.ip), 6, 600)
    check = body.get("check")
    r = db.create_order(body, body.get("token"))
    if check:
        try:
            db.set_check(r["id"], save_image(check, CHECKS, "c"))
        except Error:
            pass  # заказ важнее — чек можно прикрепить потом
    return {"code": r["code"], "token": r["token"]}


@route("/api/order/get")
def api_order_get(req, body):
    o = db.order_by_code(body.get("code"))
    if not o:
        raise Error("Заказ не найден", 404)
    return {"order": order_public(o)}


@route("/api/order/check")
def api_order_check(req, body):
    limit(("check", req.ip), 10, 600)
    o = db.order_by_code(body.get("code"))
    if not o or o["status"] not in ("new", "accepted", "ready"):
        raise Error("Заказ не найден или уже закрыт", 404)
    db.set_check(o["id"], save_image(body.get("image"), CHECKS, "c"))
    return {"order": order_public(db.order(o["id"]))}


@route("/api/order/cancel")
def api_order_cancel(req, body):
    db.client_cancel(body.get("code"), body.get("token"))
    return {"order": order_public(db.order_by_code(body.get("code")))}


# --- касса ---
@route("/api/pos/menu", "pos")
def api_pos_menu(req, body, branch):
    need = db.sync_menu(branch["id"], body.get("products") or [])
    return {"need_images": need}


@route("/api/pos/image", "pos")
def api_pos_image(req, body, branch):
    name = save_image(body.get("data"), IMG, f"p{branch['id']}")
    old = db.one("SELECT img_file FROM products WHERE branch_id=? AND pos_id=?", (branch["id"], int(body["id"])))
    db.set_image(branch["id"], int(body["id"]), str(body.get("key") or "")[:200], name)
    if old and old["img_file"]:
        (IMG / old["img_file"]).unlink(missing_ok=True)
    return {}


@route("/api/pos/poll", "pos")
def api_pos_poll(req, body, branch):
    db.touch_pos(branch["id"])
    db.pos_statuses(branch["id"], body.get("statuses") or [])
    b = db.branch(branch["id"])
    return {"branch": {"name": b["name"], "paused": bool(b["paused"]), "open": db.is_open(b)},
            "orders": [order_pos(o) for o in db.branch_active(branch["id"])],
            "big_order": int(db.num("big_order"))}


@route("/api/pos/action", "pos")
def api_pos_action(req, body, branch):
    o = db.pos_action(branch["id"], int(body.get("id") or 0), str(body.get("action") or ""), body)
    return {"order": order_pos(o)}


@route("/api/pos/pause", "pos")
def api_pos_pause(req, body, branch):
    db.x("UPDATE branches SET paused=? WHERE id=?", (1 if body.get("paused") else 0, branch["id"]))
    return {"paused": bool(body.get("paused"))}


# --- владелец ---
@route("/api/admin/login")
def api_admin_login(req, body):
    limit(("login", req.ip), 10, 600)
    if not ADMIN_PASSWORD or not hmac.compare_digest(str(body.get("password") or ""), ADMIN_PASSWORD):
        raise Error("Неверный пароль", 403)
    req.set_cookie = db.new_admin_session()
    return {}


@route("/api/admin/logout", "admin")
def api_admin_logout(req, body):
    db.x("DELETE FROM admin_sessions WHERE token=?", (req.cookie("adm"),))
    req.set_cookie = ""
    return {}


def branch_admin(b) -> dict:
    seen = b["pos_seen"]
    online = bool(seen and (now() - dt.datetime.fromisoformat(seen)).total_seconds() < 120)
    d = dict(b)
    d.update(pos_online=online, qr=f"/qr/{b['qr_file']}" if b["qr_file"] else None,
             products=db.one("SELECT COUNT(*) c FROM products WHERE branch_id=? AND active=1", (b["id"],))["c"])
    return d


@route("/api/admin/state", "admin")
def api_admin_state(req, body):
    recent = db.q("SELECT * FROM orders ORDER BY id DESC LIMIT 40")
    return {"settings": db.settings(), "branches": [branch_admin(b) for b in db.branches(all_=True)],
            "promos": [dict(p) for p in db.promos(all_=True)],
            "stats": {"day": db.stats(1), "week": db.stats(7), "month": db.stats(30)},
            "orders": [dict(order_public(o), id=o["id"], name=o["name"], phone=phone_view(o["phone"]))
                       for o in recent]}


@route("/api/admin/branch", "admin")
def api_admin_branch(req, body):
    return {"id": db.save_branch(body)}


@route("/api/admin/branch/token", "admin")
def api_admin_branch_token(req, body):
    return {"token": db.new_pos_token(int(body["id"]))}


@route("/api/admin/branch/qr", "admin")
def api_admin_branch_qr(req, body):
    b = db.branch(int(body.get("id") or 0))
    if not b:
        raise Error("Филиал не найден", 404)
    name = save_image(body.get("image"), IMG, f"qr{b['id']}") if body.get("image") else None
    db.x("UPDATE branches SET qr_file=? WHERE id=?", (name, b["id"]))
    if b["qr_file"]:
        (IMG / b["qr_file"]).unlink(missing_ok=True)
    return {}


@route("/api/admin/settings", "admin")
def api_admin_settings(req, body):
    db.save_settings(body)
    return {}


@route("/api/admin/promo", "admin")
def api_admin_promo(req, body):
    db.save_promo(body)
    return {}


@route("/api/admin/clients", "admin")
def api_admin_clients(req, body):
    return {"clients": [{"id": c["id"], "name": c["name"], "phone": phone_view(c["phone"]), "balance": c["bal"],
                         "orders": c["n"], "no_show": c["no_show"], "ref_code": c["ref_code"],
                         "last": c["last_activity"]} for c in db.find_clients(body.get("q"))]}


@route("/api/admin/client/bonus", "admin")
def api_admin_client_bonus(req, body):
    amount = int(body.get("amount") or 0)
    if not amount:
        raise Error("Введите сумму")
    db.add_bonus(int(body["id"]), amount, "manual", note=str(body.get("note") or "от владельца"))
    return {}


@route("/api/admin/client/unblock", "admin")
def api_admin_client_unblock(req, body):
    db.x("UPDATE clients SET no_show=0 WHERE id=?", (int(body["id"]),))
    return {}


# ---------- HTTP ----------
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/admin": ("admin.html", "text/html; charset=utf-8"),
          "/manifest.json": ("manifest.json", "application/manifest+json"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}
TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
FILE_RE = re.compile(r"^[A-Za-z0-9_]+\.(jpg|png|webp)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "BilalOnline"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # без шума в логах Render
        pass

    @property
    def ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def cookie(self, name):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c[name].value if name in c else None

    def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None, cache: str = "no-store"):
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "") and \
                ctype.split(";")[0] in ("text/html", "application/json", "image/svg+xml"):
            body = gzip.compress(body, 6)
            extra = dict(extra or {}, **{"Content-Encoding": "gzip"})
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, data, status=200, extra=None):
        self.send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send(200, b"OK", "text/plain")
        if path in STATIC:
            f, ctype = STATIC[path]
            return self.send(200, (WEB / f).read_bytes(), ctype, cache="no-cache")
        m = re.match(r"^/(img|qr|chk)/(.+)$", path)
        if m and FILE_RE.match(m[2]):
            kind, name = m[1], m[2]
            if kind == "chk":
                s = parse_qs(urlparse(self.path).query).get("s", [""])[0]
                if not hmac.compare_digest(s, sign(name)):
                    return self.send(403, b"forbidden", "text/plain")
            p = (CHECKS if kind == "chk" else IMG) / name
            if p.is_file():
                return self.send(200, p.read_bytes(), TYPES[name.rsplit(".", 1)[1]],
                                 cache="private, max-age=86400" if kind == "chk" else "public, max-age=2592000, immutable")
        self.send(404, "Не найдено".encode(), "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        self.set_cookie = None
        try:
            if path not in API:
                raise Error("Не найдено", 404)
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise Error("Слишком большой запрос", 413)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise Error("Неверный запрос")
            if not isinstance(body, dict):
                raise Error("Неверный запрос")
            fn, who = API[path]
            if who == "pos":
                branch = db.branch_by_token(self.headers.get("X-Pos-Token") or "")
                if not branch:
                    raise Error("Неверный ключ кассы — проверьте online.json", 401)
                data = fn(self, body, branch)
            else:
                if who == "admin" and not db.admin_ok(self.cookie("adm")):
                    raise Error("Войдите заново", 401)
                data = fn(self, body)
            extra = None
            if self.set_cookie is not None:
                secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") == "https") else ""
                age = 30 * 86400 if self.set_cookie else 0
                extra = {"Set-Cookie": f"adm={self.set_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}"}
            self.json({"ok": True, **(data or {})}, extra=extra)
        except Error as e:
            self.json({"ok": False, "error": str(e)}, e.status)
        except (KeyError, ValueError, TypeError) as e:
            self.json({"ok": False, "error": "Неверные данные"}, 400)
            traceback.print_exception(e)
        except Exception as e:  # noqa: BLE001
            traceback.print_exception(e)
            self.json({"ok": False, "error": "Ошибка сервера, попробуйте ещё раз"}, 500)


def serve(port: int):
    make_db()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"Онлайн-заказы: http://0.0.0.0:{port}  (данные: {DATA})", flush=True)
    if not ADMIN_PASSWORD:
        print("⚠️  ADMIN_PASSWORD не задан — вход в /admin выключен", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve(int(os.getenv("PORT", "8000")))
