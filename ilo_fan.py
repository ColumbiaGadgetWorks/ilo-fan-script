#!/usr/bin/env python3
"""ilo-fan: GPU-aware, predictive fan floors for HPE iLO 4 servers.

Needs iLO 4 v2.77 patched with ilo4_unlock (https://github.com/kendallgoto/ilo4_unlock),
which adds the `fan` SSH command. Without the patch, use `run --dry-run` to see what
the controller would do.

The iLO cannot see third-party GPU temperatures, so its fans ignore GPU heat. This
script reads the GPUs with nvidia-smi (and optionally vLLM's request count), predicts
where the GPU temperature is heading, and raises the iLO's per-fan *minimum* speed
(`fan p N min X`). It never lowers a maximum, so the iLO's own control loops still
work on top and can always spin the fans faster.

Subcommands:
  run [--dry-run]   control loop (what the systemd service runs)
  trust             connect once and save the iLO's SSH host key
  test              check SSH login works (read-only)
  probe             one write test: fan 0 floor to 20% for 20 s, read it back, release
  status            print GPU readings, model output and iLO fan targets
  set PCT           set every configured fan's floor once (0 = release to stock)
"""

import argparse
import math
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request

DEFAULTS = {
    "ilo": {
        "host": "",
        "user": "fanctl",
        "key": "/etc/ilo-fan/id_rsa",
        "known_hosts": "/etc/ilo-fan/known_hosts",
        "port": 22,
        "timeout_s": 20,
        "cmd_gap_s": 1.0,     # pause between SSH commands; the iLO is slow
        "fans": [0, 1, 2, 3, 4, 5],  # 0-based: fan p 0 = "Fan 1"
        "release_pwm": 20,    # floor that hands a fan back to the stock curve
    },
    "control": {
        "interval_s": 2.0,       # how often GPUs are read (local, cheap)
        "target_c": 70.0,        # GPU temperature to hold under load
        "emergency_c": 84.0,     # at or above this: all configured fans 100%
        "ambient_c": 22.0,       # room/inlet air estimate (conservative)
        "steps_pct": [0, 30, 40, 50, 65, 80, 100],  # 0 = stock iLO curve
        "hold_s": 120,           # wait after the last high demand before stepping down
        "step_down_every_s": 30, # then step down one level at a time
        "lookahead_s": 30,       # predict temperature this far ahead from the trend
        "kp_pct_per_c": 4.0,     # extra floor per degree of predicted overshoot
        "push_within_c": 10.0,   # push only once the GPU is this close to target
                                 # (a load step's first seconds extrapolate far too hot)
        "ki_pct_per_c_s": 0.05,  # slow trim that corrects model error
        "trim_min_pct": -15.0,
        "trim_max_pct": 40.0,
        "idle_power_w": 45.0,    # below this with no requests: release to stock
        "reapply_s": 300,        # resend the current floor (iLO resets wipe it)
        "park_pct": 45,          # floor left in place when the service stops
        "status_every_s": 60,
    },
    "model": {
        # GPU temperature rise per watt at a given fan speed:
        #   rth(f) = rth_ref * (f_ref / f) ** k
        # Defaults were measured on a DL380 Gen9 with a V100 SXM2 on a PCIe adapter:
        #   ~18 % fans, 75 W -> 82 C ; 43 % fans, 150 W -> 65 C ; inlet 19 C.
        "rth_ref_c_per_w": 0.307,
        "f_ref_pct": 43.0,
        "k": 1.16,
    },
    "anticipate": {
        # Read vLLM's /metrics so fans start before power or temperature rise.
        "vllm_metrics_url": "",       # e.g. http://127.0.0.1:8000/metrics
        "busy_power_w": 150.0,        # assumed draw while a request is running
    },
    "failsafe": {
        # If the iLO cannot be reached and the GPU is hot, cap GPU power instead.
        "enabled": True,
        "hot_c": 82.0,
        "normal_limit_w": 150,
        "reduced_limit_w": 100,
        "after_failures": 2,
    },
    "log": {"csv": ""},  # optional CSV path for tuning
}


def merge(base, over):
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    over = {}
    if path:
        with open(path, "rb") as f:
            over = tomllib.load(f)
    return merge(DEFAULTS, over)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- inputs

def read_gpus():
    """Return a list of dicts: index, temp_c, power_w, limit_w."""
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,temperature.gpu,power.draw,power.limit",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=True).stdout
    gpus = []
    for line in out.strip().splitlines():
        idx, t, p, lim = [x.strip() for x in line.split(",")]
        gpus.append({"index": int(idx), "temp_c": float(t),
                     "power_w": float(p) if p not in ("[N/A]", "") else 0.0,
                     "limit_w": float(lim) if lim not in ("[N/A]", "") else 0.0})
    return gpus


