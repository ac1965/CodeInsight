from app.domain.models import Order
from app.infra.mailer import notify
from app.repository.store import save
from app.util.helpers import slug


def place_order(item):
    order = Order(slug(item))
    save(order)
    notify(order)
    return order
