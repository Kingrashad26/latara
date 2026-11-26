"""Usage:
    python -m latara trade       # run one trading day
    python -m latara flatten     # backup: close everything near the bell
    python -m latara scorecard   # last month's results
"""
import logging
import os
import sys
from pathlib import Path

from . import config


def _load_dotenv():
    p = Path(".env")
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_dotenv()
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("logs/latara.log")])
    cmd = sys.argv[1] if len(sys.argv) > 1 else "trade"
    cfg = config.load()
    if cmd == "trade":
        from .trader import Latara
        Latara(cfg).run()
    elif cmd == "flatten":
        from .tools import flatten
        flatten(cfg)
    elif cmd == "scorecard":
        from .tools import scorecard
        scorecard(cfg)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
