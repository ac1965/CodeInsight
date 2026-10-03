import smtplib

import requests

from app.domain.models import Order


def notify(order: Order):
    smtplib.SMTP("mail.invalid").sendmail("a", "b", order.item)
    requests.post("http://hooks.invalid", json={"item": order.item})
