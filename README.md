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

   `T = ambient + power × Rth(fan%)`, with `Rth(f) = 0.307 × (43 / f)^1.16` °C/W

   fitted from our two measurements above. The script picks the fan speed whose
   predicted steady state stays at the target (70 °C by default). At 150 W that is
   about 41 %; idle (37 W) needs nothing.
2. **vLLM anticipation.** If `vllm_metrics_url` is set, a running or queued request
   counts as full power (`busy_power_w`) right away, before the GPU even ramps.
3. **Feedback.** A slow integral trim corrects the model if the GPU runs hotter than
   predicted (dust, warm room), and a proportional push acts on the temperature
   trend 30 seconds ahead.

The result is rounded **up** to a step (`0, 30, 40, 50, 65, 80, 100 %`, where 0 means
"stock iLO curve"):

- **Up:** immediately.
- **Down:** only after `hold_s` (120 s) without high demand, then one step every 30 s.
  Chat traffic comes in bursts; this stops the fans from hunting.
- **Emergency:** at 84 °C every configured fan goes to 100 %.

In a dry run on our server, a request arrived and the floor went to 50 % on the first
check while the GPU was still at 37 °C, about a minute before the heat arrived.

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
- **Model (`rth_ref_c_per_w`, `f_ref_pct`, `k`)**: other hardware needs its own
  numbers. Set `[log] csv`, run steady loads at two fan floors (`ilo_fan.py set 30`,
  later `set 60`), note GPU power and settled temperature, and solve
  `Rth = (T - ambient) / power` at each; `k = ln(Rth1/Rth2) / ln(f2/f1)`.
- **`hold_s`**: longer if the fans step down and up again between requests.

## Files

| File | Purpose |
|---|---|
| `ilo_fan.py` | controller and CLI (`run`, `trust`, `test`, `probe`, `status`, `set`) |
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
