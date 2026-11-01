"""Link-capacity estimator: a clean-room, simplified take on the cake-autorate idea.

Inputs each tick: loaded RTT delta over baseline, achieved throughput and the
current shaper rate. Output: the next shaper rate.

* Delay above threshold while the link is busy  -> decrease (towards 90% of the
  achieved rate), clamped to -15%/tick.
* Delay low and the shaper is the limit (achieved >= 80% of shaper) -> probe up
  by up to +5%/tick, slowing geometrically as it nears the last rate that caused bufferbloat; clamped to the configured max.
* CUSUM cliff detector: a sustained positive drift in delay *with* a collapse in
  achieved/shaper ratio flags a capacity cliff; the decrease clamp is then
  widened to -50%/tick so recovery takes seconds rather than minutes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EstimatorConfig:
    min_kbit: int = 2_000
    max_kbit: int = 100_000
    delay_thresh_ms: float = 10.0     # loaded-RTT increase that counts as bufferbloat
    max_step_down: float = 0.15
    max_step_up: float = 0.05         # probe step when far from the last rate that caused bufferbloat
    min_step_up: float = 0.005        # smallest probe step (a 5% overshoot at 500 ms ticks = ~30 ms of queue)
    approach_gain: float = 0.4        # step = gain * remaining gap to the last bad rate (geometric approach)
    bad_rate_decay: float = 1.01      # the remembered bad rate drifts up so capacity gains are still found
    probe_delay_frac: float = 0.5     # only probe up while delay is below this fraction of the threshold
    cliff_step_down: float = 0.50
    util_up: float = 0.80
    cusum_k_ms: float = 5.0           # slack per sample
    cusum_h_ms: float = 80.0          # alarm threshold (a single 80 ms spike must not trip it)
    use_cusum: bool = True


class CapacityEstimator:
    def __init__(self, cfg: EstimatorConfig, start_kbit: int):
        self.cfg = cfg
        self.rate = max(cfg.min_kbit, min(cfg.max_kbit, start_kbit))
        self._cusum = 0.0
        self.cliff = False
        self.cliff_events = 0
        self.bad_rate = None            # last shaper rate at which delay exceeded the threshold

    def _update_cusum(self, delay_ms: float) -> None:
        c = self.cfg
        self._cusum = max(0.0, self._cusum + (delay_ms - c.delay_thresh_ms) - c.cusum_k_ms)
        if self.cfg.use_cusum and self._cusum > c.cusum_h_ms:
            if not self.cliff:
                self.cliff_events += 1
            self.cliff = True
        elif self._cusum < c.cusum_h_ms * 0.25:
            self.cliff = False

    def step(self, delay_ms: float, achieved_kbit: float) -> int:
        """Return the new shaper rate in kbit/s."""
        c = self.cfg
        self._update_cusum(delay_ms)
        target = float(self.rate)
        if delay_ms > c.delay_thresh_ms and achieved_kbit > max(1000.0, 0.05 * self.rate):
            # shave towards 90% of what actually got through
            target = min(self.rate, 0.9 * achieved_kbit) if achieved_kbit < self.rate else self.rate * 0.95
            down = c.cliff_step_down if self.cliff else c.max_step_down
            target = max(target, self.rate * (1.0 - down))
            self.bad_rate = float(self.rate)
        else:
            if self.bad_rate is not None:
                self.bad_rate = min(float(c.max_kbit), self.bad_rate * c.bad_rate_decay)
            if delay_ms <= c.delay_thresh_ms * c.probe_delay_frac and achieved_kbit >= c.util_up * self.rate:
                step = c.max_step_up
                if self.bad_rate is not None:
                    gap = (self.bad_rate - self.rate) / self.rate
                    step = min(c.max_step_up, max(c.min_step_up, c.approach_gain * gap))
                target = self.rate * (1.0 + step)
        new = int(max(c.min_kbit, min(c.max_kbit, target)))
        self.rate = new
        return new
