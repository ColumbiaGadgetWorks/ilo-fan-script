# Handoff: DL380 LLM server, 2026-11-13

What changed on the makerspace's DL380 Gen9 GPU server today, and what is still
open. No addresses or credentials are kept here; those stay in the private server notes.

## Hardware and boot

- **Two GPUs:** nvidia-smi index 0 is the V100 SXM2 on a PCIe adapter, index 1 is the
  V100 PCIe 32 GB. They sit on different CPUs, so there is no P2P between them.
  GPU0 runs on a hand-spliced EPS power cable: keep an eye on it.
- **GPU power limit removed.** The power-limit unit now only turns on persistence
  mode, so both cards run at their default 250 W. Worst-case total draw is about
  650 W, which one 800 W PSU covers.
- **Boot no longer waits for every network port:** a drop-in for
  `systemd-networkd-wait-online` adds `--any`, since only one NIC is cabled.
- **Data disks:** two 1 TB HDDs are formatted ext4 and mounted (`nofail`) at `/data1`
  and `/data2`. The model files live there (and are backed up there), no longer in
  `/tmp`.

## vLLM

- `vllm.service` runs `/home/cgw/bin/start-llm-active.sh`. That file is a symlink to
  the script for whichever model should be served. To switch models, repoint the
  symlink and run `systemctl restart vllm`.
- **Currently served:** Huihui Qwen3.6 35B-A3B abliterated Q8_0 GGUF, as
  "Qwen 3.6 35B-A3B (Q8)".
  - Pipeline-parallel across both GPUs, 262K context.
  - About 34 tok/s for a single stream.
  - Cold load from the HDD takes about 5.5 min.
- **This GGUF needs** `--kernel-config '{"sm70_gguf":{"embedding_storage":"original"}}'`.
  Its token embedding is Q8_0, and without the flag startup fails with
  "VocabParallelEmbedding has no attribute qweight".
- MTP speculative decoding does not engage with pipeline parallel
  (`speculative_config=None`).
- vLLM prints the API key in its startup arguments, so filter logs before sharing them.

## Fan control (this repo)

The patched iLO 4 firmware (ilo4_unlock 2.77) was flashed, and `ilo-fan.service` is
installed and running. Changes to the controller today:

1. **First floor is always sent after a start** (09760fd). Before this, the iLO kept
   the 45 % park floor from the last stop.
2. **Thermal model refitted from measurements.** The new `thermal_test.py` holds fixed
   fan floors under real vLLM load until the GPUs settle, fits the model and validates
   the controller.
   - Result: effective ambient 25 °C (inlet reads 19 °C; the rest is chassis preheat),
     `Rth(f) = 0.247 × (50 / f)^1.06` °C/W, 2.5 °C rms over 13 points.
   - Settled loads: prefill about 160-190 W per GPU (spikes to 230 W), decode about
     70-90 W, idle about 37 W.
3. **GPU power is smoothed** (20 s) before it reaches the model, because prefill power
   swings a lot from second to second.
4. **`busy_power_w` is now 100 W** (decode), down from 250 W. A request now sets about
   a 28 % floor instead of 100 %; heavier prompts raise it as soon as the measured
   power follows.
5. **The temperature trend push was removed.** Right after a load starts, it
   extrapolated the warm-up slope (measured while the fans were still low) and sent
   the fans to 100 %. The controller is now feedforward from smoothed power, plus a
   slow integral trim, plus the hold-before-stepping-down, plus the 84 °C emergency.
6. **Finer fan steps:** `0, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80, 100 %`.

Fan speed the model wants per GPU load, at a 70 °C target:

| Load | Power | Fan |
|---|---|---|
| Idle | under 50 W | stock |
| Decode | 100 W | about 28 % |
| Typical | 150 W | about 42 % |
| Heavy prefill | 190 W | about 52 % |

Validation of the final controller is in the next section.

### Gotchas found along the way

- A background job started from a non-interactive shell inherits SIGINT as ignored,
  so Python never sees Ctrl-C or `kill -INT`. `thermal_test.py` now handles SIGTERM
  and SIGINT itself and always starts `ilo-fan.service` again.
- `thermal_test.py` checks vLLM before it stops `ilo-fan`, so a failed start no longer
  leaves the fans uncontrolled.

## Validation (final controller, 55 min run)

| Phase | Hottest GPU (end / max) | Fan floor (mean / max) |
|---|---|---|
| 4 heavy prefill streams, ~200 W, 12 min | 65 / 70 °C | 55 / 60 % |
| 16 decode streams, ~90 W, 10 min | 59 / 59 °C | 30 / 35 % |
| 1 decode stream, ~80 W, 8 min | 56 / 59 °C | 30 / 30 % |
| Bursty chat-like load, 12 min | 41 / 57 °C | 34 / 55 % |
| Idle gaps | 35-46 °C | back to stock within ~3 min |

Never above 72 °C, no throttling. The model errs a few degrees cool at high fan
speeds (prefill settled at 65 °C, not 70 °C), which is the safe side.

## Open items

- **vLLM did not restart after an engine crash.** One pipeline worker hung mid-decode:
  the log showed "No available shared memory broadcast block" every minute, then a
  TimeoutError after about 5 min, then EngineDeadError. The API server then exits
  with status 0, so `Restart=on-failure` does not restart it. Suggest
  `Restart=always` in `vllm.service`. It was not thermal and there was no Xid error.
- Watch GPU0's spliced power cable under sustained load.
- MTP with pipeline parallel (see above).
- Once the fan controller has run under normal use for a while, consider refitting
  with `thermal_test.py calibrate` in a warmer room. The fit was done at a 19 °C inlet.