def read_vllm_busy(url):
    """Requests running + waiting in vLLM, or None if unavailable."""
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=1.0) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    total = 0.0
    for line in text.splitlines():
        name = re.split(r"[{\s]", line, maxsplit=1)[0]
        if name in ("vllm:num_requests_running", "vllm:num_requests_waiting"):
            try:
                total += float(line.rsplit(" ", 1)[1])
            except ValueError:
                pass
    return total


# ---------------------------------------------------------------- model

def rth(cfg, fan_pct):
    m = cfg["model"]
    f = max(fan_pct, 5.0)
    return m["rth_ref_c_per_w"] * (m["f_ref_pct"] / f) ** m["k"]


def fan_needed(cfg, power_w, target_c, ambient_c):
    """Fan % whose steady-state GPU temperature at power_w equals target_c."""
    m = cfg["model"]
    headroom = target_c - ambient_c
    if power_w <= 0:
        return 0.0
    if headroom <= 1:
        return 100.0
    return m["f_ref_pct"] * (power_w * m["rth_ref_c_per_w"] / headroom) ** (1.0 / m["k"])


def pct_to_pwm(pct):
    return max(0, min(255, round(pct * 255 / 100)))


# ---------------------------------------------------------------- iLO

class Ilo:
    """One SSH connection per command. Chaining several `fan` commands in one
    line has crashed iLOs, and OpenSSH 10.x cannot negotiate with iLO 4 at all,
    so this uses paramiko, which still speaks diffie-hellman-group14-sha1 and
    ssh-rsa."""

    def __init__(self, cfg, dry_run=False):
        self.c = cfg["ilo"]
        self.dry = dry_run
        self.failures = 0

    def _client(self, trust=False):
        import paramiko
        cl = paramiko.SSHClient()
        if trust:
            cl.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            cl.load_host_keys(self.c["known_hosts"])
            cl.set_missing_host_key_policy(paramiko.RejectPolicy())
        cl.connect(
            self.c["host"], port=self.c["port"], username=self.c["user"],
            key_filename=self.c["key"], look_for_keys=False, allow_agent=False,
            timeout=self.c["timeout_s"], banner_timeout=self.c["timeout_s"],
            auth_timeout=self.c["timeout_s"],
            # iLO 4 only verifies plain ssh-rsa (SHA-1) signatures.
            disabled_algorithms={"pubkeys": ["rsa-sha2-512", "rsa-sha2-256"]})
        return cl

    def run(self, cmd):
        if self.dry:
            log(f"[dry-run] ilo> {cmd}")
            return ""
        cl = self._client()
        try:
            _, out, _ = cl.exec_command(cmd, timeout=self.c["timeout_s"])
            text = out.read().decode("utf-8", "replace")
        finally:
            cl.close()
        time.sleep(self.c["cmd_gap_s"])
        return text

    def trust(self):
        cl = self._client(trust=True)
        cl.save_host_keys(self.c["known_hosts"])
        cl.close()

    def set_floor(self, pct, fans=None):
        """Returns True if every command was sent."""
        pwm = self.c["release_pwm"] if pct <= 0 else pct_to_pwm(pct)
        ok = True
        for n in (fans if fans is not None else self.c["fans"]):
            try:
                self.run(f"fan p {n} min {pwm}")
            except Exception as e:
                ok = False
                log(f"iLO command failed for fan {n}: {e}")
        self.failures = 0 if ok else self.failures + 1
        return ok

    def fan_targets(self):
        """Fan 'DesiredSpeed' percentages from the iLO's own CLI (readable even
        though the patched `fan` command prints nothing)."""
        res = {}
        for n in self.c["fans"]:
            text = self.run(f"show /system1/fan{n + 1}")
            m = re.search(r"DesiredSpeed=(\d+)", text)
            res[n] = int(m.group(1)) if m else None
        return res


# ---------------------------------------------------------------- controller

