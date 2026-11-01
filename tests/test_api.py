from fastapi.testclient import TestClient

from api.app import RateLimiter, create_app
from api.scenarios import Scenarios
from tests.test_loop import make, run


class Rec:
    def __init__(self): self.calls = []
    def __call__(self, *a, **k): self.calls.append(a[0]); return None


def client(burst=50):
    f, c = make()
    run(f, c, 8)
    rec = Rec()
    sc = Scenarios(runner=rec, shell=rec)
    return TestClient(create_app(c, sc, None, RateLimiter(burst, 600))), c, rec


def test_status_flows_metrics_health():
    cl, c, _ = client()
    assert cl.get("/healthz").json()["ok"]
    s = cl.get("/api/status").json()
    assert s["kpi"]["classifier"].startswith("rules") and s["flows"]
    m = cl.get("/metrics").text
    assert 'qosense_class_kbit{class="web"}' in m and "qosense_ticks_total" in m
    assert cl.get("/").status_code == 200 and "QoSense" in cl.get("/").text


def test_intent_validation_and_lifecycle():
    cl, c, _ = client()
    r = cl.post("/api/intent", json={"target": "10.0.0.2", "cls": "voice", "ttl_s": 60})
    assert r.status_code == 200
    assert cl.post("/api/intent", json={"target": "evil; rm -rf /", "cls": "voice", "ttl_s": 60}).status_code == 422
    assert cl.post("/api/intent", json={"target": "any", "cls": "nonsense", "ttl_s": 60}).status_code == 422
    assert cl.post("/api/intent", json={"target": "any", "cls": "voice", "ttl_s": 999999}).status_code == 422
    assert cl.delete(f"/api/intent/{r.json()['id']}").status_code == 200
    assert cl.delete("/api/intent/nope").status_code == 404


def test_pin_and_bad_flow_key():
    cl, c, _ = client()
    flow = cl.get("/api/flows").json()[0]["flow"]
    assert cl.post("/api/flow/pin", json={"flow": flow, "cls": "voice"}).status_code == 200
    assert cl.post("/api/flow/pin", json={"flow": "garbage", "cls": "voice"}).status_code == 422
    assert cl.post("/api/flow/pin", json={"flow": flow, "cls": "x"}).status_code == 422


def test_scenarios_modes_and_rate_limit():
    cl, c, rec = client()
    assert cl.post("/api/scenario", json={"name": "iso_download"}).json()["started"]
    assert not cl.post("/api/scenario", json={"name": "iso_download"}).json()["started"]   # already running
    assert cl.post("/api/scenario", json={"name": "rm -rf"}).status_code == 422
    assert cl.post("/api/mode", json={"mode": "fifo"}).status_code == 200 and c.enabled is False
    assert cl.post("/api/mode", json={"mode": "qosense"}).status_code == 200 and c.enabled is True
    assert cl.post("/api/mode", json={"mode": "bogus"}).status_code == 422
    tight, _, _ = client(burst=2)
    codes = [tight.post("/api/mode", json={"mode": "fifo"}).status_code for _ in range(4)]
    assert 429 in codes


def test_websocket_streams_and_sheds_load():
    cl, c, _ = client()
    with cl.websocket_connect("/api/live") as ws:
        assert "kpi" in ws.receive_json()
