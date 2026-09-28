"""``python -m app.export_openapi`` -> api/openapi.json (the frontend type source)."""

from __future__ import annotations

import json
from pathlib import Path

from .main import app


def main() -> Path:
    out = Path(__file__).resolve().parents[1] / "openapi.json"
    out.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=1),
        encoding="utf-8",
        newline="\n",
    )
    print("wrote", out)
    return out


if __name__ == "__main__":
    main()
