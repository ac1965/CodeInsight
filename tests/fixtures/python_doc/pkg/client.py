import os
from typing import Optional

from pkg import config
from pkg.config import DEFAULT_TIMEOUT, Settings


def fetch(url: str, settings: Optional[Settings] = None, retries: "int" = config.RETRIES) -> "Settings | None":
    """URLを取得する。"""
    limit = DEFAULT_TIMEOUT
    handler = os.getcwd
    callbacks = [fetch, handler]
    return settings if limit else None


def no_doc(value: config.Settings):
    return value


def long_signature(
    first: config.Settings,
    second: int = 0,
    *,
    flag: bool = False,
) -> int:
    return second
