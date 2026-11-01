"""Production entry point: real controller thread + API on :8000 (Caddy terminates TLS in front)."""
import os
import sys
import threading

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.app import create_app                          # noqa: E402
from api.scenarios import Scenarios                     # noqa: E402
from controller.main import build_real                  # noqa: E402
from controller.probe import RttProbe                   # noqa: E402

NSP = os.environ.get("NSP", "qs")
UP = int(os.environ.get("UP_MBIT", 20))
DOWN = int(os.environ.get("DOWN_MBIT", 100))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ctrl = build_real(ns=f"{NSP}-rtr", up_mbit=UP, down_mbit=DOWN, model_path=os.path.join(ROOT, "models", "model.txt"),
                  classifier_mode="model")
# The "call" the dashboard plots: 50 pps echo from host h1 to an unusual port.
call = RttProbe(ns=f"{NSP}-h1", port=51234, hz=50.0)
call.start()
threading.Thread(target=ctrl.run_forever, daemon=True).start()
app = create_app(ctrl, Scenarios(NSP, DOWN, UP), call)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), log_level="warning")
