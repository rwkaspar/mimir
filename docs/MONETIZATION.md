# mimir — Monetization & Licensing Strategy

> **Status:** Draft / assumptions to validate (last updated 2026-11-03).
> Prices are starting hypotheses, not fixed. This is a living document.

## TL;DR

Open-source core (AGPL-3.0 + CLA). **Don't** rely on the Immich-style "pay to
support" honour license as the main revenue — mimir's audience is more
consumer than self-hoster, so that converts poorly. Instead:

1. **Lead with hardware/convenience** (pre-flashed SD cards, assembled frames) —
   immediate revenue, low infrastructure.
2. **Recurring revenue later via "mimir Connect"** — an optional hosted service
   for remote photo-sending / remote access. Self-hosters keep doing it free.
3. Keep an optional supporter license on top for goodwill.

**Where to draw the line:** never gate *local* features. Cut at
**hardware/convenience** and **hosted service**, not at software capability.

## 1. License: AGPL-3.0 + CLA

| Building block | What it does | Why |
|---|---|---|
| **AGPL-3.0** | Anyone may use/modify/redistribute, **but must publish changes** — even when offering mimir only as a network service | Stops anyone from reselling mimir closed-source as a cloud frame. Strongest protection for the open-source promise |
| **CLA** (Contributor License Agreement) | Contributors assign rights to the mimir entity | Keeps the right to **relicense commercially / for OEM** later (e.g. a hardware partner who won't accept AGPL) |

**Trade-off:** some FOSS contributors dislike CLAs. The milder option is **DCO
only** (origin attestation) — but then no later relicensing is possible. We
choose **CLA** to keep OEM/commercial options open. See [`CLA.md`](../CLA.md).

## 2. Free (self-hosted) vs. mimir Connect (hosted)

**Rule: nothing local is gated.** Everything that runs on the Pi in the home
network is free. Payment is only for what needs *infrastructure we run*.

| Free — self-hosted (AGPL) | mimir Connect — paid (subscription) |
|---|---|
| Slideshow, orientation, sleep schedule, brightness | **Send photos to the frame from anywhere** (no VPN / port-forward) |
| All self-hosted sources (Immich, Synology, Nextcloud, iCloud/Google share links) | **"Magic link" for grandma** — send photos without an account |
| Local albums, QR send **on the LAN** | **Remote management** of multiple frames (household dashboard) |
| Home Assistant / MQTT | Optional **encrypted cloud backup** of the frame's library |
| Full web UI on the home network | Email-to-frame, push notifications |

The remote access we removed Tailscale for becomes the **paid convenience**
here; self-hosters still DIY it for free.

## 3. Products & rough pricing (EUR)

| Product | Price (start) | Revenue type | Effort/infra |
|---|---|---|---|
| **mimir OS image** (download) | **€0** | — (reach/trust) | low |
| **Pre-flashed SD card** + quick-start guide | **€19–29** | one-off | low |
| **Assembled frame** (Pi + display + case) | **€149–249** (by size) | one-off, **main revenue** | medium (fulfilment) |
| **mimir Connect** | Free tier (1 frame) · **Plus €3–5/mo** or **€29–39/yr** per household | **recurring** | **lower than assumed — see §5** |
| **Supporter license** (software-only) | one-off **€25–49**, optional, **no paywall** | goodwill | low |

## 4. Rollout order

1. **Now:** set AGPL-3.0 + CLA, clean repo, finish the **ready-to-flash image**
   → trust + reach. Cheap.
2. **Early & profitable:** sell pre-flashed cards / assembled frames →
   **immediate revenue, little infra**. Carries the project while the user base
   is small.
3. **Then:** build mimir Connect → **recurring revenue**. Feasible earlier than
   first thought because infrastructure already exists (see §5).

## 5. mimir Connect — infrastructure

We already run servers, and the domain can live under **neotactiq**
(e.g. `connect.neotactiq.ai`). That removes the biggest objection to building
Connect (standing up and paying for new infra), so it can move **earlier** in
the roadmap than a from-scratch service would.

- **Hosting:** reuse existing neotactiq servers.
- **Domain:** `connect.neotactiq.ai` (or `mimir.neotactiq.ai`).
- **Minimum viable Connect:** a relay that lets a phone send a photo to a frame
  that's behind NAT — frame holds an outbound connection to the relay; no
  port-forwarding on the user's side. Start with "send a photo", add
  dashboard/backup later.
- **Cost control:** relay only (no media stored) for the free tier; charge for
  stored cloud backup, which is the part with real ongoing cost.

## 6. Open questions to validate

- Will target buyers pay for an assembled frame vs. buying a cheap commercial
  cloud frame? (Positioning: privacy, no subscription, your photos stay yours.)
- Price elasticity on the assembled frame (€149 vs €249).
- Connect: is "send from anywhere" alone worth €3–5/mo, or does it need cloud
  backup to justify a subscription?
- AGPL + CLA: acceptable to the contributors we want, or does it deter them?