class Controller:
    def __init__(self, cfg, ilo):
        self.cfg, self.ilo = cfg, ilo
        c = cfg["control"]
        self.steps = sorted(set(c["steps_pct"]) | {0})
        # Current floor in % (0 = stock). None = unknown: the iLO may still hold
        # the park floor from the last stop, so the first tick always sends.
        self.step = None
        self.trim = 0.0
        self.prev = None              # (time, temp) for the trend
        self.slope = 0.0              # degrees per second, smoothed
        self.last_high = 0.0
        self.last_down = 0.0
        self.last_sent = 0.0
        self.last_status = 0.0
        self.retry_at = 0.0
        self.power_reduced = False
        self.csv = None
        if cfg["log"]["csv"]:
            self.csv = open(cfg["log"]["csv"], "a", buffering=1)

    def demand(self, gpus, busy, now):
        """Return (wanted_pct, details) for the hottest-demand GPU."""
        c, a = self.cfg["control"], self.cfg["anticipate"]
        hot = max(gpus, key=lambda g: g["temp_c"])
        t = hot["temp_c"]

        # Temperature trend (exponential smoothing of dT/dt).
        if self.prev:
            dt = max(now - self.prev[0], 1e-3)
            self.slope = 0.7 * self.slope + 0.3 * ((t - self.prev[1]) / dt)
        self.prev = (now, t)
        t_pred = t + max(self.slope, 0.0) * c["lookahead_s"]

        # Feedforward: what fan speed the current (or about-to-be) power needs.
        p_ff = max(g["power_w"] for g in gpus)
        if busy:
            p_ff = max(p_ff, a["busy_power_w"])
        ff = fan_needed(self.cfg, p_ff, c["target_c"], c["ambient_c"])

        # Feedback: slow trim on the actual error, plus a push on predicted overshoot.
        err = t - c["target_c"]
        if err > 0 or self.trim > 0:
            self.trim += c["ki_pct_per_c_s"] * err * c["interval_s"]
        else:
            self.trim *= 0.98  # drift back toward zero when comfortably cool
        self.trim = min(max(self.trim, c["trim_min_pct"]), c["trim_max_pct"])
        push = 0.0
        if t >= c["target_c"] - c["push_within_c"]:
            push = c["kp_pct_per_c"] * max(0.0, t_pred - c["target_c"])

        want = ff + self.trim + push
        idle = (p_ff < c["idle_power_w"] and not busy and t < c["target_c"] - 10)
        if idle:
            want = 0.0
        if t >= c["emergency_c"]:
            want = 100.0
        return want, {"temp": t, "pred": t_pred, "power": p_ff, "ff": ff,
                      "trim": self.trim, "push": push, "busy": busy}

    def quantize(self, want):
        if want <= 0:
            return 0
        for s in self.steps:
            if s >= want and s > 0:
                return s
        return self.steps[-1]

    def tick(self, now):
        c = self.cfg["control"]
        gpus = read_gpus()
        busy = read_vllm_busy(self.cfg["anticipate"]["vllm_metrics_url"])
        want, d = self.demand(gpus, bool(busy), now)
        target_step = self.quantize(want)

        new = self.step
        if self.step is None:
            new = target_step                   # first send after start
            self.last_high = now
        elif target_step > self.step:
            new = target_step                   # ramp up at once
            self.last_high = now
        elif target_step == self.step:
            self.last_high = now
        elif (now - self.last_high >= c["hold_s"]
              and now - self.last_down >= c["step_down_every_s"]):
            lower = [s for s in self.steps if s < self.step]
            new = max(lower[-1], target_step)   # one level at a time
            self.last_down = now

        changed = new != self.step
        due = bool(self.step) and now - self.last_sent >= c["reapply_s"]
        if (changed or due) and now >= self.retry_at:
            why = "change" if changed else "reapply"
            old = "?" if self.step is None else self.step
            log(f"floor {old}% -> {new}% ({why}) gpu {d['temp']:.0f}C "
                f"pred {d['pred']:.0f}C {d['power']:.0f}W want {want:.0f}% "
                f"(ff {d['ff']:.0f} trim {d['trim']:+.0f} push {d['push']:.0f}"
                f"{' busy' if d['busy'] else ''})")
            if self.ilo.set_floor(new):
                self.step = new
                self.last_sent = now
            else:
                self.retry_at = now + 30  # iLO unreachable: don't hammer it
        self.failsafe(gpus, d["temp"])

        if now - self.last_status >= c["status_every_s"]:
            self.last_status = now
            log(f"status gpu {d['temp']:.0f}C {d['power']:.0f}W floor {self.step}% "
                f"want {want:.0f}%{' busy' if d['busy'] else ''}")
        if self.csv:
            self.csv.write(f"{time.time():.0f},{d['temp']:.1f},{d['pred']:.1f},{d['power']:.1f},"
                           f"{int(bool(d['busy']))},{want:.1f},{self.step}\n")

    def failsafe(self, gpus, temp):
        f = self.cfg["failsafe"]
        if not f["enabled"] or self.ilo.dry:
            return
        if (not self.power_reduced and self.ilo.failures >= f["after_failures"]
                and temp >= f["hot_c"]):
            self.set_power(gpus, f["reduced_limit_w"])
            self.power_reduced = True
            log(f"FAILSAFE: iLO unreachable and GPU {temp:.0f}C, "
                f"power limit -> {f['reduced_limit_w']} W")
        elif self.power_reduced and self.ilo.failures == 0 and temp < f["hot_c"] - 8:
            self.set_power(gpus, f["normal_limit_w"])
            self.power_reduced = False
            log(f"failsafe cleared, power limit -> {f['normal_limit_w']} W")

    @staticmethod
    def set_power(gpus, watts):
        for g in gpus:
            subprocess.run(["nvidia-smi", "-i", str(g["index"]), "-pl", str(watts)],
                           capture_output=True, timeout=15)


