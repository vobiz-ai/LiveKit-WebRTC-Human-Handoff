# SIP headers: briefing the human on transfer

When the agent dials a human into the room, it attaches **custom SIP headers** to the
outbound `INVITE`. These carry context — who the AI is, which room, which customer, and a
one-line reason — so the receiving system can **screen-pop** the human before they answer.

This doc covers exactly how those headers are **sent**, how each value is **populated**, and
how they're **read** on the far side.

---

## 1. Where they're set (the send side)

In `agent.py`, inside `connect_to_human_agent`:

```python
def _clean(v: str, n: int = 200) -> str:
    # single-line, printable-only, truncated — SIP header values must be short & safe
    return "".join(c for c in v if c.isprintable())[:n]

customer = next((p.identity for p in self.ctx.room.remote_participants.values()), "unknown")

headers = {
    "X-VH-Assistant":  "Vobiz-AI-Assistant",     # static: which AI handed off
    "X-VH-Source":     "webrtc-handoff",          # static: channel/flow id
    "X-VH-Room":       _clean(room_name, 64),     # dynamic: LiveKit room name
    "X-VH-Customer":   _clean(customer, 64),      # dynamic: customer participant identity
}
if department:
    headers["X-VH-Department"] = _clean(department, 32)   # from the tool arg
if reason:
    headers["X-VH-Reason"]     = _clean(reason)           # LLM-written one-line brief

await self.ctx.api.sip.create_sip_participant(
    api.CreateSIPParticipantRequest(
        room_name        = room_name,
        sip_trunk_id     = OUTBOUND_TRUNK_ID,
        sip_call_to      = dest,
        participant_identity = human_identity,
        wait_until_answered  = True,
        headers          = headers,                       # ← attached to the INVITE
        include_headers  = sip_protocol.SIP_X_HEADERS,    # ← see §4
    )
)
```

- **`headers=`** is a `dict[str, str]`. LiveKit's SIP service writes each entry as a SIP
  header on the outbound `INVITE` to the Vobiz trunk.
- The values are sanitized by `_clean()` so a stray newline or non-printable char can't
  corrupt the SIP message.

---

## 2. How each value is populated

| Header | Source | Example |
|---|---|---|
| `X-VH-Assistant` | static string in code | `Vobiz-AI-Assistant` |
| `X-VH-Source` | static string in code | `webrtc-handoff` |
| `X-VH-Room` | `ctx.room.name` (the live LiveKit room) | `voice_assistant_room_6527` |
| `X-VH-Customer` | first remote participant's `identity` | `voice_assistant_user_8421` |
| `X-VH-Department` | tool arg `department` (LLM-supplied) | `billing` |
| `X-VH-Reason` | tool arg `reason` (LLM-supplied summary) | `Customer disputes a duplicate charge on invoice 4471` |

`X-VH-Reason` is the important one: the tool's function description instructs the LLM to
pass a short brief, so the human gets context **without** the customer re-explaining.

> Want richer/structured data (full transcript, account id, cart contents)? Don't stuff it
> into headers. Send it to your own backend keyed by `X-VH-Room`, and have the agent screen
> fetch by room name. Headers are a *pointer + summary*, not a payload.

---

## 3. The `X-VH-` prefix rule (Vobiz-specific)

**Vobiz forwards only headers whose name starts with `X-VH-`. Everything else is dropped.**

```
X-VH-Reason: ...     ✅ forwarded to the destination
X-Reason:    ...     ❌ dropped by Vobiz
Reason:      ...     ❌ dropped by Vobiz
```

This matches the warning in the sibling `LiveKit-Vobiz-Outbound/make_call.py`:
> `header KEY does not start with 'X-VH-' — Vobiz will drop it`

So always name custom headers `X-VH-<Something>`.

### Value constraints
- **Single line, ASCII/printable.** No CR/LF (`_clean` strips non-printables).
- **Short.** Keep well under a few hundred bytes; we cap at 200 (64 for ids). Oversized
  headers can be truncated or rejected by intermediaries.

---

## 4. `include_headers=SIP_X_HEADERS` (the receive side, inbound mapping)

`include_headers=sip_protocol.SIP_X_HEADERS` tells the LiveKit SIP service to **map SIP
`X-*` headers it sees into LiveKit participant attributes**. It's the symmetric/inbound
counterpart to the outbound `headers=` dict:

- **Outbound** (this app dialing the human): `headers=` writes `X-VH-*` onto the INVITE.
- **Inbound** (a SIP call arriving into LiveKit): `SIP_X_HEADERS` copies incoming `X-*`
  headers onto the new participant so your agent can read them via
  `participant.attributes`.

Keeping it set means that if you later run the **inbound** direction (e.g. Ameyo forwards a
call *into* LiveKit), any `X-VH-*` headers Ameyo sends are readable on the participant.

---

## 5. Who actually reads the headers

| Destination | Sees `X-VH-*`? | Notes |
|---|---|---|
| **Ameyo / SIP softphone / PBX** | ✅ yes | Reads SIP headers; can screen-pop the human agent with room, customer, reason |
| **Plain mobile / PSTN phone** | ❌ no | A handset has no way to display SIP headers — the call still connects, headers just aren't surfaced |
| **Your own SIP receiver** | ✅ yes | Parse the INVITE headers in your SIP stack |

For the **production Ameyo flow**, this is the screen-pop channel: Ameyo receives the call,
reads `X-VH-Room` / `X-VH-Customer` / `X-VH-Reason`, and shows the human agent who's calling
and why before they pick up.

---

## 6. Verifying it works

1. **Agent log** — every dial prints the exact dict:
   ```
   [handoff] dialing +9199... INTO room voice_assistant_room_6527 (additive, no REFER) with headers={'X-VH-Assistant': 'Vobiz-AI-Assistant', ...}
   ```
2. **Vobiz trunk webhook** — the `CallInitiated`/`Hangup` events confirm the call leg (see
   [`WEBHOOKS.md`](WEBHOOKS.md)).
3. **SIP capture** — on a SIP endpoint you control, capture the INVITE (e.g. `tcpdump` /
   the `.pcap` approach in `LiveKit-Vobiz-Outbound`) and confirm the `X-VH-*` lines.

---

## 7. Adding your own header

```python
headers["X-VH-AccountId"] = _clean(account_id, 64)
headers["X-VH-Priority"]  = "high"
```
Rules: prefix `X-VH-`, keep it short/single-line, and remember a plain phone won't show it —
it only matters to a SIP-aware receiver like Ameyo.
