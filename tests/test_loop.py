"""Drive Controller.tick() with fake telemetry (no kernel needed)."""
from controller.common import FlowCounters, FlowKey
from controller.enforcer import Enforcer
from controller.main import Controller


class Fake:
    def __init__(self):
        self.t = 0.0
        self.cmds = []
        self.delay = 0.0
        self.pk = 0
        self.tin_bytes = {"r-wan": 0, "br-lan": 0}
        self.ach = {"r-wan": 0, "br-lan": 0}      # kbit/s achieved

    def clock(self): return self.t
    def ex(self, cmd, ns): self.cmds.append(cmd)

    def conntrack(self):
        self.pk += 25
        k = FlowKey("udp", "10.0.0.2", "198.51.100.10", 40000, 51234)   # a call on an unusual port
        return [FlowCounters(k, self.pk, self.pk * 172, self.pk, self.pk * 172)]

    def cake(self, dev):
        self.tin_bytes[dev] += int(self.ach[dev] * 1000 / 8 * 0.5)
        return {"tins": [{"bytes": self.tin_bytes[dev]}]}


def make(**kw):
    f = Fake()
    enf = Enforcer("r-wan", "br-lan", executor=f.ex, sleep=lambda s: None)
    enf.initial_setup(19_000, 95_000)
    c = Controller(enf, up_kbit=20_000, down_kbit=100_000, conntrack_fn=f.conntrack, cake_fn=f.cake,
                   delay_fn=lambda n: f.delay, rtt_fn=lambda n: 20 + f.delay, classifier_mode="rules",
                   clock=f.clock, **kw)
    return f, c


def run(f, c, n):
    for _ in range(n):
        f.t += 0.5
        c.tick()


def test_flow_is_classified_and_marked():
    f, c = make()
    run(f, c, 8)
    assert c.state["flows"] and c.state["flows"][0]["class"] == "web"      # rules miss the odd port
    assert any(cmd[0] == "conntrack" and cmd[-1] == "2" for cmd in f.cmds)
    c.intents.add("10.0.0.2", "voice", 60)                                 # user intent fixes it
    run(f, c, 2)
    assert any(cmd[0] == "conntrack" and cmd[-1] == "4" for cmd in f.cmds)


def test_capacity_cliff_reduces_down_rate_fast():
    f, c = make()
    f.ach["br-lan"] = 90_000; run(f, c, 6)
    f.delay = 120.0; f.ach["br-lan"] = 20_000          # WAN dropped to 20 Mbit, bufferbloat appears
    run(f, c, 4)
    assert c.enf.down_kbit < 60_000 and c.state["kpi"]["cliff"]
    assert any("tc" == cmd[0] and "change" in cmd for cmd in f.cmds)


def test_disabled_controller_holds_static_state():
    f, c = make(); c.enabled = False
    f.delay = 100.0; f.ach["br-lan"] = 20_000
    n = len(f.cmds); run(f, c, 5)
    assert len(f.cmds) == n and c.enf.down_kbit == 95_000


def test_heartbeat_file_and_tick_budget(tmp_path):
    f, c = make(heartbeat=str(tmp_path / "run" / "hb"))
    run(f, c, 3)
    assert (tmp_path / "run" / "hb").exists()
    assert max(c.tick_ms) < 400
