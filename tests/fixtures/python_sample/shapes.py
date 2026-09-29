from functools import lru_cache

DEFAULT_COLOR = "black"


class Shape:
    kind = "generic"

    def __init__(self, color: str = DEFAULT_COLOR) -> None:
        self.color = color

    def area(self) -> float:
        raise NotImplementedError


class Circle(Shape):
    kind = "circle"

    def __init__(self, radius: float, color: str = DEFAULT_COLOR) -> None:
        super().__init__(color)
        self.radius = radius

    @lru_cache(maxsize=None)
    def area(self) -> float:
        return 3.14159 * self.radius * self.radius


async def fetch_area(shape: Shape) -> float:
    return shape.area()
