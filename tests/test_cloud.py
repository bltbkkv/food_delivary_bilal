"""Сценарии онлайн-заказов через настоящий HTTP: клиент → касса → бонусы → владелец."""
import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_PASSWORD"] = "secret-pass"

from app import db as dbm  # noqa: E402
from app import main  # noqa: E402

JPG = b"\xff\xd8\xff\xe0" + b"0" * 100
NOON = dbm.dt.datetime(2026, 11, 20, 12, 0)


class Server:
    def __init__(self):
        main.ADMIN_PASSWORD = "secret-pass"
        main._hits.clear()
        main.make_db(":memory:")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, path, body=None, headers=None, status=200):
        req = urllib.request.Request(self.url + path, json.dumps(body or {}).encode(),
                                     {"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                code, data, hdrs = r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            code, data, hdrs = e.code, json.loads(e.read()), e.headers
        assert code == status, (path, code, data)
        self.last_headers = hdrs
        return data

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url + path) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""


class Base(unittest.TestCase):
    def setUp(self):
        self.now = mock.patch.object(dbm, "now", lambda: NOON)
        self.now.start()
        self.s = Server()
        self.db = main.db
        self.bid = self.db.save_branch({"name": "Тунгуч", "phone": "0555 000 111", "delivery_fee": 150,
                                        "free_from": 2000, "min_order": 500})
        self.pos = {"X-Pos-Token": self.db.branch(self.bid)["pos_token"]}
        self.s.call("/api/pos/menu", {"products": [
            {"id": 1, "name": "Шаурма M", "category": "Шаурма", "price": 250, "image_key": "a1"},
            {"id": 2, "name": "Крылышки 6 шт", "category": "Курица", "price": 400},
            {"id": 3, "name": "Кола 0.5", "category": "Напитки", "price": 80, "active": False}]}, self.pos)

    def tearDown(self):
        self.s.srv.shutdown()
        self.s.srv.server_close()
        main.db.db.close()
        self.now.stop()

    def order(self, **kw):
        body = {"branch_id": self.bid, "kind": "pickup", "name": "Айбек", "phone": "0700 123 456",
                "items": [{"id": 1, "qty": 2}, {"id": 2, "qty": 1}], "pay": "cash"}
        body.update(kw)
        return self.s.call("/api/order/new", body)

    def poll(self, statuses=None):
        return self.s.call("/api/pos/poll", {"statuses": statuses or []}, self.pos)

    def act(self, oid, action, status=200, **kw):
        return self.s.call("/api/pos/action", {"id": oid, "action": action, **kw}, self.pos, status=status)


class Flow(Base):
    def test_menu_from_pos_and_images(self):
        r = self.s.call("/api/pos/menu", {"products": [
            {"id": 1, "name": "Шаурма M", "category": "Шаурма", "price": 260, "image_key": "a1"},
            {"id": 2, "name": "Крылышки 6 шт", "category": "Курица", "price": 400}]}, self.pos)
        self.assertEqual(r["need_images"], [1], "фото ещё не загружено")
        self.s.call("/api/pos/image", {"id": 1, "key": "a1", "data": base64.b64encode(JPG).decode()}, self.pos)
        r = self.s.call("/api/pos/menu", {"products": [{"id": 1, "name": "Шаурма M", "price": 260, "image_key": "a1"}]},
                        self.pos)
        self.assertEqual(r["need_images"], [])
        menu = self.s.call("/api/menu", {"branch_id": self.bid})["products"]
        self.assertEqual([(p["name"], p["price"]) for p in menu], [("Шаурма M", 260)], "удалённое — скрыто")
        self.assertEqual(self.s.get(menu[0]["img"]), (200, JPG))
        self.s.call("/api/pos/menu", {"products": []}, {"X-Pos-Token": "wrong"}, status=401)

    def test_order_accept_done_cashback_and_trust(self):
        r = self.order()
        self.assertTrue(r["token"])
        o = self.poll()["orders"][0]
        self.assertEqual((o["status"], o["total"], o["phone"], o["kind"]), ("new", 900, "0700 123 456", "pickup"))
        self.act(o["id"], "accept", pos_order_id=77, pos_number=12)
        got = self.s.call("/api/order/get", {"code": r["code"]})["order"]
        self.assertEqual((got["status_text"], got["number"]), ("Готовится", 12))
        self.poll([{"id": o["id"], "pos_status": "called"}])
        self.assertEqual(self.s.call("/api/order/get", {"code": r["code"]})["order"]["status"], "ready")
        self.poll([{"id": o["id"], "pos_status": "ready"}])  # на кассе закрыли чек
        me = self.s.call("/api/me", {"token": r["token"]})
        self.assertEqual(me["orders"][0]["status"], "done")
        self.assertEqual(me["client"]["balance"], 45 + 100, "5% кэшбэк + бонус за первый заказ")
        self.assertTrue(me["client"]["trusted"])

    def test_spend_bonus_only_from_trusted_device(self):
        r = self.order()
        oid = self.poll()["orders"][0]["id"]
        self.act(oid, "accept")
        self.act(oid, "done")
        # чужое устройство с тем же номером — бонусы не тратятся
        r2 = self.order(use_bonus=True)
        self.assertNotEqual(r2["token"], r["token"])
        o2 = self.s.call("/api/order/get", {"code": r2["code"]})["order"]
        self.assertEqual(o2["bonus_spent"], 0)
        # своё — тратятся, не больше 30% заказа
        r3 = self.order(token=r["token"], use_bonus=True, items=[{"id": 1, "qty": 1}])
        o3 = self.s.call("/api/order/get", {"code": r3["code"]})["order"]
        self.assertEqual((o3["bonus_spent"], o3["total"]), (75, 175))
        # отклонили — бонусы вернулись
        oid3 = [o for o in self.poll()["orders"] if o["code"] == r3["code"]][0]["id"]
        self.act(oid3, "reject", reason="закончилась курица")
        me = self.s.call("/api/me", {"token": r["token"]})
        self.assertEqual(me["client"]["balance"], 145)
        self.assertEqual([o for o in me["orders"] if o["code"] == r3["code"]][0]["reason"], "закончилась курица")

    def test_delivery_fee_min_order_and_free_delivery(self):
        e = self.s.call("/api/order/new", {"branch_id": self.bid, "kind": "delivery", "name": "А", "phone": "0700123456",
                                           "address": "мкр Тунгуч 12", "items": [{"id": 1, "qty": 1}]}, status=400)
        self.assertIn("Минимальный заказ", e["error"])
        r = self.order(kind="delivery", address="мкр Тунгуч 12, кв 5")
        o = self.s.call("/api/order/get", {"code": r["code"]})["order"]
        self.assertEqual((o["delivery_fee"], o["total"]), (150, 1050))
        r = self.order(kind="delivery", address="мкр Тунгуч 12", items=[{"id": 2, "qty": 5}])
        self.assertEqual(self.s.call("/api/order/get", {"code": r["code"]})["order"]["delivery_fee"], 0)

    def test_prices_come_from_menu_not_browser(self):
        r = self.order(items=[{"id": 1, "qty": 1, "price": 1}])
        self.assertEqual(self.s.call("/api/order/get", {"code": r["code"]})["order"]["total"], 250)
        e = self.s.call("/api/order/new", {"branch_id": self.bid, "kind": "pickup", "name": "А",
                                           "phone": "0700123456", "items": [{"id": 3, "qty": 1}]}, status=400)
        self.assertIn("нет в меню", e["error"])

    def test_big_order_needs_call(self):
        r = self.order(items=[{"id": 2, "qty": 8}])
        o = [x for x in self.poll()["orders"] if x["code"] == r["code"]][0]
        self.assertTrue(o["need_call"])
        self.assertIn("позвоните", self.act(o["id"], "accept", status=400)["error"])
        self.act(o["id"], "called")
        self.act(o["id"], "accept")

    def test_transfer_with_check(self):
        r = self.order(pay="transfer", check="data:image/jpeg;base64," + base64.b64encode(JPG).decode())
        o = self.poll()["orders"][0]
        self.assertTrue(o["has_check"])
        self.assertEqual(self.s.get(o["check_url"]), (200, JPG))
        self.assertEqual(self.s.get(o["check_url"].split("?")[0])[0], 403, "без подписи чек не открыть")
        self.act(o["id"], "paid")
        self.assertTrue(self.s.call("/api/order/get", {"code": r["code"]})["order"]["paid"])

    def test_referral_and_no_show(self):
        a = self.order(phone="0555 111 222")
        oid = self.poll()["orders"][0]["id"]
        self.act(oid, "accept")
        self.act(oid, "done")
        ref = self.s.call("/api/me", {"token": a["token"]})["client"]["ref_code"]
        b = self.order(phone="0777 999 888", name="Друг", ref=ref)
        ob = [x for x in self.poll()["orders"] if x["code"] == b["code"]][0]
        self.act(ob["id"], "accept")
        self.act(ob["id"], "done")
        self.assertEqual(self.s.call("/api/me", {"token": a["token"]})["client"]["balance"], 45 + 100 + 150)
        # не забрал 2 раза — дальше только перевод
        for _ in range(2):
            c = self.order(phone="0999 000 000")
            oc = [x for x in self.poll()["orders"] if x["code"] == c["code"]][0]
            self.act(oc["id"], "no_show")
        e = self.s.call("/api/order/new", {"branch_id": self.bid, "kind": "pickup", "name": "А",
                                           "phone": "0999000000", "items": [{"id": 1, "qty": 1}]}, status=400)
        self.assertIn("переводом", e["error"])
        self.order(phone="0999 000 000", pay="transfer")

    def test_table_order_without_phone(self):
        r = self.s.call("/api/order/new", {"branch_id": self.bid, "kind": "table", "table_no": "5",
                                           "items": [{"id": 1, "qty": 1}]})
        self.assertIsNone(r["token"])
        o = self.poll()["orders"][0]
        self.assertEqual((o["kind"], o["table_no"], o["pay"]), ("table", "5", "cash"))

    def test_closed_and_paused(self):
        self.db.save_branch({"id": self.bid, "open_from": "09:00", "open_to": "11:00"})
        self.assertIn("не принимает", self.s.call("/api/order/new", {"branch_id": self.bid}, status=400)["error"])
        self.db.save_branch({"id": self.bid, "open_from": "09:00", "open_to": "02:00"})
        self.s.call("/api/pos/pause", {"paused": True}, self.pos)
        self.assertFalse(self.s.call("/api/public")["branches"][0]["open"])
        self.s.call("/api/pos/pause", {"paused": False}, self.pos)
        self.assertTrue(self.s.call("/api/public")["branches"][0]["open"])

    def test_client_cancel_and_happy_hours(self):
        self.db.save_settings({"happy_from": "11:00", "happy_to": "15:00", "happy_mult": "2"})
        self.assertEqual(self.s.call("/api/public")["rules"]["cashback_pct"], 10)
        r = self.order()
        self.s.call("/api/order/cancel", {"code": r["code"], "token": "x"}, status=404)
        self.s.call("/api/order/cancel", {"code": r["code"], "token": r["token"]})
        self.assertEqual(self.poll()["orders"][0]["status"], "cancelled")

    def test_bonus_expire(self):
        r = self.order()
        oid = self.poll()["orders"][0]["id"]
        self.act(oid, "accept")
        self.act(oid, "done")
        with mock.patch.object(dbm, "now", lambda: NOON + dbm.dt.timedelta(days=91)):
            self.assertEqual(self.s.call("/api/me", {"token": r["token"]})["client"]["balance"], 0)


class Admin(Base):
    def login(self):
        self.s.call("/api/admin/login", {"password": "secret-pass"})
        return {"Cookie": self.s.last_headers["Set-Cookie"].split(";")[0]}

    def test_admin(self):
        self.s.call("/api/admin/state", status=401)
        self.s.call("/api/admin/login", {"password": "no"}, status=403)
        h = self.login()
        st = self.s.call("/api/admin/state", {}, h)
        self.assertEqual(st["branches"][0]["products"], 2)
        self.assertTrue(st["branches"][0]["pos_online"] is False)
        self.s.call("/api/admin/settings", {"cashback_pct": "7", "welcome_bonus": "200"}, h)
        self.assertEqual(self.s.call("/api/public")["rules"]["welcome_bonus"], 200)
        self.s.call("/api/admin/promo", {"title": "Комбо дня", "text": "Шаурма + кола 299"}, h)
        self.assertEqual(self.s.call("/api/public")["promos"][0]["title"], "Комбо дня")
        self.s.call("/api/admin/branch/qr", {"id": self.bid, "image": base64.b64encode(JPG).decode()}, h)
        b = self.s.call("/api/public")["branches"][0]
        self.assertTrue(b["has_qr"])
        r = self.order()
        cl = self.s.call("/api/admin/clients", {"q": "0700"}, h)["clients"]
        self.assertEqual(cl[0]["phone"], "0700 123 456")
        self.s.call("/api/admin/client/bonus", {"id": cl[0]["id"], "amount": 500, "note": "компенсация"}, h)
        self.assertEqual(self.s.call("/api/me", {"token": r["token"]})["client"]["balance"], 500)
        old = self.pos["X-Pos-Token"]
        self.s.call("/api/admin/branch/token", {"id": self.bid}, h)
        self.s.call("/api/pos/poll", {}, {"X-Pos-Token": old}, status=401)


class Units(unittest.TestCase):
    def test_phone_and_window(self):
        self.assertEqual(dbm.phone_norm("+996 (555) 12-34-56"), "996555123456")
        self.assertEqual(dbm.phone_norm("0555123456"), "996555123456")
        with self.assertRaises(dbm.Error):
            dbm.phone_norm("12345")
        t = dbm.dt.datetime(2026, 1, 1, 1, 30)
        self.assertTrue(dbm.in_window(t, "09:00", "02:00"))
        self.assertFalse(dbm.in_window(t.replace(hour=3), "09:00", "02:00"))
        self.assertTrue(dbm.in_window(t, "00:00", "00:00"))


if __name__ == "__main__":
    unittest.main()
