# Telephony setup (Vobiz SIP trunk → LiveKit)

The handoff dials a human's phone by placing an **outbound SIP call** through a LiveKit
**outbound SIP trunk** backed by your [Vobiz](https://vobiz.ai) account. This is the one
piece you must configure before transfers work — it produces the `OUTBOUND_TRUNK_ID`
(`ST_…`) and the `VOBIZ_*` values in `.env`.

> Reference implementation: **[vobiz-ai/LiveKit-Vobiz-Outbound](https://github.com/vobiz-ai/LiveKit-Vobiz-Outbound)**
> — a standalone outbound-calling agent with ready-made `setup_trunk.py` and `make_call.py`
> scripts. If you've already created a trunk there, reuse its `OUTBOUND_TRUNK_ID` and skip to
> [step 4](#4-put-the-values-in-env).

---

## What you need from each provider

| Provider | Get | Where |
|---|---|---|
| **LiveKit Cloud** | Project URL, API key, API secret | [cloud.livekit.io](https://cloud.livekit.io) → Project → Settings → Keys |
| **Vobiz** | SIP domain (`xxxx.sip.vobiz.ai`), SIP username, SIP password, a DID number (`+91…`) | [Vobiz Console](https://console.vobiz.ai) → SIP → Outbound Trunks |

Make sure the Vobiz account has **balance** and the DID/CLI is **authorized** for outbound,
or calls are rejected at admission (visible in the `CallInitiated` webhook — see
[`WEBHOOKS.md`](WEBHOOKS.md)).

---

## 1. Install the LiveKit CLI (optional but easiest)

```bash
brew install livekit-cli            # macOS;  see https://docs.livekit.io/home/cli/
lk cloud auth                       # authenticate to your LiveKit Cloud project
```
Prefer code? Skip the CLI and use the [Python API in step 3b](#3b-create-the-trunk-with-python).

---

## 2. Set credentials in your environment

The CLI and the Python scripts read these:
```bash
export LIVEKIT_URL=wss://<your-project>.livekit.cloud
export LIVEKIT_API_KEY=<key>
export LIVEKIT_API_SECRET=<secret>
```
(They're also in this project's `.env`.)

---

## 3a. Create the outbound trunk with the CLI

Create a `trunk.json` describing the Vobiz trunk:
```json
{
  "trunk": {
    "name": "Vobiz Outbound",
    "address": "<subdomain>.sip.vobiz.ai",
    "numbers": ["+91XXXXXXXXXX"],
    "auth_username": "<sip_username>",
    "auth_password": "<sip_password>"
  }
}
```
Create it and note the returned id (starts with `ST_`):
```bash
lk sip outbound create trunk.json
lk sip outbound list            # lists trunks + their ST_ ids
```

## 3b. …or create the trunk with Python

Equivalent to the CLI, using the LiveKit API (verified against `livekit-api` 1.x):
```python
import asyncio
from livekit import api

async def main():
    lk = api.LiveKitAPI()  # reads LIVEKIT_URL / API_KEY / API_SECRET from env
    trunk = api.SIPOutboundTrunkInfo(
        name="Vobiz Outbound",
        address="<subdomain>.sip.vobiz.ai",
        numbers=["+91XXXXXXXXXX"],
        auth_username="<sip_username>",
        auth_password="<sip_password>",
        transport=api.SIPTransport.SIP_TRANSPORT_UDP,
    )
    created = await lk.sip.create_sip_outbound_trunk(
        api.CreateSIPOutboundTrunkRequest(trunk=trunk)
    )
    print("OUTBOUND_TRUNK_ID =", created.sip_trunk_id)  # ST_...
    await lk.aclose()

asyncio.run(main())
```

## 3c. Already have a trunk? Update its Vobiz auth

If the trunk exists but its credentials changed, use the **`setup_trunk.py`** pattern from
[LiveKit-Vobiz-Outbound](https://github.com/vobiz-ai/LiveKit-Vobiz-Outbound/blob/main/setup_trunk.py),
which calls `update_outbound_trunk_fields(trunk_id, address=…, auth_username=…, auth_password=…, numbers=[…])`.
Copy that script next to this project, set `OUTBOUND_TRUNK_ID` + `VOBIZ_*` in `.env`, and run it:
```bash
.venv/bin/python setup_trunk.py
```

---

## 4. Put the values in `.env`

```env
OUTBOUND_TRUNK_ID=ST_xxxxxxxxxxxx        # from step 3
VOBIZ_SIP_DOMAIN=<subdomain>.sip.vobiz.ai
VOBIZ_USERNAME=<sip_username>
VOBIZ_PASSWORD=<sip_password>
VOBIZ_OUTBOUND_NUMBER=+91XXXXXXXXXX       # caller-ID shown on the human's phone
```
`agent.py` reads `OUTBOUND_TRUNK_ID` and passes it to `create_sip_participant(...)` when it
dials the human into the room.

---

## 5. Verify the trunk works (before testing the handoff)

The fastest end-to-end check is the standalone outbound agent — it uses the *same* trunk to
place a call and even demonstrates `transfer_call`:

```bash
# in a clone of LiveKit-Vobiz-Outbound
python agent.py dev                       # terminal 1: start the worker
python make_call.py --to +91XXXXXXXXXX    # terminal 2: dial your phone
```
If your phone rings and the AI talks, the trunk is good — the handoff in *this* project will
dial successfully too. If not, see [Troubleshooting](#troubleshooting).

---

## How this maps to the handoff

```
agent.py → create_sip_participant(sip_trunk_id=OUTBOUND_TRUNK_ID, sip_call_to=<human>)
              └─ LiveKit SIP service → Vobiz trunk (auth via VOBIZ_USERNAME/PASSWORD)
                    └─ rings the human → they join the SAME LiveKit room
```
Outbound trunk = the path from LiveKit to the phone network. Caller-ID is
`VOBIZ_OUTBOUND_NUMBER`. The `X-VH-*` SIP headers ([`SIP-HEADERS.md`](SIP-HEADERS.md)) ride on
the INVITE this trunk sends.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `create_sip_participant` errors / phone never rings | bad/empty `OUTBOUND_TRUNK_ID`, or trunk auth (`VOBIZ_USERNAME/PASSWORD/DOMAIN`) wrong |
| Call rejected immediately | Vobiz admission denied — check the `CallInitiated` webhook `Reason` (balance, CLI ownership, KYC, rate/concurrency). See [`WEBHOOKS.md`](WEBHOOKS.md) |
| Rings but caller-ID wrong | `VOBIZ_OUTBOUND_NUMBER` not set / not authorized on the trunk |
| Number rejected | must be E.164 with country code (`+91…`) |
| `max auth retry attempts` | trunk credentials out of date — re-run the `setup_trunk.py` update (step 3c) |

More telephony detail and the SIP transfer guide live in the
[LiveKit-Vobiz-Outbound](https://github.com/vobiz-ai/LiveKit-Vobiz-Outbound) repo
(`transfer_call.md`, packet captures, etc.).
