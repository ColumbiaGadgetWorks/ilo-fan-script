#!/usr/bin/env python3
"""Thermal test for ilo-fan: measure GPU temperature against fan speed under real
vLLM load, fit the controller's thermal model, and check the controller holds the
target.

Run as root on the GPU server (it stops ilo-fan.service while it runs and starts it
again at the end, also on errors or Ctrl-C):

    /opt/ilo-fan/venv/bin/python /opt/ilo-fan/thermal_test.py all
    /opt/ilo-fan/venv/bin/python /opt/ilo-fan/thermal_test.py calibrate
    /opt/ilo-fan/venv/bin/python /opt/ilo-fan/thermal_test.py validate \
        --set model.rth_ref_c_per_w=0.28 --set model.k=1.1

calibrate  holds fixed fan floors (high to low) under two loads until the hottest
           GPU settles: 4 streams of long unique prompts ("prefill", the heaviest
           realistic load, ~150-190 W per GPU on our V100s) and 16 generating streams
           ("decode", ~85 W). Then it fits T = ambient + P * rth_ref * (f_ref / f) ** k
           to every GPU's settled temperature and power (fit.json).
validate   runs the real Controller (from ilo_fan.py, config file plus --set
           overrides) through prefill, decode, single-stream and bursty chat-like
           load, with idle gaps to watch the fans come back down. `all` validates
           with the freshly fitted model.

Load comes from the vLLM API (key read from the start script, never printed).
Every 2 s a row goes to samples.csv: per-GPU temperature, power, utilisation, SM
clock and throttle reasons, the fan floor, the fans' actual DesiredSpeed and the
inlet temperature from the iLO. Results land in /root/thermal-test/<time>/.
"""
import argparse
import csv
import json
import math
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ilo_fan as F  # noqa: E402

PROMPT = ("Write a long, detailed technical history of machine tools, from the "
          "earliest lathes to modern CNC, with many specific examples.")
WORDS = ("spindle lathe gantry stepper servo coolant chuck collet arbor feed rate "
         "toolpath gcode encoder bearing ballscrew backlash fixture vise endmill "
         "router laser kerf plasma torch nozzle filament extruder hotend bed "
         "solder flux resistor capacitor inductor diode relay fuse breaker").split()


def log(msg):
    F.log(msg)


# ---------------------------------------------------------------- iLO access

class LockedIlo(F.Ilo):
    """One iLO SSH command at a time, shared by the controller and the monitor."""
    lock = threading.Lock()

    def run(self, cmd):
        with self.lock:
            return super().run(cmd)


class IloMonitor(threading.Thread):
    """Reads inlet temperature and every fan's DesiredSpeed, one command at a time."""

    def __init__(self, ilo, fans):
        super().__init__(daemon=True)
        self.ilo, self.fans = ilo, fans
        self.inlet = None
        self.fan_pct = {}
        self.history = []  # (time, mean fan %, inlet)
        self.stop = False

    def _read(self, path, key):
        try:
            m = re.search(key + r"=(\d+)", self.ilo.run(f"show {path}"))
            return int(m.group(1)) if m else None
        except Exception as e:
            log(f"iLO read {path} failed: {e}")
            return None

    def run(self):
        while not self.stop:
            v = self._read("/system1/sensor1", "CurrentReading")
            if v is not None:
                self.inlet = v
            for n in self.fans:
                if self.stop:
                    return
                v = self._read(f"/system1/fan{n + 1}", "DesiredSpeed")
                if v is not None:
                    self.fan_pct[n] = v
            self.history.append((time.time(), self.fan_mean(), self.inlet))
            time.sleep(5)

    def fan_mean(self):
        v = [x for x in self.fan_pct.values() if x is not None]
        return statistics.mean(v) if v else None


# ---------------------------------------------------------------- load

def read_api_key(script):
    text = open(script).read()
    m = re.search(r"--api-key\s+['\"]?([^'\"\s\\]+)", text)
    return m.group(1) if m else ""


