# Architecture deep dive

How the WebRTC → human handoff works under the hood: room creation, agent dispatch, the
additive SIP bridge, audio mixing, and the auto-mute.

## 1. The room exists before the handoff

There is **one room** for the whole interaction, and it is created the instant the customer
joins — not by the agent, and not again at transfer time.

```
t0  Customer clicks "start" in the browser
       → frontend POST /api/token  → returns a JWT
t1  Browser connects to LiveKit with that JWT (roomJoin grant)
       → LiveKit AUTO-CREATES the room (first participant materialises it)
t2  The JWT carries RoomConfiguration { agents: [{ agent_name: "handoff-agent" }] }
       → LiveKit dispatches the Python worker into THIS room
   Room participants: [ customer (WebRTC) , AI agent ]
```

The token is minted in `frontend/app/api/token/route.ts`; the dispatch config is built in
`frontend/lib/utils.ts` from `AGENT_NAME`. The matching Python side is
`WorkerOptions(agent_name="handoff-agent")` in `agent.py`. **The two names must match** or
the agent is never dispatched.

### Explicit vs automatic dispatch
- **Explicit** (this project): token embeds `RoomAgentDispatch(agent_name=...)`. Only that
  named agent joins, and only for rooms whose token requests it. Set via `AGENT_NAME`.
- **Automatic**: an agent worker started *without* `agent_name` joins *every* new room. We
  don't use this — explicit keeps the worker scoped.

## 2. The conversation

`AgentSession` wires the realtime pipeline:

```python
AgentSession(
    stt = deepgram.STT(model="nova-3", language="multi"),
    llm = openai.LLM(model="gpt-4o-mini"),
    tts = openai.TTS(...) | cartesia.TTS(...),
    tools = fnc.flatten(),         # exposes connect_to_human_agent to the LLM
)
```

`RoomInputOptions(noise_cancellation=BVC(), close_on_disconnect=False)`:
- **`BVC()`** is the WebRTC-grade noise filter. (Telephony agents use `BVCTelephony()`;
  here the customer is on WebRTC, so plain `BVC`.)
- **`close_on_disconnect=False`** is essential: by default LiveKit closes the room when the
  agent leaves. For a warm handoff we may *want* the agent to leave while the
  customer↔human call continues — so we keep the room alive.

## 3. The additive bridge (the core idea)

When the LLM calls `connect_to_human_agent`, the tool runs `create_sip_participant`
against the **same** room:

```python
await ctx.api.sip.create_sip_participant(
    api.CreateSIPParticipantRequest(
        room_name        = ctx.room.name,        # ← the EXISTING room
        sip_trunk_id     = OUTBOUND_TRUNK_ID,    # Vobiz outbound trunk
        sip_call_to      = dest,                 # +E.164 human number
        participant_identity = f"human_{dest}",
        wait_until_answered  = True,             # block until pickup
        headers          = {... X-VH-* ...},     # brief the receiver
        include_headers  = sip_protocol.SIP_X_HEADERS,
    )
)
```

What LiveKit does:
1. The **SIP service** places an outbound `INVITE` to the Vobiz trunk for `dest`.
2. Vobiz rings the destination. `wait_until_answered=True` makes the call block until the
   human answers (or it errors/times out).
3. On answer, LiveKit attaches that call as a **new participant** in the room.

Room participants now: `[ customer (WebRTC) , AI agent , human (SIP) ]`.

### Why audio "just works"
LiveKit is a **selective forwarding unit (SFU)**. Every participant's published audio track
is forwarded to every other subscriber, and the client mixes what it receives. So:
- the customer hears `AI + human`,
- the human hears `AI + customer`,
- no manual bridging, transcoding, or conference server is needed.

The WebRTC↔telephony sample-rate/codec difference (48 kHz Opus vs 8 kHz PCMU) is handled by
the LiveKit SIP gateway transcoding at the edge.

## 4. Auto-mute (so the AI doesn't talk over the human)

A prompt like "stay silent after the human joins" is **not reliable** — the LLM still
responds to speech. We enforce silence at the I/O layer instead:

```python
self.session.interrupt()                      # cut any in-flight TTS
self.session.output.set_audio_enabled(False)  # stop PUBLISHING agent audio (can't speak)
self.session.input.set_audio_enabled(False)   # stop STT intake (no turns → no replies)
```

- Disabling **output** removes the agent's audio track from the mix → it is inaudible.
- Disabling **input** stops speech-to-text, so the LLM is never triggered again → it
  generates nothing. This also drops STT/LLM/TTS CPU load, which helps overall call audio.

The agent remains a (silent) participant — useful if you later want to un-mute it to
re-engage. If you'd rather it leave entirely, set `STEP_AWAY=true`:

```python
self.ctx.shutdown()   # agent disconnects; room stays alive (close_on_disconnect=False)
```

## 5. Lifecycle summary

```
create room (customer joins)
   └─ dispatch agent  ── converse ──► tool: connect_to_human_agent
                                          └─ create_sip_participant (same room)
                                                └─ human answers → joins room
                                                      └─ agent mutes  (STEP_AWAY=false)
                                                         or disconnects (STEP_AWAY=true)
   customer ↔ human continue until either hangs up
```

## Design decisions & trade-offs
- **Additive vs REFER** — additive keeps the customer on WebRTC and allows 3-way (AI can
  facilitate). REFER would require the customer to be on SIP (they aren't).
- **Mute vs leave** — muting keeps a re-engage path and keeps one CDR/room; leaving frees
  the agent worker slot. Default is mute.
- **Explicit dispatch** — scopes the worker to handoff rooms and lets the token carry
  metadata if needed.
- **`wait_until_answered=True`** — simpler UX (tool returns only once connected) at the cost
  of blocking the tool call during ring; we speak a "please hold" line *before* dialing.
