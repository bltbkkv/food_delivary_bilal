"""Вкладка «Онлайн-заказы» на кассе и значок с уведомлением на остальных экранах."""
import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import online


def online_page(request):
    return render(request, "online.html", {"configured": bool(online.conf()), "site": online.base_url()})


def online_poll(request):
    if not online.conf():
        return JsonResponse({"ok": False, "configured": False, "error": "Нет файла online.json", "orders": []})
    try:
        d = online.poll_cached()
    except online.OnlineError as e:
        return JsonResponse({"ok": False, "configured": True, "error": str(e), "orders": online._state["orders"],
                             "branch": online._state["branch"]})
    return JsonResponse({"ok": True, "configured": True, "orders": d["orders"], "branch": d["branch"],
                         "site": online.base_url()})


@csrf_exempt
@require_POST
def online_action(request, order_id):
    try:
        body = json.loads(request.body or b"{}")
        name = body.get("action")
        if name == "accept":
            o = online.accept(order_id, request.session.get("employee_id"))
        elif name in ("called", "paid", "ready", "done", "reject", "no_show"):
            extra = {k: body[k] for k in ("reason", "undo") if k in body}
            o = online.action(order_id, name, **extra)
        else:
            return JsonResponse({"ok": False, "error": "Неизвестное действие"}, status=400)
    except online.OnlineError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "order": o})


@csrf_exempt
@require_POST
def online_pause(request):
    try:
        body = json.loads(request.body or b"{}")
        return JsonResponse({"ok": True, **online.pause(body.get("paused"))})
    except online.OnlineError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)


@csrf_exempt
@require_POST
def online_menu_sync(request):
    try:
        n = online.sync_menu(force=True)
        return JsonResponse({"ok": True, "images": n})
    except online.OnlineError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
