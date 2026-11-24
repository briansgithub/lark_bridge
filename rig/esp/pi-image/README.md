# Rig Pi image (second SD card)

These scripts turn a **fresh, separate** SD card with Raspberry Pi OS Lite (64-bit) into the
ESP32 HIL rig Pi described in [`firmware/docs/hil-rig.md`](../../../firmware/docs/hil-rig.md) §5:

- the onboard Bluetooth becomes the **fake car** (A2DP sink plus AVRCP CT);
- the **rig lease** lets only one agent drive the hardware at a time;
- the Pi drives the **Pico fake Lark** over GPIO SWD and its control UART (§3.4);
- a read-only **self-test** reports everything as JSON.

> **Never run any of this on the product LarkBridge card.** `provision.sh` refuses to run
> when it finds overlayroot or the product install (`/usr/local/lib/rpi-lark-bridge`,
> `/var/lib/larkbridge-persist`, `bridge-*.service`, hostname `larkbridge`, and so on). It has no
> override. To gather facts from the product Pi, use only the
> [read-only checklist](#read-only-checklist-for-the-current-product-pi) below.

## Files

| File | Installed as | What it does |
|---|---|---|
| `provision.sh` | (run from the repo copy) | Idempotent provisioning; `--dry-run` shows the changes it would make |
| `lib.sh` | `/usr/local/lib/larkbridge-rig/` | Logging, `rig.conf` loading, controller lookup, product-card guard |
| `rig_lease.py` | `rig-lease` | Lease acquire, release, status and check on `/run/larkbridge-rig.lease` |
| `fake-car.sh` (+ `fake_car_agent.py`, `fake_car_status.py`) | `fake-car` | `pair-accept`, `record`, `avrcp`, `status` |
| `selftest.sh` | `rig-selftest` | Read-only health JSON |
| `pico-ctl.sh` | `pico-ctl` | Pico `flash`, `reset`, `send`, `recv`, `idle` and `status` (Stage E stubs) |
| `bt-pin.sh` | `larkbridge-rig-bt-pin.service` | Pins BlueZ to the onboard controller by address |
| `conf_edit.py` | (provision only) | Marked or idempotent edits of `config.txt`, `cmdline.txt` and `main.conf` |
| `files/` | `/etc/...` | WirePlumber and PipeWire fake-car config, sshd drop-in, udev rule, units |
| `tests/` | — | `python3 -m unittest discover -s rig/esp/pi-image/tests` (any Linux, under 5 s) |

## 1. Flash the card (control PC)

1. Use a **new, labelled** SD card ("RIG"). Take the product card out of the Pi and put it
   somewhere safe. The rig and the product use the same Pi 3B with different cards.
2. In Raspberry Pi Imager, choose **Raspberry Pi OS Lite (64-bit)**, the current Trixie-based
   release (WirePlumber 0.5 is required). Under OS customisation:
   - hostname `larkbridge-rig` (not `larkbridge`);
   - user `admin`, with any password (password login is turned off later);
   - Services: enable SSH with **public-key authentication only**, and paste the control PC's
     public key;
   - no Wi-Fi: the rig uses Ethernet.
3. Add the rig to `~/.ssh/config` on the PC (`Host larkbridge-rig`, `User admin`, its IP), as
   hil-rig.md §4 says. Set `rig_pi_host` in `rig/inventory.toml`.

## 2. Boot and provision

Boot the Pi with the RIG card on Ethernet, then copy only this directory over from the PC:

```bash
tar -C <repo> -cz rig/esp/pi-image | ssh larkbridge-rig 'rm -rf ~/pi-image && mkdir ~/pi-image && tar -xz -C ~/pi-image'
ssh larkbridge-rig
cd ~/pi-image/rig/esp/pi-image
./provision.sh --dry-run --bt-addr auto          # review; changes nothing
sudo ./provision.sh --bt-addr auto               # or --bt-addr B8:27:EB:xx:xx:xx
sudo reboot
```

`--bt-addr auto` picks the single controller that is not on USB. Unplug the USB-BT500
first, or pass the address from the checklist. Re-running `provision.sh` is safe and
keeps the pinned address. If Imager did not install your key, add `--ssh-key ~/key.pub`.
Without a key, provision refuses to turn off password login, so it cannot lock you out.

What it changes:

- apt: `bluez rfkill pipewire pipewire-bin wireplumber libspa-0.2-bluetooth dbus-user-session
  alsa-utils sox openocd gpiod python3 python3-serial python3-dbus python3-gi` (plus
  `raspi-utils` for `pinctrl` and `vcgencmd` if missing).
- `config.txt`: one marked block at the end, `[all]` / `enable_uart=1`. This puts the mini UART
  (`/dev/serial0`) on GPIO14/15 and leaves the PL011 for Bluetooth. It refuses `disable-bt` and
  `miniuart-bt`.
- `cmdline.txt`: removes `console=serial0,...`, and masks the serial getty, so the Pico's UART
  carries no console.
- sshd drop-in `/etc/ssh/sshd_config.d/10-larkbridge-rig.conf`: key authentication only, no root.
- Bluetooth: `main.conf` gets `Class = 0x000420` (car audio), `ControllerMode = bredr` and
  `Name = LarkRig Fake Car`. A udev rule de-authorizes **every USB Bluetooth interface**, so the
  BT500 never binds. `larkbridge-rig-bt-pin.service` powers off and rfkill-blocks any other
  controller and fails loudly if the pinned one is missing.
- Fake car: WirePlumber `bluez5.roles = [ a2dp_sink ]`, no HFP or HSP, SBC only, hardware
  volume off, and seat monitoring off for headless use. A PipeWire null sink
  `larkbridge_fake_car` is the recording tap, and received media is routed there, not to the
  C-Media instrument speaker. Lingering is on for the rig user.
- Lease: tmpfiles.d creates `/run/larkbridge-rig.lease`, owned by the rig user, at each boot.
- `larkbridge-rig-pico-idle.service` puts BCM25, 24, 26 and 14 back to floating inputs at boot.
- `/etc/larkbridge-rig/rig.conf` holds the pinned address, the pins, the UART and the sink name.
- hostname `larkbridge-rig`; the rig user joins `bluetooth`, `dialout`, `gpio` and `audio`.

## 3. Verify

```bash
rig-selftest | python3 -m json.tool     # exit 0 = no failed check
fake-car status                         # controller powered, tap present
rig-lease status
pico-ctl status
```

`rig-selftest` checks for the product card, the config, that the pinned controller is
present and the only one, the pin service, tools, `rp2040.cfg`, the recording tap, the UART
and console, the sshd drop-in, disk, temperature and `vcgencmd get_throttled`. It changes
nothing.

## Using it

```bash
rig-lease acquire --owner "$AGENT" --ttl 3600   # 75 = busy; same owner renews
fake-car pair-accept 120 --until-paired         # pairing gates only; trusts the new bond
fake-car record 1800 /tmp/h5.wav                # H5: 30 min of the S31 tone via the 32UE
fake-car avrcp next                             # H7: AVRCP CT -> 32UE
pico-ctl flash fake_lark.elf                    # SWD over GPIO; pins idle afterwards
pico-ctl send "PLAY tone_1k" && pico-ctl recv 2
rig-lease release --owner "$AGENT"
```

Lease rules (details in `rig_lease.py`). Every read and write is under `flock`. A lease is
stale, and is taken over with the previous holder recorded, when it is missing or corrupt,
from another boot, expired (measured on `CLOCK_BOOTTIME`, so the NTP clock step on this
RTC-less Pi cannot affect it), or when its `--pid` on this host has exited. Exit codes: 0 ok,
3 not the holder, 75 busy.

## Decisions made without asking (overnight run)

- **Target OS is Trixie and WirePlumber 0.5.** The product Pi runs Debian 13, WP 0.5.8 and
  BlueZ 5.82 (BRINGUP-REPORT). No 0.4 Lua config is shipped; provision warns on WP < 0.5.
- **The BT500 stays out through software as well as hardware.** It is de-authorized by
  interface class, not VID:PID, so any USB Bluetooth dongle is excluded. A USB port cannot
  be switched off on a Pi 3B, so "unpowered" means unplugged or never bound.
- **Received audio goes to a null sink, not to a speaker.** The default policy would play it
  out of the C-Media dongle, which is the instrument speaker in the acoustic fixture.
- **Pairing uses our own accept-all D-Bus agent** (`fake_car_agent.py`) instead of
  `bt-agent` or `bluetoothctl` piping, so its behaviour is known. It runs only during
  `pair-accept`, and marks new devices Trusted.
- **AVRCP** uses `MediaPlayer1` when BlueZ exposes a player and falls back to the deprecated
  `MediaControl1`.
- **OpenOCD does not use the RUN pin.** It resets over SWD. `pico-ctl reset` pulses RUN
  itself, and every command returns all four pins to inputs through an EXIT trap.
- **Hostname guard.** A card named `larkbridge` is refused, because that is the product's name.

## Open questions (unverified until the rig card boots)

1. **Recording clock.** The bluez stream is resampled to the null sink's clock, so `record`
   suits continuity and level, but the H5 ±500 ppm figure should come from firmware telemetry
   unless the recording is shown to keep the source clock.
2. **`busctl --json=short` shape** of `GetManagedObjects`: the parser accepts the
   `{"type","data":[...]}` envelope and plain mappings. Check one real `fake-car status`.
3. **OpenOCD as a non-root user.** It is unknown whether Debian's 0.12 `bcm2835gpio` works
   through `/dev/gpiomem` without root. If not, run `sudo pico-ctl flash` or set
   `PICO_OPENOCD_DRIVER=linuxgpiod`. The OpenOCD 0.12 `adapter gpio` syntax is assumed (R15
   proved the drivers and `rp2040.cfg`, not this exact command line).
4. **D-Bus policy.** The rig user calls BlueZ through the `bluetooth` group. Confirm that
   Debian's `bluetooth.conf` policy grants it on Trixie.
5. **Car Class of Device.** Whether the 32UE filters on CoD is unknown; `0x000420` is a guess
   at "car audio".
6. The control-UART protocol, baud rate and the Pico ELF come from Stage E
   (`rig/esp/fake-lark/`). `send` and `recv` move raw lines only.

## Read-only checklist for the current product Pi

**READ-ONLY. These commands only print. Do not run `provision.sh`, `sudo` writes,
`bluetoothctl` power, pairing or remove commands, or anything else on the product card.**
Paste the output back to the agent. Because the rig card goes in the same Pi 3B, the
onboard controller address seen here is the address the rig will use.

```bash
cat /proc/device-tree/model; echo
. /etc/os-release; echo "$PRETTY_NAME"; uname -r
bluetoothctl list                                   # controllers and addresses
btmgmt info                                         # hciN -> addr, read-only mgmt query
hciconfig -a                                        # if installed (deprecated tool)
for h in /sys/class/bluetooth/hci*; do echo "$h -> $(readlink -f "$h/device")"; done
                                                    # onboard = serial/UART path, BT500 = usb path
lsusb                                               # BT500 VID:PID
bluetoothd --version; wireplumber --version; pipewire --version
openocd --version 2>&1 | head -n 1; ls /usr/share/openocd/scripts/target/rp2040.cfg
command -v pinctrl raspi-gpio gpioget vcgencmd
grep -v '^\s*#' /boot/firmware/config.txt | grep -v '^\s*$'
cat /boot/firmware/cmdline.txt
ls -l /dev/serial* /dev/ttyS0 /dev/ttyAMA0 2>&1
vcgencmd get_throttled; vcgencmd measure_temp
```

Facts the agent needs from this output:

- the **onboard controller address** (the hci whose device path is not under `usb`), for
  `--bt-addr` and `rig_bt_addr`;
- whether `btmgmt info` and `hciconfig` exist and their output format (`bt_controllers` parses
  `btmgmt info`);
- OS and kernel, BlueZ, WirePlumber and OpenOCD versions, and whether `rp2040.cfg` exists;
- which of `pinctrl`, `raspi-gpio` and `gpioget` exist (`pico-ctl` uses the first it finds);
- where `serial0` points today, and the current `config.txt` overlays (any `disable-bt` or
  `miniuart-bt` on the product is fine there, but tells us the radio layout);
- the throttling state on the bench power supply.
