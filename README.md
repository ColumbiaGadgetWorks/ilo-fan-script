# ilo-fan-script

GPU-aware, predictive fan control for HPE ProLiant servers with iLO 4, built for
Columbia Gadget Works' DL380 Gen9 LLM server (V100 SXM2 on a PCIe adapter, running vLLM).

HPE's iLO cannot read third-party GPU temperatures, so the server's fans ignore GPU
heat. On our DL380 Gen9 the GPU hit 82 °C and throttled itself while the fans idled
at 15-20 %. This script watches the GPU and tells the iLO to raise its fans, early.

It needs the iLO 4 v2.77 firmware patched with
**[kendallgoto/ilo4_unlock](https://github.com/kendallgoto/ilo4_unlock)**, which adds
the `fan` SSH command. All credit for the firmware work goes to that project, the
[Airbus Security Lab iLO4 toolbox](https://github.com/airbus-seclab/ilo4_toolbox) and
/u/phoenixdev. This repo only adds a controller on top.

- **[docs/flashing-guide.md](docs/flashing-guide.md)**: step-by-step flashing for a DL380 Gen9.
- **[ilo_fan.py](ilo_fan.py)**: the controller (Python 3.11+, needs `paramiko`).
- **[thermal_test.py](thermal_test.py)**: load test that measures GPU temperature
  against fan speed, fits the controller's model and checks the result.

## Without the mod: Increased Cooling

Try this first. The BIOS "Thermal Configuration" setting raises the fan floor with no
firmware changes (set it in the F9 BIOS menu, or from the running server with HPE's
`ilorest`; it applies at the next reboot). Measured on our server, same vLLM load:

| | Optimal Cooling (default) | Increased Cooling |
|---|---|---|
| GPU under load | 82 °C, held to ~75 W (throttling) | **65 °C, full 150 W** |
| GPU idle | 46 °C | 35 °C |
| Fans | ~11 % idle, 15-27 % under load | **43 % all the time** |

The catch is noise: the fans stay at 43 % even when nothing is running. The mod plus
this script gives quiet idle and strong cooling only when the GPU needs it.

## How the controller works

The iLO runs about 60 control loops, one per sensor, and each fan follows whichever
loop asks for the most. `ilo-fan` never touches those loops or any maximum. It only
raises each fan's **minimum** with `fan p N min X`, so the iLO can still spin a fan
faster on its own if a CPU or drive gets hot.

Every 2 seconds it reads each GPU with `nvidia-smi` (and, optionally, vLLM's request
count) and works out a fan floor from three parts:

1. **Feedforward from power (the prediction).** GPU power jumps within a second of a
   request starting; temperature takes about a minute to follow. The steady-state
   temperature is roughly

   `T = ambient + power × Rth(fan%)`, with `Rth(f) = 0.247 × (50 / f)^1.06` °C/W

   and `ambient` = 25 °C. That is the air the GPUs actually see: the iLO inlet read
   19 °C, the rest is heat picked up inside the chassis. The numbers come from
   `thermal_test.py` (13 settled points, both GPUs, fans 18-80 %, 70-190 W, 2.5 °C
   rms; it errs slightly hot at high fan speeds, which is the safe side). The script
   picks the fan speed whose predicted steady state stays at the target (70 °C by
   default): about 28 % for decode (100 W), 42 % at 150 W, 52 % for heavy prefill
   (190 W). Idle (35-40 W) needs nothing.

   GPU power is smoothed (`power_tau_s`, 20 s) before it goes into the model.
   Prefill power swings between ~100 and 230 W from second to second, and the
   temperature only follows the average.
2. **vLLM anticipation.** If `vllm_metrics_url` is set, a running or queued request
   counts as `busy_power_w` (100 W, the decode level) right away, before the GPU even
   ramps. That sets a ~28 % floor; heavier prompts raise it as soon as the measured
   power follows.
3. **Feedback.** A slow integral trim raises the floor if the GPU runs hotter than
   the model predicts (dust, a warm room) and lets it drift back when it doesn't.
   There is no trend term: an earlier version projected the temperature trend 30 s
   ahead, but right after a load starts the GPU warms fast because the fans were
   still low, so it kept predicting far too hot and sent the fans to 100 % for loads
   the feedforward already covered. A load step can overshoot the target by a few
   degrees for a minute; the GPU only throttles in the 80s.

The result is rounded **up** to a step (`0, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80,
100 %`, where 0 means "stock iLO curve"):

- **Up:** immediately.
- **Down:** only after `hold_s` (120 s) without high demand, then one step every 30 s.
  Chat traffic comes in bursts; this stops the fans from hunting.
- **Emergency:** at 84 °C every configured fan goes to 100 %.

When a request arrives, the floor goes to the decode level on the first check while
the GPU is still cool; a heavy prompt raises it within seconds as the measured power
follows, about a minute before the heat arrives.

### Talking to the iLO

- One SSH connection per command (chained `fan` commands have crashed iLOs), with a
  1 s gap. A full 6-fan update takes roughly 5-15 s, so the GPU is read often but the
  iLO is only contacted when the step changes.
- Uses **paramiko**, not the `ssh` binary: OpenSSH 10.x can't negotiate with iLO 4
  ([ilo4_unlock#52](https://github.com/kendallgoto/ilo4_unlock/issues/52)).
  It is pinned to paramiko 3.x: 4.0 and later dropped `diffie-hellman-group14-sha1`
  and `ssh-rsa` host keys, the only ones iLO 4 offers.
- Key-based login with a dedicated iLO user; the host key is pinned (`trust` once).
- `fan` settings vanish on any iLO reset, so the current floor is resent every 5 minutes.

### Failure behavior

| What fails | What happens |
|---|---|
| Script crashes or is killed | The last floor stays in the iLO (fails high, not low). systemd restarts it. |
| Service stopped cleanly | Floors left at `park_pct` (45 %, about Increased Cooling). |
| iLO unreachable and GPU ≥ 82 °C | GPU power limit drops to 100 W until the iLO answers and the GPU cools. |
| iLO reset | Fans fall back to stock until the next resend (≤ 5 min) or step change. |

## Install

On the GPU server, as root:

```bash
git clone https://github.com/ColumbiaGadgetWorks/ilo-fan-script.git
cd ilo-fan-script
sudo ./install.sh
```

This puts the script in `/opt/ilo-fan` (own venv with paramiko), creates
`/etc/ilo-fan/ilo-fan.toml` and a 2048-bit RSA key `/etc/ilo-fan/id_rsa`, and installs
(but does not enable) `ilo-fan.service`. Then:

1. Set `[ilo] host` in `/etc/ilo-fan/ilo-fan.toml`.
2. In the iLO web page, create a user for the script (Administration > User
   Administration) and add the printed public key to it (Administration > Security >
   Secure Shell Key > Authorize New Key).
   The user only needs the **Login** and **Configure iLO Settings** privileges to
   run `fan` commands. iLO 4 only lets an account with every privilege import an
   SSH key for itself, so if you import the key as that user, give it full rights
   for the import and remove the extras afterwards.
3. Check the connection and the patch:
   ```bash
   F=/opt/ilo-fan/venv/bin/python; S=/opt/ilo-fan/ilo_fan.py
   $F $S trust     # pin the iLO host key
   $F $S test      # SSH login works
   $F $S probe     # Fan 1 to 20 % and back: proves the patch is active
   $F $S status    # GPU readings, model output, iLO fan targets
   ```
4. `systemctl enable --now ilo-fan`, then watch it: `journalctl -fu ilo-fan`.
5. Once it has run under real load for a while, set the BIOS back to **Optimal
   Cooling** for a quiet idle (it applies at the next reboot).

**Before flashing**, you can watch what it would do without touching the iLO:

```bash
/opt/ilo-fan/venv/bin/python /opt/ilo-fan/ilo_fan.py run --dry-run
```

## Tuning

All settings are in [ilo-fan.toml.example](ilo-fan.toml.example); anything left out
uses the defaults in `ilo_fan.py`.

- **`target_c`**: lower for more cooling and noise, higher for quiet.
- **`fans`**: start with all six. If you learn which fans sit in front of the GPU's
  riser, list only those and the rest stay on the stock curve.
- **Model (`ambient_c`, `rth_ref_c_per_w`, `f_ref_pct`, `k`)**: other hardware needs
  its own numbers. With vLLM running, as root:
  ```bash
  /opt/ilo-fan/venv/bin/python /opt/ilo-fan/thermal_test.py all
  ```
  It stops `ilo-fan.service`, holds fixed fan floors (80 % down to 30 %) under heavy
  prefill and decode load until the GPUs settle, fits the model to every GPU's
  settled temperature and power, then runs the real controller with the fitted model
  through prefill, decode, single-stream and bursty load with idle gaps. It takes
  2-3 hours and starts the service again at the end (also on errors or Ctrl-C).
  Results, `fit.json` and a `summary.txt` land in `/root/thermal-test/<time>/`;
  copy the fitted numbers into `ilo-fan.toml`. `calibrate` and `validate` run the
  halves on their own; `validate --set model.k=1.1` tries other values.
- **`busy_power_w`**: the power assumed while vLLM has a request but the GPU hasn't
  ramped yet. Use the decode level (`thermal_test.py` reports it): heavy prompts are
  caught by the measured power a second later anyway, and a prefill-level number
  keeps the fans high through every short chat.
- **`hold_s`**: longer if the fans step down and up again between requests.

## Files

| File | Purpose |
|---|---|
| `ilo_fan.py` | controller and CLI (`run`, `trust`, `test`, `probe`, `status`, `set`) |
| `thermal_test.py` | load test: calibrate and fit the thermal model, validate the controller |
| `ilo-fan.toml.example` | config template |
| `ilo-fan.service` | systemd unit |
| `install.sh` | installer |
| `docs/flashing-guide.md` | how to flash the patched iLO firmware |

## References

- [kendallgoto/ilo4_unlock](https://github.com/kendallgoto/ilo4_unlock): patched iLO 4
  firmware, `fan` command reference, community scripts and issue reports
- [airbus-seclab/ilo4_toolbox](https://github.com/airbus-seclab/ilo4_toolbox): the
  iLO 4 research the patch is built on
- [/u/phoenixdev, "Silence of the fans"](https://www.reddit.com/r/homelab/comments/di3vrk/silence_of_the_fans_controlling_hp_server_fans/):
  the original fan-control discovery, and the PID table explanation in its comments

Use at your own risk. Flashing modified firmware can brick the iLO, and fan changes
can overheat hardware. See the warnings in the ilo4_unlock README.
