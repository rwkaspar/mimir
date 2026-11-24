# Contributing to mimir

Thanks for your interest in improving mimir! This project is open source under
the [AGPL-3.0](LICENSE).

## Contributor License Agreement

By contributing you agree to the [Contributor License Agreement](CLA.md). It
lets the project offer commercial/OEM licenses in the future while staying fully
open source today.

**How to signal agreement:**

1. Sign off your commits — `git commit -s` adds a
   `Signed-off-by: Your Name <you@example.com>` line.
2. In your first pull request, confirm: *"I have read and agree to the CLA."*

Maintainers record contributors in [`CONTRIBUTORS.md`](CONTRIBUTORS.md).

## Development

- The app targets **Raspberry Pi (Zero 2 W and newer)** on Raspberry Pi OS
  (Trixie). Develop against real Pi hardware — behaviour that depends on the
  GPU, display stack (labwc/Chromium/Wayland), DDC/CI backlight, Wi-Fi, or
  boot/Plymouth does **not** reproduce faithfully on an x86 machine or an
  emulator.
- Python backend lives in `frame/`, the viewer in `viewer/`, provisioning in
  `scripts/` and `imaging/`.
- **Deploy caveat:** the Pi runs `git reset --hard origin/main` + `git pull` on
  every boot (see `scripts/update.sh`). To test real changes on a frame, commit
  and push, then pull on the Pi — an `scp`'d file is reverted on the next
  auto-update.

## Tests

```
scripts/test.sh                    # everything, in Docker (needs only Docker)
scripts/test.sh tests/browser      # pytest arguments pass through
```

Run it before every push; CI runs the same thing. Four layers, each there
because its kind of mistake has reached a real frame:

| Layer | What it catches |
|---|---|
| `tests/test_*.py` (unit) | logic: schedule, e-ink cycle, picture selection, setup screen at every panel size |
| `tests/test_guards.py` (reads the source) | a `sudo` command without a sudoers rule in `setup_pi.sh`; state kept in `/tmp` (gone after a power cut); a page calling an address the server does not route; a text without both languages; a page script reusing a name the browser owns |
| `tests/test_http.py` (real server, as a phone on the LAN) | anything readable or writable without the access token; routes that crash; both ways of rotating rebuild the boot logo |
| `tests/browser/` (real mobile Chromium) | script errors, failed requests, pictures that do not load, raw text keys, pages wider than the screen; tapping through the pictures page |

The browser tests save a screenshot of every page to `tests/browser/shots/`
(CI: the `page-screenshots` artifact). **Look at them** — a green run says the
page works, not that it looks right.

`KNOWN_SUDO_GAPS` and `TMP_OK` in `tests/test_guards.py` are the two places
where an exception to a rule is written down with its reason. Adding a line
there is a decision, not a way to get the run green.

After deploying to a frame, run the read-only device check **on the frame**:

```
ssh frame@mimir.local python3 digital_photo_frame/scripts/frame_check.py
```

It only reads (GET requests to the local server, systemd, the journal, sudo's
rule list): right commit checked out, services restarted since the code
changed, every page answers, every picture has a thumbnail, the night schedule
is in the config, every sudo rule the code needs is installed on *this*
device, the boot logo matches the orientation, no errors in the log.

## Pull requests

- Keep changes focused; describe what and why.
- Match the existing style (no large unrelated reformatting).
- Note how you tested — ideally on a real Pi.

## Reporting issues

Open a GitHub issue with: what you expected, what happened, your Pi model and OS
version, and relevant logs (`journalctl -u photo_frame_server` /
`photo_frame_cage`).
