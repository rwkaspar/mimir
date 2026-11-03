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

## Pull requests

- Keep changes focused; describe what and why.
- Match the existing style (no large unrelated reformatting).
- Note how you tested — ideally on a real Pi.

## Reporting issues

Open a GitHub issue with: what you expected, what happened, your Pi model and OS
version, and relevant logs (`journalctl -u photo_frame_server` /
`photo_frame_cage`).
