import json
from urllib.parse import quote
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from .models import Product, Category


# ---------- Вспомогательные функции для работы с корзиной в сессии ----------
def get_cart(request):
    """Возвращает словарь корзины из сессии"""
    return request.session.get('cart', {})

def save_cart(request, cart):
    """Сохраняет корзину в сессию"""
    request.session['cart'] = cart

# ---------- Страницы (HTML) ----------
def catalog(request):
    """Главная страница с каталогом товаров (группировка по категориям)"""
    categories = Category.objects.all()
    products = Product.objects.filter(is_active=True).select_related('category')
    cart_count = sum(get_cart(request).values())
    context = {
        'categories': categories,
        'products': products,
        'cart_count': cart_count,
    }
    return render(request, 'orders/catalog.html', context)

def cart_page(request):
    """Страница корзины (HTML) – данные подгружаются через JS"""
    return render(request, 'orders/cart_page.html')

# ---------- API для работы с корзиной (JSON) ----------
def cart_json(request):
    """Возвращает текущее содержимое корзины в формате JSON"""
    cart = get_cart(request)
    items = []
    total = 0
    for product_id, qty in cart.items():
        product = get_object_or_404(Product, id=int(product_id))
        subtotal = product.price * qty
        total += subtotal
        items.append({
            'id': product.id,
            'name': product.name,
            'price': float(product.price),
            'quantity': qty,
            'subtotal': float(subtotal),
            'image_url': product.image.url if product.image else '',
        })
    return JsonResponse({'items': items, 'total': float(total)})

@require_http_methods(['POST'])
@csrf_exempt  # упрощённо, для разработки; в проде используйте csrf_token в fetch
def api_add_to_cart(request):
    """Добавляет товар в корзину (+1 или указанное количество)"""
    data = json.loads(request.body)
    product_id = str(data.get('product_id'))
    quantity = int(data.get('quantity', 1))
    cart = get_cart(request)
    cart[product_id] = cart.get(product_id, 0) + quantity
    save_cart(request, cart)
    new_cart_count = sum(cart.values())
    return JsonResponse({'status': 'ok', 'cart_count': new_cart_count})

@require_http_methods(['POST'])
@csrf_exempt
def api_update_cart(request):
    """Обновляет количество конкретного товара в корзине (если 0 – удаляет)"""
    data = json.loads(request.body)
    product_id = str(data.get('product_id'))
    quantity = int(data.get('quantity'))
    cart = get_cart(request)
    if quantity <= 0:
        cart.pop(product_id, None)
    else:
        cart[product_id] = quantity
    save_cart(request, cart)
    # Возвращаем обновлённую корзину
    items = []
    total = 0
    for pid, qty in cart.items():
        product = get_object_or_404(Product, id=int(pid))
        subtotal = product.price * qty
        total += subtotal
        items.append({
            'id': product.id,
            'name': product.name,
            'price': float(product.price),
            'quantity': qty,
            'subtotal': float(subtotal),
            'image_url': product.image.url if product.image else '',
        })
    return JsonResponse({'items': items, 'total': float(total)})

@require_http_methods(['POST'])
@csrf_exempt
def api_remove_item(request):
    """Удаляет товар из корзины полностью"""
    data = json.loads(request.body)
    product_id = str(data.get('product_id'))
    cart = get_cart(request)
    cart.pop(product_id, None)
    save_cart(request, cart)
    items = []
    total = 0
    for pid, qty in cart.items():
        product = get_object_or_404(Product, id=int(pid))
        subtotal = product.price * qty
        total += subtotal
        items.append({
            'id': product.id,
            'name': product.name,
            'price': float(product.price),
            'quantity': qty,
            'subtotal': float(subtotal),
            'image_url': product.image.url if product.image else '',
        })
    return JsonResponse({'items': items, 'total': float(total)})

@require_http_methods(['POST'])
@csrf_exempt
def api_clear_cart(request):
    """Полная очистка корзины"""
    request.session['cart'] = {}
    return JsonResponse({'status': 'ok'})

# ---------- Необязательно: отправка заказа через GET-параметры (альтернатива JS) ----------
def whatsapp_checkout(request):
    """Принимает POST с формой и перенаправляет на WhatsApp с готовым текстом"""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        phone = request.POST.get('phone', '').strip()
        address = request.POST.get('address', '').strip()
        if not (name and phone and address):
            # можно вернуть ошибку, но для простоты редирект на корзину с сообщением
            return redirect('cart_page')
        cart = get_cart(request)
        if not cart:
            return redirect('cart_page')
        items_text = ""
        total = 0
        for pid, qty in cart.items():
            product = get_object_or_404(Product, id=int(pid))
            subtotal = product.price * qty
            total += subtotal
            items_text += f"• {product.name} × {qty} = {subtotal} сом\n"
        message = f"🛍️ *Новый заказ*\n\n👤 Имя: {name}\n📞 Телефон: {phone}\n🏠 Адрес: {address}\n\n📋 Состав:\n{items_text}\n💰 Итого: {total} сом"
        from urllib.parse import quote
        encoded = quote(message)
        whatsapp_url = f"https://wa.me/{settings.SELLER_WHATSAPP_NUMBER}?text={encoded}"
        # Очищаем корзину после оформления
        request.session['cart'] = {}
        return redirect(whatsapp_url)
    return redirect('cart_page')