class Load:
    """N streaming workers. Lowering N closes the extra streams at once, which
    makes vLLM abort those requests."""

    def __init__(self, base, key):
        self.base, self.key = base.rstrip("/"), key
        self.n = 0
        self.kind = "decode"
        self.prefill_words = 6000
        self.workers = []
        self.done = False
        self.tokens = 0
        self.errors = 0
        with urllib.request.urlopen(self._req("/v1/models"), timeout=10) as r:
            self.model = json.load(r)["data"][0]["id"]

    def _req(self, path, body=None):
        h = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        return urllib.request.Request(self.base + path, data=data, headers=h)

    def set(self, n):
        self.n = n
        while len(self.workers) < n:
            t = threading.Thread(target=self._work, args=(len(self.workers),), daemon=True)
            self.workers.append(t)
            t.start()

    def stop(self):
        self.n = 0
        self.done = True

    def _body(self):
        if self.kind == "prefill":
            # A long prompt that prefix caching cannot reuse, and one output token.
            words = " ".join(random.choice(WORDS) for _ in range(self.prefill_words))
            return {"model": self.model, "stream": True, "max_tokens": 1,
                    "messages": [{"role": "user", "content": words + "\nSummarize."}],
                    "chat_template_kwargs": {"enable_thinking": False}}
        return {"model": self.model, "stream": True, "max_tokens": 4096,
                "ignore_eos": True, "temperature": 0.8,
                "messages": [{"role": "user", "content": PROMPT}],
                "chat_template_kwargs": {"enable_thinking": False}}

    def _work(self, i):
        while not self.done:
            if i >= self.n:
                time.sleep(0.3)
                continue
            body = self._body()
            try:
                with urllib.request.urlopen(self._req("/v1/chat/completions", body),
                                            timeout=120) as r:
                    for line in r:
                        if i >= self.n or self.done:
                            break
                        if line.startswith(b"data: {"):
                            self.tokens += 1
            except Exception as e:
                self.errors += 1
                if self.errors <= 5:
                    log(f"load worker {i}: {e}")
                time.sleep(2)


# ---------------------------------------------------------------- sampling

def read_gpus_full():
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,temperature.gpu,power.draw,utilization.gpu,"
         "clocks.sm,clocks_throttle_reasons.active", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=True).stdout
    res = []
    for line in out.strip().splitlines():
        i, t, p, u, clk, why = [x.strip() for x in line.split(",")]
        res.append({"i": int(i), "t": float(t), "p": float(p), "u": float(u),
                    "clk": float(clk), "why": why})
    return res


class Recorder(threading.Thread):
    def __init__(self, path, mon, load, state):
        super().__init__(daemon=True)
        self.mon, self.load, self.state = mon, load, state
        self.rows = []
        self.stop = False
        self.f = open(path, "w", newline="")
        self.w = None

    def run(self):
        while not self.stop:
            t0 = time.time()
            try:
                g = read_gpus_full()
                row = {"time": round(t0), "phase": self.state["phase"],
                       "streams": self.load.n, "kind": self.load.kind, "floor": self.state.get("floor"),
                       "want": self.state.get("want"), "fan_avg": self.mon.fan_mean(),
                       "inlet": self.mon.inlet,
                       "t_hot": max(x["t"] for x in g), "p_max": max(x["p"] for x in g)}
                for x in g:
                    for k in ("t", "p", "u", "clk", "why"):
                        row[f"g{x['i']}_{k}"] = x[k]
                if self.w is None:
                    self.w = csv.DictWriter(self.f, fieldnames=list(row))
                    self.w.writeheader()
                self.w.writerow(row)
                self.f.flush()
                self.rows.append(row)
            except Exception as e:
                log(f"sample error: {e}")
            time.sleep(max(0.0, 2.0 - (time.time() - t0)))

    def since(self, t):
        return [r for r in self.rows if r["time"] >= t]


def slope_c_per_min(rows):
    if len(rows) < 10:
        return 99.0
    xs = [r["time"] for r in rows]
    ys = [r["t_hot"] for r in rows]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    return 60.0 * sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def throttle_bits(row):
    bits = 0
    for k, v in row.items():
        if k.endswith("_why"):
            try:
                bits |= int(v, 16)
            except (TypeError, ValueError):
                pass
    return bits


