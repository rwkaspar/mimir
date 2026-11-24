# mimir on e-paper

An e-paper frame shows **one picture and then switches itself off**. The panel
keeps the image without power; a PiSugar's clock switches the Pi back on when
the next picture is due. With one picture a day a small battery lasts for weeks.

It is the same mimir — same albums, same sync, same phone setup — with a
different display and a different rhythm. No video, no backlight, no touch menu.

```
RTC alarm powers the Pi up
  → server starts, syncs the albums (if Wi-Fi is reachable)
  → next photo is fitted to the panel, dithered to its inks, drawn
  → next alarm is armed, the Pi powers off
… hours of nothing …
```

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W | any Pi with the 40-pin header works |
| SPI e-paper panel | **Waveshare** (any panel of their `waveshare_epd` library) or **Pimoroni Inky** |
| PiSugar 2 / 3 (optional) | battery **and** the RTC that wakes the Pi. Without it the frame simply stays on and redraws on schedule — fine on mains power |

## Setup

Use the **`mimir-eink`** image: SPI is on, the panel libraries for Waveshare
and Inky are included and the e-ink cycle is the display service from the
first start. The wizard only asks which panel the frame has.

> The e-ink image is built by CI but has not been run on a panel yet.

1. Flash `mimir-eink` (see the README) and boot.
2. An e-ink frame has nothing to show yet (it does not know its panel), so
   join the frame's own Wi-Fi by hand: **`mimir-Setup`**, password
   **`photoframe`**. The setup page opens; pick your home Wi-Fi.
3. Reconnect the phone to your home Wi-Fi and tap **"Einrichtung fortsetzen"**
   on the setup page (keep that page open — the link carries the frame's access
   key). After the language the wizard asks for your panel and the battery
   board (if any).
4. The frame sizes its pictures to the panel and restarts; with a battery
   board it first downloads the PiSugar service. The phone page continues by
   itself; the panel now shows the setup screen.

To have the panel show the setup screen from the very first start, name the
panel before booting: a `mimir.yaml` on the boot partition with the `eink:`
section below (the `variant` line is not needed on this image).

**Flashed the HDMI image, or installed from source?** There the wizard asks
**"Which display does your frame have?"**; choosing e-ink installs the panel
driver, turns SPI on and restarts (3–5 min, needs network).

What changes with e-ink: one still picture at a time (no videos — they are not
even synced), no clock (a date line instead), no night sleep step.

**Without the wizard** (pre-seeding, or an older install): put a file
`mimir.yaml` on the boot partition before the first boot —

```yaml
variant: eink
eink:
  driver: waveshare:epd7in3f     # your panel — see the table below
  pisugar_model: PiSugar 3       # omit if you have no PiSugar
  update_time: "06:00"           # when the daily picture changes
```

— or, on a running frame, set those keys in `config_frame.yaml` and run
`sudo scripts/switch_variant.sh eink` (back: `… lcd`). It does everything the
wizard does and reboots.

### Panels

`driver` is `waveshare:<module>`, where `<module>` is the file name of the
panel's driver in Waveshare's library (the name printed on the panel's wiki
page), or `inky` (auto-detected), or `simulator` (writes a PNG, no hardware).

| Panel | `driver` | Inks |
|---|---|---|
| Waveshare 7.3" ACeP 7-colour (F) | `waveshare:epd7in3f` | 7 colours |
| Waveshare 7.3" Spectra 6 (E) | `waveshare:epd7in3e` | 6 colours |
| Waveshare 5.65" 7-colour (F) | `waveshare:epd5in65f` | 7 colours |
| Waveshare 4.01" 7-colour (F) | `waveshare:epd4in01f` | 7 colours |
| Waveshare 7.5" V2 | `waveshare:epd7in5_V2` | black/white |
| Waveshare 7.5" B V2 | `waveshare:epd7in5b_V2` | black/white/red |
| Pimoroni Inky Impression / wHAT / pHAT | `inky` | auto |

A panel that is not listed still works: give its module name and, if it is not
black/white, `palette:` (`bw`, `bwr`, `bwy`, `7color`, `spectra6`).

## Settings (`eink:` in `config_frame.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `update_time` | `06:00` | time of the (first) picture change |
| `updates_per_day` | `1` | 1–24, spread evenly over the day |
| `show_date` | `true` | small date in a corner |
| `fit` | `cover` | `cover` fills the panel (crops), `contain` shows the whole picture |
| `saturation` / `contrast` | `1.3` / `1.1` | lift before dithering — e-paper is paler than an LCD |
| `flip` | `false` | panel mounted upside down |
| `manual_awake_minutes` | `10` | how long the frame stays reachable after you switch it on by hand |
| `remote_qr` | `true` | switched on by hand: show the QR code for the phone remote first, then the photo |
| `remote_qr_minutes` | `5` | how long that code stays when the frame is not on battery (on battery: `manual_awake_minutes`) |
| `stay_awake` | `false` | never power off (development) |
| `pisugar` | `true` | `false` ignores a PiSugar |

The first four can be changed from the phone (`/remote`), which also shows the
battery level, the last and the next picture.

## Reaching a frame that is asleep

A battery frame is off almost all day. To change settings:

- **Press the PiSugar button.** A wake that is not at the scheduled time is
  taken as "someone wants something": the frame shows the QR code for the
  phone remote and stays on Wi-Fi for `manual_awake_minutes`, then draws a new
  picture and goes back to sleep.
- **Plug it in.** On external power it never switches off.
- **No network at all?** Create an empty file `mimir-stay-awake` on the boot
  partition — the frame then stays up until you delete it.

If the frame is switched on by hand and cannot find its Wi-Fi, it shows the
"reconnect" QR code instead of a photo.

## Safety net

Powering off is only done when all of this holds: a PiSugar answers, it reports
running on battery, and it **confirmed** the wake alarm at the requested time.
Anything else — no PiSugar, unclear power state, an alarm that did not take —
and the frame stays on and says why in the log
(`journalctl -u photo_frame_eink`).

## Developing without a panel

```bash
python3 -m frame.eink_cycle config_frame.yaml --once --no-poweroff --simulate out.png
```

draws exactly what the panel would show into `out.png` (set
`eink.driver` to the panel you are targeting to get its size and inks).
`pytest tests` runs the whole cycle against a simulated panel, a fake PiSugar
and a fake server.

## Not verified on hardware yet

The cycle, the dithering and the PiSugar protocol handling are covered by tests
and the simulator, but as of this writing no physical panel or PiSugar has been
attached. Expect to tune `saturation`/`contrast` for your panel, and check the
first few days that the frame really wakes on schedule before trusting it on
battery (run it plugged in first — it then never powers off).
