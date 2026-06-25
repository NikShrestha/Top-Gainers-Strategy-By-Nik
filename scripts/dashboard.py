"""
Launch the web dashboard.

    python -m scripts.dashboard
then open http://localhost:8000

Set HOST=0.0.0.0 (default) so it's reachable from other devices / once deployed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print(f"Dashboard on http://localhost:{port}  (Ctrl+C to stop)")
    uvicorn.run("src.webapp:app", host=host, port=port, log_level="warning")
