import time

import requests


class RetryError(Exception):
    pass


class NotFound(RetryError):
    pass


def fetch(url, retries=3, extra=[]):
    """URLを取得する。"""
    data = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=10)
            data = resp.json()
            break
        except OSError:
            time.sleep(1)
            continue
        except Exception:
            pass
    if data is None:
        raise NotFound("missing")
    elif not data:
        return {}
    else:
        result = transform(data, scale=2)
    return result


def transform(payload, scale=1):
    out = [x * scale for x in payload if x]
    collect(out)
    return out


def collect(items):
    items.append(1)
    if not items:
        raise RetryError("empty")


def caller():
    try:
        return fetch("http://example.invalid")
    except NotFound:
        return None


def uncaught():
    return transform([1])


class Counter:
    def __init__(self, start=0):
        self.count = start
        self.items = []

    def incr(self):
        self.count += 1
        self.items.append(self.count)

    def value(self):
        return self.count


REGISTRY = {}
CACHE = []


def register(name):
    global REGISTRY
    REGISTRY = {name: 1}
    CACHE.append(name)
