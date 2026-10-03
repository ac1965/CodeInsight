import pickle
import subprocess
import time


def swallow():
    try:
        return 1
    except:  # noqa: E722
        pass


def run(cmd, items=[]):
    subprocess.run(cmd, shell=True)
    return eval("1 + 1")


async def wait():
    time.sleep(1)


def load(blob):
    # TODO: 検証を追加する
    if not blob:
        raise Exception("empty")
    return pickle.loads(blob)
