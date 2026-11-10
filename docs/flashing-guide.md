# Flashing the ilo4_unlock firmware (DL380 Gen9, iLO 4 v2.77)

This walks through putting [kendallgoto/ilo4_unlock](https://github.com/kendallgoto/ilo4_unlock)
on an HPE ProLiant DL380 Gen9. It follows that project's README, with Gen9 details
and the known problems from its issue tracker added. Read its README and
[Legal section](https://github.com/kendallgoto/ilo4_unlock#legal) first.

## Before you start

**What it does:** replaces the iLO 4 firmware with HPE's own v2.77 plus a patch that
adds hidden SSH commands, including `fan`. The server's BIOS, OS and data are not
touched. Everything else in the iLO keeps working.

**The risks:**
- If the flash is interrupted or the image is corrupt, the iLO cannot recover. Its
  flash chip would have to be desoldered and reprogrammed. The server itself still
  boots and runs without an iLO; you lose remote console, power control and fan
  management (fans may run at full speed).
- Bad fan settings can overheat parts. `ilo-fan` only ever raises minimum speeds,
  which can only add cooling.
- ilo4_unlock's author tested on DL380p and DL380e Gen8. A DL380 Gen9 on 2.77 has
  been reported working ([issue #52](https://github.com/kendallgoto/ilo4_unlock/issues/52)).

**Version check:** v2.77 is the newest iLO 4 with a useful patch. 2.78 and 2.79
removed the fan tools. Check yours in the iLO web page (Information > Overview) or
from the server: `ilorest rawget /redfish/v1/Managers/1/ | grep FirmwareVersion`.
If you are on 2.77, flashing the patched 2.77 is a same-version flash. **Do not
update the iLO past 2.77 afterwards.**

**You need:**
- A Linux machine with Docker to build the image (any distro).
- Two USB sticks, or one with [Ventoy](https://www.ventoy.net/): an Ubuntu 22.04
  desktop live image, and the three flash files.
- Physical access to open the server and flip a switch on the motherboard.
- About 30 minutes of downtime.

**Secure Boot must be off** for the flasher to reach `/dev/mem`
([issue #20](https://github.com/kendallgoto/ilo4_unlock/issues/20)). Check from
the running server: `ilorest rawget /redfish/v1/Systems/1/bios/ | grep SecureBoot`.
On the CGW DL380 it was already disabled (checked 2026-11-10).

## 1. Build the patched image

On any Linux box with Docker:

```bash
git clone --recurse-submodules https://github.com/kendallgoto/ilo4_unlock.git
cd ilo4_unlock
cat patches/latest     # must print 277
docker build -o build .
ls -al build
```

The build downloads HPE's stock firmware and flasher from HPE, checks their hashes,
and patches 2.77. `build/` should contain exactly:

| File | What it is |
|---|---|
| `ilo4_250.bin` | the **patched 2.77** image, renamed so HPE's flasher accepts it |
| `flash_ilo4` | HPE's flasher (from the 2.50 package CP027911) |
| `CP027911.xml` | the flasher's component file |

If `docker build -o` complains, use `DOCKER_BUILDKIT=1 docker build -o build .` or
`docker buildx build -o build .`. Without Docker, follow the Python 2.7 steps in the
ilo4_unlock README and run `./build.sh init` then `./build.sh 277`; in that case the
image is `build/ilo4_277.bin.patched` and you rename it to `ilo4_250.bin` yourself.

Copy the three files to a USB stick (FAT32 is fine).

## 2. Prepare the server

1. Tell people the server will be down.
2. Stop the GPU services cleanly and power off:
   ```bash
   sudo systemctl stop ilo-fan vllm
   sudo poweroff
   ```
3. **Unplug both power cords** and wait 30 seconds. The iLO runs whenever the server
   has power, even when it is "off".

## 3. Enable the iLO security override (switch S1)

1. Remove the top cover.
2. Find the **System Maintenance Switch**, a block of small DIP switches on the
   motherboard. The diagram on the underside of the cover shows where it is and
   what each switch does.
3. Set **S1 to ON**. On Gen9, S1 ON disables iLO security, which lets the flasher
   write the iLO.
4. **Touch nothing else.** Other switches clear the BIOS configuration or passwords.
5. Leave the cover off for now if you like (you need S1 again in step 6), but keep
   the airflow in mind if it runs for long.

## 4. Boot Ubuntu and flash

1. Plug both power cords back in, insert the Ubuntu USB, and power on.
2. Press **F11** at the HPE splash screen for the one-time boot menu and pick the USB.
3. Choose "Try Ubuntu". Open a terminal.
4. Copy the files off the USB stick (FAT32 loses the executable bit) and flash:
   ```bash
   sudo modprobe -r hpilo
   mkdir -p /tmp/flash && cp /media/ubuntu/*/flash_ilo4 /media/ubuntu/*/CP027911.xml /media/ubuntu/*/ilo4_250.bin /tmp/flash/
   cd /tmp/flash && chmod +x flash_ilo4
   ls -l          # three files, ilo4_250.bin about 16 MB
   sudo ./flash_ilo4 --direct
   ```
   (Adjust `/media/ubuntu/*/` if the stick mounted elsewhere; `lsblk` shows it.)
5. **Do not interrupt it.** The fans go to full speed. Expect about 2 minutes of
   erasing and 1 minute of writing. A good run ends with lines like
   `Programming offset 00ffff00...ok (0)` and `Waking up iLO 4 IOP`.

If it fails **before** erasing starts (for example `Error opening /dev/mem`), nothing
was written. Stop and check Secure Boot, and that `hpilo` is unloaded. The reporter
in issue #20 fixed it by booting an Ubuntu 21.10 live image instead of 22.04.

## 5. Finish

1. Wait for the fans to spin down, then `sudo poweroff`.
2. **Unplug both power cords** again.
3. Set **S1 back to OFF**. Refit the cover.
4. Plug in and power on. The server boots its normal OS.

## 6. Check it worked

The patched `fan` command prints nothing in most SSH sessions (only the first one after
an iLO reset shows output), and the version still says 2.77. The only reliable proof is
a write: set a fan floor and watch the fan follow. With `ilo-fan` installed (see the
[README](../README.md)):

```bash
ilo_fan.py probe
```

That raises Fan 1's floor to 20 % for 20 seconds, reads it back through the iLO's
standard `show /system1/fan1`, and releases it. "PATCH WORKS" means you're done.

By hand, from any machine with an OpenSSH **older than 10** (OpenSSH 10.x cannot
negotiate with iLO 4, see issue #52):

```bash
ssh -o KexAlgorithms=+diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-rsa \
    -o PubkeyAcceptedKeyTypes=+ssh-rsa USER@ILO-IP 'fan p 0 min 51'
```

Fan 1 should move to about 20 % within a few seconds (iLO web page, Information >
System Information > Fans). Release it with `fan p 0 min 20`.

## Recovery

| Problem | Fix |
|---|---|
| Fans stuck high or odd after testing | Reset the iLO (web page: Information > Diagnostics > Reset iLO). No `fan` setting survives an iLO reset. |
| Patched iLO misbehaving, want stock back | Upload HPE's stock iLO 4 firmware in the web page (Administration > Firmware). Stock 2.77 keeps the option to re-patch later; 2.82 is the last HPE release. |
| Flash interrupted, iLO dead | The flash chip has to be desoldered and reprogrammed. The server runs without an iLO meanwhile. |

## Notes

- Fan numbers in `fan p N` are 0-based (`fan p 0` is "Fan 1") and the value is
  0-255, linear: `51` = 20 %, `128` = 50 %, `255` = 100 %.
- Send one `fan` command per SSH command. Chaining several in one line has crashed
  iLOs.
- `fan` settings are lost on every iLO reset or power loss. `ilo-fan` reapplies its
  floor every 5 minutes and on every change.
