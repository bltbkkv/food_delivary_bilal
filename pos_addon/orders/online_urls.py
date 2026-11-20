from django.urls import path

from . import online_views

urlpatterns = [
    path("", online_views.online_page, name="online_page"),
    path("poll/", online_views.online_poll, name="online_poll"),
    path("<int:order_id>/action/", online_views.online_action, name="online_action"),
    path("pause/", online_views.online_pause, name="online_pause"),
    path("menu-sync/", online_views.online_menu_sync, name="online_menu_sync"),
]
