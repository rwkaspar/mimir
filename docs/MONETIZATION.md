# mimir — Monetization & Licensing Strategy

> **Status:** Draft / assumptions to validate (last updated 2026-11-03).
> Prices are starting hypotheses, not fixed. This is a living document.

## TL;DR

Open-source core (AGPL-3.0 + CLA). **The business is hardware/convenience, not
a subscription.** Remote access is too weak to charge for on its own, and the
photo-frame category punishes subscriptions (the market leader, Aura, has none
and markets "no fees" as a feature).

1. **Primary revenue: hardware & convenience** — pre-flashed SD images and
   assembled frames. This is where Aura makes its money, and mimir's
   open-source + privacy angle is the differentiator.
2. **mimir Connect is a free feature**, not a paywall — it makes the product
   good (effortless photo sending). Self-hosters run their own relay for free.
3. **Optional recurring revenue only for real-cost add-ons**, later and
   uncertain: encrypted cloud backup, or a "family plan" (many frames + lots of
   storage). Basic "send a photo to the frame" stays free.

**Positioning:** *"mimir — your photos, your frame. No subscription, no cloud
lock-in."* Aim it straight at the subscription cloud frames.

## Why not a remote-access subscription

We considered charging for remote access / "send from anywhere" and rejected it:

- **It overlaps with what's already free.** A frame pointed at a shared album
  (Immich/Synology/Google/Nextcloud) already gets new photos the family adds
  from anywhere. "Send one photo right now" is a nice-to-have, not a monthly
  need.
- **Low frequency → low willingness to pay.** People don't consciously "use
  remote access" often. The real value is passive ("the frame stays effortlessly
  fed"), which is hard to pin a per-month price on.
- **The category resists subscriptions.** Aura deliberately has no fees and
  uses that as a selling point; Nixplay/Skylight charge and draw recurring
  backlash. A subscription is a liability here, not an asset.
- **The buyer who pays is the gifter, not the self-hoster.** Self-hosters will
  never pay (they DIY). The adult child buying a frame for a parent pays for
  *zero-hassle hardware*, like they would for an Aura.

## 1. License: AGPL-3.0 + CLA

| Building block | What it does | Why |
|---|---|---|
| **AGPL-3.0** | Anyone may use/modify/redistribute, **but must publish changes** — even when offering it only as a network service | Protects the open-source core against closed-source resale. Same license on device + relay |
| **CLA** (Contributor License Agreement) | Contributors assign rights to the mimir entity | Keeps the right to **relicense commercially / for OEM** later |

See [`CLA.md`](../CLA.md). Trade-off and the DCO alternative are noted there.

## 2. Products & rough pricing (EUR)

| Product | Price (start) | Revenue type | Notes |
|---|---|---|---|
| **mimir OS image** (download) | **€0** | — (reach/trust) | the open device software |
| **Pre-flashed SD card** + quick-start | **€19–29** | one-off | pay for not-flashing |
| **Assembled frame** (Pi + display + case) | **€149–249** | one-off, **main revenue** | where Aura makes money; our privacy/open angle differentiates |
| **mimir Connect** (send photos remotely) | **€0 — free feature** | — | self-hostable relay; managed hosting on neotactiq, also free for basic send |
| **Cloud backup / Family plan** *(optional, later)* | **TBD** (~€2–4/mo or one-off) | recurring, **uncertain** | only the parts with real storage cost; validate demand first |
| **Supporter license** (software-only) | one-off **€25–49**, optional, no paywall | goodwill | for the self-hoster crowd |

## 3. mimir Connect — role & infra

Connect is a **free feature** that makes the frame effortless to feed (see
[`CONNECT.md`](CONNECT.md)). It is **not** the business model.

- **Code:** separate repo `mimir-connect` (AGPL-3.0, self-hostable). Private for
  now (pre-launch); can go public at launch to invite self-hosting.
- **Hosting:** official managed relay reuses existing neotactiq servers at
  `connect.neotactiq.ai` — offered **free for basic send**. Enthusiasts point
  their frame at their own relay via `connect.relay_url`.
- The only thing worth charging for later is **durable encrypted cloud backup**
  (real ongoing storage cost) — and only if buyers actually ask for it.

## 4. Rollout order

1. **Now:** AGPL + CLA (done), finish the **ready-to-flash image** → reach +
   trust. Cheap.
2. **Early & profitable:** sell **pre-flashed cards / assembled frames** →
   immediate revenue, little infra. This carries the project.
3. **Keep Connect free** so the product feels effortless; run the managed relay
   cheaply (relay-only, no stored media on the free path).
4. **Only if validated:** add paid cloud backup / family plan.

## 5. Open questions to validate

- Will target buyers pay €149–249 for an open, private, **no-subscription**
  frame vs. a cheap subscription cloud frame? (Positioning is the whole bet.)
- Price elasticity on the assembled frame (€149 vs €249).
- Is there *any* real demand for paid cloud backup, or is "your photos stay on
  your Pi" actually the selling point (i.e. no cloud at all)?
- AGPL + CLA: acceptable to the contributors we want?
