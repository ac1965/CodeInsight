import argparse
import asyncio
import atexit
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

TIMEOUT = 30
RETRIES = 3
NAME = "demo"
lowercase_value = 1


@lru_cache(maxsize=None)
def cached_lookup(key):
    return key


def cmd_run(args):
    timeout = int(os.getenv("APP_TIMEOUT", "10"))
    token = os.environ["APP_TOKEN"]
    print(args.verbose, args.name, args.retries, timeout, token)


def cmd_serve(args):
    os.environ.setdefault("APP_MODE", "serve")
    with open("settings.json") as handle:
        settings = json.load(handle)
    return TIMEOUT + RETRIES, settings


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true", help="詳細表示")
    sub = parser.add_subparsers()
    run = sub.add_parser("run", help="実行する")
    run.add_argument("name", help="名前")
    run.add_argument("--retries", type=int, default=RETRIES, help="再試行回数")
    run.set_defaults(func=cmd_run)
    serve = sub.add_parser("serve")
    serve.set_defaults(func=cmd_serve)
    return parser


def main():
    args = build_parser().parse_args()
    return args.func(args)


class Worker:
    def start(self):
        thread = threading.Thread(target=self._loop)
        thread.start()
        pool = ThreadPoolExecutor()
        pool.submit(self._loop)

    def _loop(self):
        return None

    def hook(self, signal):
        signal.connect(self._on_done)
        signal.connect(lambda: None)

    def _on_done(self):
        return None


async def fetch():
    return 1


async def runner():
    await asyncio.gather(fetch())
    asyncio.create_task(fetch())


atexit.register(cmd_serve)

if __name__ == "__main__":
    main()