def summarize(name, rows, mon, t_from, extra=None):
    tail = [r for r in rows if r["time"] >= rows[-1]["time"] - 120] if rows else []
    fans = [f for (t, f, _) in mon.history if t >= t_from and f is not None]
    s = {"phase": name, "seconds": (rows[-1]["time"] - rows[0]["time"]) if rows else 0,
         "t_hot_end": round(statistics.mean(r["t_hot"] for r in tail), 1) if tail else None,
         "t_hot_max": max(r["t_hot"] for r in rows) if rows else None,
         "p_max_end": round(statistics.mean(r["p_max"] for r in tail), 1) if tail else None,
         "p_max_p95": round(sorted(r["p_max"] for r in rows)[int(0.95 * (len(rows) - 1))], 1)
         if rows else None,
         "fan_avg_end": round(statistics.mean(fans[-2:]), 1) if fans else None,
         "inlet": mon.inlet,
         "secs_over_72": 2 * sum(1 for r in rows if r["t_hot"] > 72),
         # 0x08 HW slowdown, 0x20 SW thermal, 0x40 HW thermal; 0x04 is the power cap
         "thermal_throttle_samples": sum(1 for r in rows if throttle_bits(r) & 0x68),
         "power_cap_samples": sum(1 for r in rows if throttle_bits(r) & 0x04)}
    gpus = sorted({k[1] for r in tail for k in r if k.startswith("g") and k.endswith("_t")})
    for i in gpus:
        s[f"g{i}_t_end"] = round(statistics.mean(r[f"g{i}_t"] for r in tail), 1)
        s[f"g{i}_p_end"] = round(statistics.mean(r[f"g{i}_p"] for r in tail), 1)
    if gpus:
        hot = max(gpus, key=lambda i: s[f"g{i}_t_end"])
        s["hot_gpu"] = int(hot)
        s["p_hot_end"] = s[f"g{hot}_p_end"]
    if extra:
        s.update(extra)
    return s


# ---------------------------------------------------------------- phases

