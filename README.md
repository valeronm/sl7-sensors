# sl7-sensors

Ambient light and color sensor support for the Surface Laptop 7 (and likely
other Snapdragon X Elite laptops) on Linux — including desktop
auto-brightness.

On these machines the ambient light sensor (an ams TCS3430 behind the
camera) is not on any host-visible bus. It belongs to the Qualcomm Sensor
Core (SSC) running on the ADSP; the DSP firmware contains the actual sensor
driver and returns calibrated lux over QMI. The catch: the SSC reads its
sensor *configuration* (bus wiring, factory calibration) from the **host
filesystem at runtime** over FastRPC. Windows provides that file service —
Linux doesn't, so the SSC boots empty and no sensors exist. No kernel
driver is missing; the fix is all userspace.

This package supplies the missing pieces:

- **sl7-sensors.service** — `adsprpcd` (built from
  [quic/fastrpc](https://github.com/quic/fastrpc), vendored submodule)
  attached to the *sensors* protection domain, serving the sensor
  configuration. Started automatically when the ADSP appears.
- **sl7-sensors setup** — one-time extraction of that configuration from
  the machine's own Windows partition (the files are Microsoft/Qualcomm
  proprietary and per-device — they cannot be redistributed).
- **sensor proxy** — a `net.hadess.SensorProxy` D-Bus service, so
  GNOME/KDE auto-brightness works exactly as if an IIO sensor existed.
  Optional `--controller` mode bypasses the desktop's controller and drives
  `/sys/class/backlight` directly with smooth 1-count ramping (see below).
- **sl7-sensors lux** / **sl7-sensors color** — CLI readers: stream lux or
  the full color/CCT output straight over QMI. No root needed.

## Install

Grab the deb from the [Releases page](https://github.com/valeronm/sl7-sensors/releases)
(CI builds and attaches it on every tag), or build it yourself with
`dpkg-buildpackage -us -uc -b`.

```sh
sudo apt install ./sl7-sensors_1.0_arm64.deb
sudo sl7-sensors setup        # finds & mounts the Windows partition itself
sl7-sensors lux               # → one lux line per second = it works
```

Requirements: a kernel with `CONFIG_QCOM_FASTRPC` and the ADSP remoteproc
running (any working audio setup on these laptops implies both), and the
Windows partition still present (or pass its mount point to the setup
script).

GNOME: Settings → Power → "Automatic Screen Brightness" appears once the
proxy is running.

## Direct backlight controller (optional)

GNOME 50's auto-brightness applies its smoothing on sparse D-Bus events and
couples the slider into a second feedback anchor; in dim rooms the result
is visibly jumpy. `--controller` mode implements the whole loop in the
daemon instead: slow/fast dual-rate filtering of the lux stream, a
`K·√lux` response curve, and exponential-approach backlight ramping whose
minimum step is a single raw backlight count (imperceptible; large changes
glide at up to 2% of range per 100 ms). The
brightness slider keeps working and *is* the calibration: set it where you
like, and "this light ↔ this brightness" is adopted and persisted as the
anchor.

Switch modes with the built-in command (it edits
`/etc/default/sl7-sensor-proxy` and restarts the service; it also prints the
matching per-user `gsettings` command to run — GNOME's own auto-brightness
must be off in controller mode and on in desktop mode):

```sh
sudo sl7-sensors mode controller   # this package drives the backlight
sudo sl7-sensors mode desktop      # back to GNOME/KDE's auto-brightness
```

The controller's calibration anchor survives in `/var/lib/sl7-sensor-proxy/`
across switches; re-enabling picks up where you left off.

Any external backlight write is treated as calibration — including GNOME's
*idle dimming*, which the controller adopts and then re-adopts when
activity restores the old level. That round trip is normally lossless, but
if the room lighting changes while the machine sits dimmed the anchor can
come back slightly off; disable idle dimming if you notice it
(`gsettings set org.gnome.settings-daemon.plugins.power idle-dim false`).
Writes of 0 (screen blanking) are never adopted.

## How it works / caveats

- The sensors-PD attach (`FASTRPC_INIT_ATTACH_SNS`) joins a PD the firmware
  already runs — nothing is created on the DSP. The root PD does **not**
  answer FastRPC on this firmware; don't point adsprpcd at it.
- The DSP registry re-reads its config on every attach, so
  `systemctl restart sl7-sensors` is the retry knob.
- Config text files must be LF: the DSP parser keeps `\r` and builds
  corrupt paths (setup handles the conversion).
- `/persist` (symlink into `/var/lib/sl7-sensors`) must exist on the rootfs
  because the registry lists its database directory via literal `opendir` —
  the only path that bypasses the FastRPC search-path mechanism.
- One attach per boot is enough; sensor streaming rides QMI and survives
  the daemon.
- First diagnostic when sensors are dead: `apt install qrtr-tools` and run
  `qrtr-lookup` — the sensor service is service **400** (`SNS_CLIENT_SVC`,
  node 5). Absent = the DSP side never came up (re-run `sl7-sensors setup`
  / `systemctl restart sl7-sensors`); present = the client side is at
  fault (`sl7-sensors status`, journal).
- GNOME's own auto-brightness maps lux to brightness linearly with **no
  minimum floor**: in a genuinely dark room it will dim the screen to
  near-black. That's upstream gsd-power behavior with any honest lux
  sensor, not specific to this package. The `--controller` mode keeps a
  2% floor (`CTRL_MIN_FRAC`) for exactly this reason.
- If GNOME auto-brightness stops reacting after the proxy restarts (e.g.
  after switching modes or upgrading), you've hit a gsd-power bug: its
  claim-tracking flag isn't reset when the sensor proxy vanishes, so it
  never re-claims the new instance. Locking and unlocking the screen heals
  it (the blank/unblank path resyncs the flag), as does
  `systemctl --user restart org.gnome.SettingsDaemon.Power.target`
  (the unit itself refuses manual restart; restarting the target works).
- Machines other than the SL7: the mechanism is generic, only the driver
  package name differs — setup searches for `*snscfg*`. Reports welcome.
- Protocol details (QMI/TLV framing, payload layouts, recovered schemas):
  see [`protocol/README.md`](protocol/README.md).

## License

MIT for everything in this repository; the vendored fastrpc submodule is
BSD-3-Clause (Qualcomm).
