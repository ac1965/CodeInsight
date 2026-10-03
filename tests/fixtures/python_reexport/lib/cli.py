import extlib

from lib._shared import *

app = extlib.Typer()


@app.command()
def main():
    shared_helper()
    undefined_name()
    names = []
    names.append("a")
    return ", ".join(names)
