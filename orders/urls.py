from django.urls import path
from . import views

urlpatterns = [
    path('', views.catalog, name='catalog'),
    path('cart-page/', views.cart_page, name='cart_page'),
    path('cart/', views.cart_json, name='cart_json'),
    path('api/add-to-cart/', views.api_add_to_cart, name='api_add_to_cart'),
    path('api/update-cart/', views.api_update_cart, name='api_update_cart'),
    path('api/remove-from-cart/', views.api_remove_item, name='api_remove_item'),
    path('api/clear-cart/', views.api_clear_cart, name='api_clear_cart'),
    path('checkout-wa/', views.whatsapp_checkout, name='whatsapp_checkout'),  # опционально
]