class Test:
    def __init__(self, args, cfg):
        self.args, self.cfg = args, cfg
        self.out = os.path.join(args.out, time.strftime("%Y%m%d-%H%M"))
        os.makedirs(self.out, exist_ok=True)
        self.ilo = LockedIlo(cfg)
        self.mon = IloMonitor(self.ilo, cfg["ilo"]["fans"])
        self.load = Load(args.api, read_api_key(args.start_script))
        self.state = {"phase": "start", "floor": None, "want": None}
        self.rec = Recorder(os.path.join(self.out, "samples.csv"), self.mon, self.load,
                            self.state)
        self.results = []

    def start(self):
        log(f"output: {self.out}, model: {self.load.model}")
        self.mon.start()
        self.rec.start()
        while self.mon.fan_mean() is None:  # first full iLO sweep
            time.sleep(2)

    def set_floor(self, pct):
        self.state["floor"] = pct
        self.ilo.set_floor(pct)

    def record(self, s):
        self.results.append(s)
        log("RESULT " + json.dumps(s))
        with open(os.path.join(self.out, "phases.jsonl"), "a") as f:
            f.write(json.dumps(s) + "\n")

    def hold(self, name, floor, streams, kind):
        """Fixed floor and load until the hottest GPU settles. False if aborted hot."""
        a = self.args
        self.state["phase"] = name
        log(f"phase {name}: floor {floor}%, {streams} {kind} streams")
        self.set_floor(floor)
        self.load.kind = kind
        self.load.set(streams)
        t0 = time.time()
        aborted = False
        while True:
            time.sleep(4)
            rows = self.rec.since(t0)
            el = time.time() - t0
            if rows and rows[-1]["t_hot"] >= a.abort_c:
                aborted = True
                log(f"phase {name}: {rows[-1]['t_hot']}C >= {a.abort_c}C, aborting")
                break
            if el >= a.min_s and abs(slope_c_per_min(self.rec.since(time.time() - 120))) < 0.3:
                break
            if el >= a.max_s:
                break
        self.load.set(0)
        self.record(summarize(name, self.rec.since(t0), self.mon, t0,
                              {"floor": floor, "streams": streams, "kind": kind,
                               "aborted": aborted}))
        if aborted:
            self.set_floor(80)
        return not aborted

    def calibrate(self):
        # Heaviest realistic load (long prompts) and steady chat decode.
        for streams, kind, floors in ((4, "prefill", (80, 65, 50, 40, 30)),
                                      (16, "decode", (50, 40, 30))):
            for fl in floors:
                if not self.hold(f"cal_{kind}_{fl}", fl, streams, kind):
                    break
        self.load.set(0)
        self.set_floor(65)
        self.state["phase"] = "cooldown"
        time.sleep(120)
        return self.fit()

    def fit(self):
        """Fit T = ambient + P * rth_ref * (f_ref / f) ** k to every GPU's settled
        temperature and mean power in every finished calibration phase. ambient is
        fitted too: air reaches the GPUs several degrees above the iLO inlet reading
        (19 C inlet fitted as 25-27 C on our DL380), and forcing it to the inlet
        bends k far too low."""
        good = [s for s in self.results
                if s["phase"].startswith("cal_") and not s["aborted"] and s["fan_avg_end"]
                and s["inlet"] is not None]
        pts = []  # (fan %, power W, temperature C, phase, gpu)
        for s in good:
            for key in s:
                if key.startswith("g") and key.endswith("_p_end") and s[key] > 60:
                    g = key[1:-6]
                    pts.append((s["fan_avg_end"], s[key], s[f"g{g}_t_end"], s["phase"], int(g)))
        res = {"points": [{"phase": ph, "gpu": g, "fan": f, "p": p, "t": t}
                          for f, p, t, ph, g in pts]}
        f_ref = self.cfg["model"]["f_ref_pct"]
        if len({round(f) for f, *_ in pts}) >= 2:
            inlet = min(s["inlet"] for s in good)
            best = None
            for a10 in range(int(inlet * 10), int((inlet + 15) * 10) + 1, 2):
                amb = a10 / 10
                ys = [t - amb for _, _, t, *_ in pts]
                for k100 in range(40, 251, 2):
                    k = k100 / 100
                    xs = [p * (f_ref / f) ** k for f, p, *_ in pts]
                    r = sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs)
                    err = sum((r * x - y) ** 2 for x, y in zip(xs, ys))
                    if best is None or err < best[0]:
                        best = (err, amb, k, r)
            err, amb, k, r = best
            res.update({"ambient_c": amb, "rth_ref_c_per_w": round(r, 4), "f_ref_pct": f_ref,
                        "k": k, "rms_c": round(math.sqrt(err / len(pts)), 2),
                        "residual_c": [round(t - (amb + p * r * (f_ref / f) ** k), 1)
                                       for f, p, t, *_ in pts]})
        for kind in ("prefill", "decode"):
            ph = [s for s in self.results if s["phase"].startswith(f"cal_{kind}")]
            if ph:
                res[f"{kind}_p_hot_mean_w"] = round(statistics.mean(s["p_hot_end"] for s in ph), 1)
                res[f"{kind}_p_max_p95_w"] = max(s["p_max_p95"] for s in ph)
        log("FIT " + json.dumps(res))
        with open(os.path.join(self.out, "fit.json"), "w") as f:
            json.dump(res, f, indent=1)
        return res

    def validate(self, cfg):
        ctl = F.Controller(cfg, self.ilo)
        orig = ctl.demand

        def demand(*a):
            want, d = orig(*a)
            self.state["want"] = round(want, 1)
            return want, d
        ctl.demand = demand
        stop = {"flag": False}

        def loop():
            while not stop["flag"]:
                t0 = time.monotonic()
                try:
                    ctl.tick(t0)
                    self.state["floor"] = ctl.step
                except Exception as e:
                    log(f"tick error: {e}")
                time.sleep(max(0.0, cfg["control"]["interval_s"] - (time.monotonic() - t0)))
        th = threading.Thread(target=loop, daemon=True)
        th.start()

        def phase(name, seconds, load_fn):
            self.state["phase"] = name
            log(f"phase {name}: {seconds}s")
            t0 = time.time()
            while time.time() - t0 < seconds:
                n, kind = load_fn(time.time() - t0)
                self.load.kind = kind
                self.load.set(n)
                time.sleep(1)
            self.load.set(0)
            rows = self.rec.since(t0)
            floors = [r["floor"] for r in rows if r["floor"] is not None]
            self.record(summarize(name, rows, self.mon, t0, {
                "floor_mean": round(statistics.mean(floors), 1) if floors else None,
                "floor_max": max(floors) if floors else None}))

        idle = lambda t: (0, "decode")  # noqa: E731
        try:
            sc = self.args.scale
            phase("val_idle_start", int(120 * sc), idle)
            phase("val_prefill_4x", int(720 * sc), lambda t: (4, "prefill"))
            phase("val_idle_after_prefill", int(300 * sc), idle)
            phase("val_decode_16x", int(600 * sc), lambda t: (16, "decode"))
            phase("val_decode_1x", int(480 * sc), lambda t: (1, "decode"))
            phase("val_idle_after_decode", int(300 * sc), idle)
            # Chat-like: a long prompt and its answer every 90 s.
            phase("val_bursty", int(720 * sc),
                  lambda t: (2, "prefill") if t % 90 < 8 else
                  ((2, "decode") if t % 90 < 30 else (0, "decode")))
            phase("val_idle_end", int(300 * sc), idle)
        finally:
            stop["flag"] = True
            th.join(timeout=60)

    def close(self):
        self.load.stop()
        self.mon.stop = True
        self.rec.stop = True
        time.sleep(3)
        with open(os.path.join(self.out, "summary.txt"), "w") as f:
            for s in self.results:
                f.write(json.dumps(s) + "\n")


