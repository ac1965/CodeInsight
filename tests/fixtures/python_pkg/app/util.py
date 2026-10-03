def helper(value):
    return str(value)


def describe(shape: "Derived") -> str:
    return shape.greet()


def make() -> str:
    obj = Derived()
    return obj.greet()


def untyped(thing, other: "Missing"):
    thing.greet()
    return other.greet()


from .models import Derived  # noqa: E402
