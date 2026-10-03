import importlib
import os

from . import plugin
from . import util
from .models import Base, Derived


def logged(fn):
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


@logged
def run(name):
    obj = Derived()
    obj.hello()
    util.helper(name)
    handler = getattr(obj, "greet")
    handler()
    getattr(obj, "greet")()
    importlib.import_module(name)
    os.getcwd()
    return plugin.start()


async def fetch():
    return await _inner()


async def _inner():
    return util.helper("x")