def apply_overrides(cfg, sets):
    for s in sets or []:
        key, val = s.split("=", 1)
        sec, name = key.split(".", 1)
        old = cfg[sec][name]
        cfg[sec][name] = type(old)(val) if not isinstance(old, list) else json.loads(val)
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["calibrate", "validate", "all"])
    ap.add_argument("--config", default="/etc/ilo-fan/ilo-fan.toml")
    ap.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE",
                    help="override a controller setting for validation")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--start-script", default="/home/cgw/bin/start-llm-active.sh")
    ap.add_argument("--out", default="/root/thermal-test")
    ap.add_argument("--min-s", type=int, default=240, help="shortest calibration phase")
    ap.add_argument("--max-s", type=int, default=600, help="longest calibration phase")
    ap.add_argument("--abort-c", type=float, default=83.0)
    ap.add_argument("--scale", type=float, default=1.0, help="validation length factor")
    args = ap.parse_args()

    # Stop cleanly (and restart ilo-fan) on SIGTERM or SIGINT. A job started in the
    # background by a non-interactive shell inherits SIGINT as ignored, so set both.
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)

    cfg = F.load_config(args.config)
    test = Test(args, cfg)  # fails early (e.g. vLLM down) while ilo-fan still runs
    subprocess.run(["systemctl", "stop", "ilo-fan"], check=False)
    try:
        test.start()
        if args.mode in ("calibrate", "all"):
            fit = test.calibrate()
            if args.mode == "all" and 0.3 <= fit.get("k", 0) <= 3.0:
                cfg = F.merge(cfg, {"model": {"rth_ref_c_per_w": fit["rth_ref_c_per_w"],
                                              "k": fit["k"]},
                                    "control": {"ambient_c": fit["ambient_c"]}})
        if args.mode in ("validate", "all"):
            test.validate(apply_overrides(cfg, args.set))
    finally:
        test.close()
        subprocess.run(["systemctl", "start", "ilo-fan"], check=False)
        log("done, ilo-fan.service started again")


if __name__ == "__main__":
    main()
