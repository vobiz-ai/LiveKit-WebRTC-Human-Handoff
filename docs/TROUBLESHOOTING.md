# Troubleshooting

Common issues with the WebRTC → human handoff, and how to fix them. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the mechanics referenced below.

---

## The agent kept talking after the human joined
**Cause:** relying on the prompt ("stay silent") — the LLM ignores it and keeps responding.
**Fix (already in `agent.py`):** hard-mute at the I/O layer the moment the human answers:
```python
self.session.interrupt()
self.session.output.set_audio_enabled(False)   # can't speak
self.session.input.set_audio_enabled(False)    # can't hear → never replies
```
If it still talks: confirm you see `agent hard-muted (audio in/out disabled)` in the log. If
not, the human leg may not have reached "answered" (the mute runs *after*
`create_sip_participant` returns) — check the dial actually connected.

---

## Bad / choppy call quality
Separate the two possible causes:
1. **Was the agent muted?** Before the mute fix, the agent's TTS track + continuous
   STT/LLM/TTS CPU load degraded the mix. Ensure the mute fires (log line above).
2. **Is it the trunk/network?** Read the **MOS / jitter** from the Vobiz `Hangup` webhook
   for that `CallUUID` (see [`WEBHOOKS.md`](WEBHOOKS.md)).
   - MOS < 3.6 / high jitter → carrier/codec/network path, not the agent.
   - MOS ≥ 4.0 → look at the browser mic, local CPU, or that the agent wasn't yet muted.

Also tell *which direction* is bad (you→human, human→you, both) and *whether it was bad
before the transfer too* (i.e. the AI's own voice was choppy) — that isolates LiveKit↔Vobiz
from a local/browser issue.

---

## Agent never joins the room (no greeting)
- **`AGENT_NAME` mismatch.** `frontend/.env.local` `AGENT_NAME` must equal
  `agent.py`'s `WorkerOptions(agent_name=...)` → both `handoff-agent`.
- **Worker not running / not registered.** You should see
  `registered worker {"agent_name": "handoff-agent", ...}` in the agent log.
- **Wrong project.** Frontend and agent must use the **same** `LIVEKIT_URL` + key/secret.
- Explicit dispatch only fires **when the room is first created** — reload the page to get a
  fresh room if you changed dispatch config.

---

## Transfer dial fails / human phone never rings
- **Trunk not configured.** `OUTBOUND_TRUNK_ID` must be a valid LiveKit **outbound** SIP
  trunk bound to Vobiz (auth username/password/domain). See `setup_trunk.py` in
  `LiveKit-Vobiz-Outbound`.
- **Number format.** `to_number` must be E.164 (`+91...`). The tool rejects non-`+` input.
- **Vobiz rejected the call.** Check the `CallInitiated` webhook for `Allowed:false` and the
  `Reason` (insufficient balance, CLI ownership, KYC/Aadhaar, rate/concurrency, no routes).
- **Caller-ID.** `VOBIZ_OUTBOUND_NUMBER` must be authorized on the trunk.

---

## `next: command not found` when starting the frontend
`npm install` hadn't finished linking `node_modules/.bin` yet. Wait for install to fully
complete, then `npm run dev`.

---

## SIP headers not visible on the far side
- **Missing `X-VH-` prefix.** Vobiz drops any header not prefixed `X-VH-`.
- **Dialing a plain mobile.** Handsets can't display SIP headers — connect to a SIP
  endpoint (Ameyo / softphone / your SIP receiver) to see them. See [`SIP-HEADERS.md`](SIP-HEADERS.md).
- **Value too long / multiline.** Keep short, single-line; the tool truncates to ≤200 chars.

---

## Customer ↔ human can't hear each other after handoff
- The agent must **not** have closed the room. Ensure
  `RoomInputOptions(close_on_disconnect=False)` — otherwise the agent leaving (or
  `STEP_AWAY=true`) tears the room down.
- Confirm the human actually became a participant (`wait_until_answered=True` returns only
  on answer; an error means they didn't join).

---

## Room closes the instant the AI leaves
You have `close_on_disconnect=True` (the default in the *outbound* sample). For handoff it
must be `False` so the customer↔human call survives the agent's departure.

---

## Useful log lines (agent)
| Log | Meaning |
|---|---|
| `registered worker {...}` | worker is up and connected to LiveKit |
| `agent joining WebRTC room: <room>` | a customer joined; agent dispatched |
| `dialing <n> INTO room <room> ... with headers={...}` | handoff started; shows exact headers |
| `human answered -- now in the room` | SIP leg connected |
| `agent hard-muted (audio in/out disabled)` | mute succeeded |
| `STEP_AWAY=true -> agent disconnected; room stays alive` | drop-off handoff |
| `failed to connect human: <err>` | dial failed — cross-check the `CallInitiated` webhook |
