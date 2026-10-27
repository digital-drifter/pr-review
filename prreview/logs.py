from datetime import datetime


def log(message: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)
