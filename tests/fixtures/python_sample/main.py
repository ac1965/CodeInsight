from shapes import Circle


def describe(circle: Circle) -> str:
    return f"area={circle.area()}"


def main() -> None:
    circle = Circle(radius=2.0)
    print(describe(circle))


if __name__ == "__main__":
    main()
