"""Связь кассы с сайтом онлайн-заказов.

Касса сама ходит на сайт (раз в несколько секунд): забирает новые заказы, сообщает, что стало
с принятыми, и присылает своё меню (названия, цены, фото, стоп-лист). Сайт к кассе не подключается,
поэтому ничего настраивать в роутере не нужно — нужен только интернет.

Настройка — файл online.json рядом с manage.py (адрес сайта и ключ кассы из админки сайта):
    {"url": "https://bilal-online.onrender.com", "token": "…"}
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import urllib.error
import urllib.request
from base64 import b64encode
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction

from .models import Employee, Order, OrderItem, Product

CONF_FILE = Path(settings.BASE_DIR) / "online.json"
KIND_TO_POS = {"delivery": "delivery", "pickup": "takeaway", "table": "here"}
KIND_TEXT = {"delivery": "Доставка", "pickup": "Самовывоз", "table": "Стол"}
MENU_EVERY = 600
# Служебные позиции кассы (по ним чек понимает тип заказа) — на сайте их не показываем
SERVICE = {"delivery": "доставка", "pickup": "с собой", "table": "здесь"}


def _is_service(p) -> bool:
    text = f"{p.name or ''} {p.category or ''}".lower()
    return any(w in text for w in SERVICE.values())  # меню целиком раз в 10 минут (и сразу, если что-то поменялось)


class OnlineError(Exception):
    pass


_conf = {"mtime": None, "data": None}
_state = {"orders": [], "branch": None, "at": 0, "menu_hash": None, "menu_at": 0, "error": None}
_lock = threading.Lock()
_menu_lock = threading.Lock()


def conf() -> dict | None:
    try:
        m = CONF_FILE.stat().st_mtime
    except OSError:
        return None
    if _conf["mtime"] != m:
        try:
            data = json.loads(CONF_FILE.read_text(encoding="utf-8-sig"))
            _conf.update(mtime=m, data=data if data.get("url") and data.get("token") else None)
        except (ValueError, OSError):
            _conf.update(mtime=m, data=None)
    return _conf["data"]


def call(path: str, body: dict | None = None, timeout: float = 8) -> dict:
    c = conf()
    if not c:
        raise OnlineError("Онлайн-заказы не настроены: нет файла online.json")
    req = urllib.request.Request(c["url"].rstrip("/") + path, json.dumps(body or {}).encode(),
                                 {"Content-Type": "application/json", "X-Pos-Token": c["token"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except ValueError:
            raise OnlineError(f"Сайт ответил ошибкой {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise OnlineError("Нет связи с сайтом — проверьте интернет")
    if not data.get("ok"):
        raise OnlineError(data.get("error") or "Ошибка сайта")
    return data


# ---------- меню ----------
def _image_key(p) -> str | None:
    try:
        if p.image and Path(p.image.path).is_file():
            st = Path(p.image.path).stat()
            return f"{p.image.name}:{st.st_size}:{int(st.st_mtime)}"
    except (ValueError, OSError):
        pass
    return None


def menu_payload() -> list[dict]:
    out = []
    for p in Product.objects.all().order_by("category", "id"):
        if not p.name or not p.price or _is_service(p):
            continue
        out.append({"id": p.id, "name": p.name, "category": p.category or "Меню", "price": float(p.price),
                    "active": bool(p.is_active), "image_key": _image_key(p), "sort": getattr(p, "order", 0) or 0})
    return out


def _photo(p) -> bytes | None:
    """Фото блюда для сайта: уменьшаем до 800 px (если есть Pillow), чтобы сайт быстро открывался."""
    try:
        raw = Path(p.image.path).read_bytes()
    except (ValueError, OSError):
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        im.thumbnail((800, 800))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — нет Pillow или странный файл: отправим как есть, если не огромный
        return raw if len(raw) < 2_500_000 else None


def sync_menu(force: bool = False) -> int:
    """Отправить меню, если поменялось (или прошло 10 минут). Возвращает, сколько фото загружено."""
    if not _menu_lock.acquire(blocking=force, timeout=60 if force else -1):
        return 0
    try:
        items = menu_payload()
        h = hashlib.md5(json.dumps(items, sort_keys=True).encode()).hexdigest()
        if not force and h == _state["menu_hash"] and time.time() - _state["menu_at"] < MENU_EVERY:
            return 0
        need = call("/api/pos/menu", {"products": items}, timeout=20)["need_images"]
        sent = 0
        for p in Product.objects.filter(id__in=need):
            data = _photo(p)
            if data:
                call("/api/pos/image", {"id": p.id, "key": _image_key(p), "data": b64encode(data).decode()}, timeout=30)
                sent += 1
        _state.update(menu_hash=h, menu_at=time.time())
        return sent
    finally:
        _menu_lock.release()


def _menu_bg():
    try:
        sync_menu()
    except Exception as e:  # noqa: BLE001
        _state["error"] = str(e)
    finally:
        connection.close()  # свой поток — своё подключение к базе кассы


# ---------- заказы ----------
def _pos_status(pos_id) -> str | None:
    o = Order.objects.filter(id=pos_id).only("status", "cancelled").first()
    if not o:
        return None  # чек не нашли (другая база?) — ничего не меняем на сайте
    return "cancelled" if o.cancelled or o.status == "cancelled" else o.status


def poll() -> dict:
    """Новые заказы с сайта + сообщить сайту, что стало с принятыми (вызван / закрыт / отменён)."""
    with _lock:
        statuses = [{"id": o["id"], "pos_status": _pos_status(o["pos_order_id"])}
                    for o in _state["orders"] if o["status"] in ("accepted", "ready") and o.get("pos_order_id")]
        try:
            d = call("/api/pos/poll", {"statuses": [s for s in statuses if s["pos_status"]]})
        except OnlineError as e:
            _state["error"] = str(e)
            raise
        _state.update(orders=d["orders"], branch=d["branch"], at=time.time(), error=None)
    if time.time() - _state["menu_at"] > 30:  # меню — в фоне, чтобы касса не ждала
        threading.Thread(target=_menu_bg, daemon=True).start()
    return d


def poll_cached(max_age: float = 2.5) -> dict:
    """Несколько вкладок кассы спрашивают одновременно — на сайт ходим не чаще раза в 2–3 секунды."""
    if time.time() - _state["at"] < max_age:
        return {"orders": _state["orders"], "branch": _state["branch"]}
    return poll()


def find(oid: int) -> dict:
    o = next((x for x in _state["orders"] if x["id"] == oid), None)
    if not o:
        poll()
        o = next((x for x in _state["orders"] if x["id"] == oid), None)
    if not o:
        raise OnlineError("Заказ не найден — обновите страницу")
    return o


def _note(o: dict) -> str:
    parts = ["🌐 " + KIND_TEXT[o["kind"]] + (f" {o['table_no']}" if o.get("table_no") else "")]
    if o.get("name") or o.get("phone"):
        parts.append(" ".join(x for x in (o.get("name"), o.get("phone")) if x))
    if o.get("address"):
        parts.append(o["address"])
    if o.get("want_time"):
        parts.append("к " + o["want_time"])
    if o.get("comment"):
        parts.append(o["comment"])
    if o.get("bonus_spent"):
        parts.append(f"бонусы −{o['bonus_spent']}")
    if o.get("delivery_fee"):
        parts.append(f"доставка +{o['delivery_fee']}")
    parts.append("ОПЛАЧЕН переводом" if o.get("paid") else ("перевод — проверить" if o["pay"] == "transfer" else "оплата при получении"))
    text = " · ".join(parts)
    return text if len(text) <= 200 else text[:199] + "…"


def _service_product(kind: str):
    word = SERVICE[kind]
    for p in Product.objects.filter(is_active=True):
        if word in f"{p.name or ''} {p.category or ''}".lower():
            return p
    return None


def accept(oid: int, employee_id=None) -> dict:
    """Принять: заказ появляется в кассе как обычный (кухня его видит), печатается чек, сайт узнаёт номер."""
    o = find(oid)
    if o["status"] != "new":
        raise OnlineError("Заказ уже обработан")
    if o.get("need_call") and not o.get("called"):
        raise OnlineError("Большой заказ — сначала позвоните клиенту и нажмите «Позвонил»")
    products = {p.id: p for p in Product.objects.filter(id__in=[i["id"] for i in o["items"]])}
    missing = [i["name"] for i in o["items"] if i["id"] not in products]
    if missing:
        raise OnlineError("Нет на кассе: " + ", ".join(missing))
    emp = Employee.objects.filter(id=employee_id).first() if employee_id else None
    with transaction.atomic():
        order = Order(employee=emp, total=Decimal(o["total"]), note=_note(o), status="pending",
                      order_type=KIND_TO_POS[o["kind"]], is_paid=bool(o.get("paid")))
        order.save()
        for i in o["items"]:
            OrderItem.objects.create(order=order, product=products[i["id"]], quantity=int(i["qty"]),
                                     price=Decimal(str(i["price"])), options=[])
        mark = _service_product(o["kind"])
        if mark:  # «Доставка» / «С собой» / «Здесь» — как у обычного заказа, чтобы на чеке был тип
            OrderItem.objects.create(order=order, product=mark, quantity=1, options=[],
                                     price=Decimal(o.get("delivery_fee") or 0) if o["kind"] == "delivery" else Decimal(0))
        r = call("/api/pos/action", {"id": oid, "action": "accept", "pos_order_id": order.id,
                                     "pos_number": order.receipt_number})  # ошибка — заказ в кассе не создастся
    try:
        from .views import print_receipt_direct  # печать чеков как у обычного заказа
        print_receipt_direct(order)
    except Exception as e:  # noqa: BLE001 — принтер не помешает принять заказ
        print("Онлайн-заказ: чек не напечатан:", e)
    _replace(r["order"])
    return r["order"]


def action(oid: int, name: str, **extra) -> dict:
    o = find(oid)
    r = call("/api/pos/action", {"id": oid, "action": name, **extra})
    if name == "paid" and o.get("pos_order_id"):
        Order.objects.filter(id=o["pos_order_id"]).update(is_paid=not extra.get("undo"))
    if name in ("reject", "no_show") and o.get("pos_order_id"):  # уже был в кассе — отменяем и там
        Order.objects.filter(id=o["pos_order_id"]).update(cancelled=True, status="cancelled")
    _replace(r["order"])
    return r["order"]


def pause(paused: bool) -> dict:
    return call("/api/pos/pause", {"paused": bool(paused)})


def _replace(order: dict):
    with _lock:
        _state["orders"] = [order if x["id"] == order["id"] else x for x in _state["orders"]]


def base_url() -> str:
    c = conf()
    return c["url"].rstrip("/") if c else ""
