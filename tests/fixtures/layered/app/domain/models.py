from app.web.routes import render  # 意図的な逆向き依存（ドメイン層がプレゼンテーション層に依存）


class Order:
    def __init__(self, item):
        self.item = item

    def describe(self):
        return render(self)
