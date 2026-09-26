"""``python -m app`` -> uvicorn on config.HOST:config.PORT."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from . import config, logging_setup  # noqa: E402
from .main import app  # noqa: E402

if __name__ == "__main__":
    # Before uvicorn starts: our records must survive the process they describe.
    logging_setup.setup()
    print(f"crypto-hunter api -> http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")
