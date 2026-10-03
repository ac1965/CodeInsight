class Base:
    def greet(self):
        return "base"

    def hello(self):
        return self.greet()


class Derived(Base):
    def greet(self):
        return super().greet() + "!"
