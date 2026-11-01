"""FastAPI service: dashboard + REST + WebSocket in front of the controller.

create_app() takes injected `controller`, `scenarios` and `call_monitor` so tests run without a
kernel. In production, api/serve.py wires the real ones.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from controller.common import CLASSES, FlowKey

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(os.path.dirname(HERE), "experiments", "report.html")
MAX_WS = 20


class IntentIn(BaseModel):
    target: str = Field("any", max_length=45)
    cls: str = Field("voice", max_length=10)
    ttl_s: int = Field(120, ge=1, le=3600)
    note: str = Field("", max_length=80)


class PinIn(BaseModel):
    flow: str = Field(max_length=120)
    cls: Optional[str] = Field(None, max_length=10)    # None => unpin


class ScenarioIn(BaseModel):
    name: str = Field(max_length=24)


class ModeIn(BaseModel):
    mode: str = Field(max_length=16)


class RateLimiter:
    """Token bucket per client: `burst` actions, refilled at `per_min`/minute."""

    def __init__(self, burst: int = 6, per_min: int = 12, clock=time.monotonic):
        self.burst, self.rate, self.clock = burst, per_min / 60.0, clock
        self.b = defaultdict(lambda: (float(burst), clock()))

    def allow(self, who: str) -> bool:
        tokens, t = self.b[who]
        now = self.clock()
        tokens = min(self.burst, tokens + (now - t) * self.rate)
        if tokens < 1:
            self.b[who] = (tokens, now)
            return False
        self.b[who] = (tokens - 1, now)
        return True


def create_app(controller, scenarios, call_monitor=None, limiter: Optional[RateLimiter] = None) -> FastAPI:
    app = FastAPI(title="QoSense", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    limiter = limiter or RateLimiter()
    ws_count = {"n": 0}

    def guard(request: Request):
        who = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
        if not limiter.allow(who):
            raise HTTPException(429, "slow down: demo actions are rate limited")

    def snapshot() -> dict:
        st = controller.state
        out = {"kpi": st["kpi"], "flows": st["flows"], "intents": st.get("intents", []),
               "events": list(st["events"])[:15], "scenarios": scenarios.status(),
               "tick_ms_p99": sorted(controller.tick_ms)[int(0.99 * (len(controller.tick_ms) - 1))] if controller.tick_ms else None}
        if call_monitor is not None:
            now = time.monotonic()
            out["call"] = {"rtt_ms": call_monitor.latest_rtt(now), "delay_ms": call_monitor.delay_ms(now),
                           "recent": call_monitor.recent(now, 5.0)[-50:], "loss": call_monitor.lost}
        return out

    @app.get("/api/status")
    def status():
        return snapshot()

    @app.get("/api/flows")
    def flows():
        return controller.state["flows"]

    @app.post("/api/intent")
    def add_intent(body: IntentIn, request: Request):
        guard(request)
        try:
            it = controller.intents.add(body.target, body.cls, body.ttl_s, body.note)
        except ValueError as e:
            raise HTTPException(422, str(e))
        controller.event(f"intent {it.id}: {it.cls} for {it.target} ({body.ttl_s}s)")
        return vars(it)

    @app.delete("/api/intent/{iid}")
    def del_intent(iid: str, request: Request):
        guard(request)
        if not controller.intents.remove(iid):
            raise HTTPException(404, "no such intent")
        return {"ok": True}

    @app.post("/api/flow/pin")
    def pin(body: PinIn, request: Request):
        guard(request)
        try:
            key = FlowKey.parse(body.flow)
            if body.cls is None:
                controller.classifier.unpin(key)
            else:
                controller.classifier.pin(key, body.cls)
        except ValueError as e:
            raise HTTPException(422, str(e))
        controller.event(f"user correction: {body.flow} -> {body.cls or 'auto'}")
        return {"ok": True}

    @app.post("/api/scenario")
    def scenario(body: ScenarioIn, request: Request):
        guard(request)
        try:
            return scenarios.start(body.name)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/mode")
    def mode(body: ModeIn, request: Request):
        guard(request)
        try:
            scenarios.set_mode(controller, body.mode)
        except ValueError as e:
            raise HTTPException(422, str(e))
        controller.event(f"mode -> {body.mode}")
        return {"mode": body.mode}

    @app.get("/healthz")
    def health():
        return {"ok": True, "ticks": controller.ticks}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        k = controller.state["kpi"]
        lines = []
        for name, key in (("qosense_delay_ms", "delay_ms"), ("qosense_rtt_ms", "rtt_ms"),
                          ("qosense_shaper_up_kbit", "up_kbit"), ("qosense_shaper_down_kbit", "down_kbit"),
                          ("qosense_achieved_up_kbit", "up_achieved_kbit"), ("qosense_achieved_down_kbit", "down_achieved_kbit"),
                          ("qosense_rollbacks_total", "rollbacks")):
            v = k.get(key)
            if v is not None:
                lines.append(f"{name} {v}")
        by_cls = defaultdict(float)
        for f in controller.state["flows"]:
            by_cls[f["class"]] += f["kbit"]
        for c in CLASSES:                      # per-class aggregates only: tiny label cardinality
            lines.append(f'qosense_class_kbit{{class="{c}"}} {by_cls.get(c, 0)}')
        lines.append(f"qosense_ticks_total {controller.ticks}")
        return "\n".join(lines) + "\n"

    @app.websocket("/api/live")
    async def live(ws: WebSocket):
        if ws_count["n"] >= MAX_WS:             # load shedding
            await ws.close(code=1013)
            return
        await ws.accept()
        ws_count["n"] += 1
        try:
            while True:
                await ws.send_text(json.dumps(snapshot(), default=str))
                await asyncio.sleep(1.0)
        except Exception:
            pass
        finally:
            ws_count["n"] -= 1

    @app.get("/report", include_in_schema=False)
    def report():
        if os.path.exists(REPORT):
            return FileResponse(REPORT)
        return JSONResponse({"detail": "report not generated yet"}, status_code=404)

    app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
    return app
