import json
import sqlite3


def save(order):
    connection = sqlite3.connect("orders.db")
    connection.execute("INSERT INTO orders VALUES (?)", (order.item,))
    with open("orders.log", "w") as handle:
        json.dump({"item": order.item}, handle)
