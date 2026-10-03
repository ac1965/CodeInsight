from app.service.orders import place_order


def handle(request):
    return place_order(request["item"])


def render(order):
    return str(order)