# ---------------------------------------------------------------- commands

def cmd_run(cfg, args):
    ilo = Ilo(cfg, dry_run=args.dry_run)
    ctl = Controller(cfg, ilo)
    stopping = {"flag": False}

    def stop(*_):
        stopping["flag"] = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log(f"ilo-fan starting{' (dry run)' if args.dry_run else ''}: target "
        f"{cfg['control']['target_c']}C, fans {cfg['ilo']['fans']}, "
        f"steps {ctl.steps}")
    interval = cfg["control"]["interval_s"]
    while not stopping["flag"]:
        t0 = time.monotonic()
        try:
            ctl.tick(t0)
        except Exception as e:
            log(f"tick error: {e}")
        time.sleep(max(0.0, interval - (time.monotonic() - t0)))

    park = cfg["control"]["park_pct"]
    log(f"stopping: leaving fan floor at {park}%")
    ilo.set_floor(park)


def cmd_status(cfg, args):
    c = cfg["control"]
    gpus = read_gpus()
    busy = read_vllm_busy(cfg["anticipate"]["vllm_metrics_url"])
    for g in gpus:
        need = fan_needed(cfg, g["power_w"], c["target_c"], c["ambient_c"])
        print(f"GPU {g['index']}: {g['temp_c']:.0f}C {g['power_w']:.0f}W "
              f"(limit {g['limit_w']:.0f}W) -> model wants {need:.0f}% fans "
              f"for {c['target_c']:.0f}C")
    print(f"vLLM busy: {busy}")
    for p in (75, 150, 250):
        print(f"  model: {p}W needs {fan_needed(cfg, p, c['target_c'], c['ambient_c']):.0f}% fans")
    if cfg["ilo"]["host"]:
        print("iLO fan targets (%):", Ilo(cfg).fan_targets())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="/etc/ilo-fan/ilo-fan.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("trust")
    sub.add_parser("test")
    sub.add_parser("probe")
    sub.add_parser("status")
    s = sub.add_parser("set")
    s.add_argument("pct", type=int)
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        if args.cmd == "run" and args.dry_run:
            cfg = load_config(None)
        else:
            sys.exit(f"config not found: {args.config}")

    if args.cmd != "run" or not args.dry_run:
        if args.cmd not in ("status",) and not cfg["ilo"]["host"]:
            sys.exit("set [ilo] host in the config first")

    if args.cmd == "run":
        cmd_run(cfg, args)
    elif args.cmd == "trust":
        Ilo(cfg).trust()
        print(f"saved iLO host key to {cfg['ilo']['known_hosts']}")
    elif args.cmd == "test":
        print(Ilo(cfg).run("show /system1/fan1").strip() or "(connected, no output)")
    elif args.cmd == "probe":
        ilo = Ilo(cfg)
        print("before:", ilo.fan_targets())
        print("setting fan 0 (Fan 1) floor to 20% ...")
        ilo.set_floor(20, fans=[0])
        time.sleep(20)
        after = ilo.fan_targets()
        print("after: ", after)
        ilo.set_floor(0, fans=[0])
        ok = (after.get(0) or 0) >= 19
        print("PATCH WORKS: Fan 1 followed the floor" if ok else
              "Fan 1 did not reach 20%: patch inactive, or that fan was already "
              "higher (check 'before')")
    elif args.cmd == "status":
        cmd_status(cfg, args)
    elif args.cmd == "set":
        ok = Ilo(cfg).set_floor(args.pct)
        print("sent" if ok else "failed")


if __name__ == "__main__":
    main()
