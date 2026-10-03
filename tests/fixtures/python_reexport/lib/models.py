class Widget:
    def run(self):
        return "run"


def make_widget():
    return Widget()


class Holder:
    items = []

    def __init__(self, widget: Widget, label: str):
        self.widget = widget
        self.label = label
        self.cache = {}
        self.counter = 0
        self.counter += 1
        self.other = Widget()

    def go(self):
        self.widget.run()
        self.label.upper()
        self.cache.get("a")
        self.counter.bit_length()
        self.other.run()
        self.items.append(1)
