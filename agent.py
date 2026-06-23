"""
WebRTC customer  ->  AI agent  ->  warm handoff to a HUMAN  (additive, NOT SIP REFER).

Flow:
  1. Customer opens the React frontend and joins a LiveKit room over WebRTC.
  2. The join token embeds RoomAgentDispatch(agent_name="handoff-agent"), so this
     Python worker is auto-dispatched into that room.
  3. Customer talks to the AI. When they ask for a human, the AI calls
     connect_to_human_agent, which DIALS a human's phone INTO THE SAME ROOM using
     create_sip_participant.

Why not SIP REFER?
  The customer leg is WebRTC -- there is no SIP dialog to REFER. So we bridge by
  ADDING a SIP participant. LiveKit mixes every participant's audio, so the WebRTC
  customer and the phone human hear each other directly. The AI can stay (warm) or
  step away (STEP_AWAY=true); the room survives because close_on_disconnect=False.

Run:  python agent.py dev
"""

import logging
import os
from typing import Optional

from dotenv import load_dotenv

from livekit import agents, api
from livekit.protocol import sip as sip_protocol
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.agents import llm
from livekit.plugins import openai, cartesia, deepgram, noise_cancellation

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webrtc-handoff")

AGENT_NAME = "handoff-agent"  # must match AGENT_NAME in frontend/.env.local
OUTBOUND_TRUNK_ID = os.getenv("OUTBOUND_TRUNK_ID")
STEP_AWAY = os.getenv("STEP_AWAY", "false").lower() == "true"


def _build_tts():
    if os.getenv("TTS_PROVIDER", "openai").lower() == "cartesia":
        return cartesia.TTS(
            model=os.getenv("CARTESIA_TTS_MODEL", "sonic-2"),
            voice=os.getenv("CARTESIA_TTS_VOICE", "f786b574-daa5-4673-aa0c-cbe3e8534c02"),
        )
    return openai.TTS(
        model=os.getenv("OPENAI_TTS_MODEL", "tts-1"),
        voice=os.getenv("OPENAI_TTS_VOICE", "alloy"),
    )


class HandoffFunctions(llm.ToolContext):
    """Holds room/session so the tool can dial a human into the live room."""

    def __init__(self, ctx: agents.JobContext):
        super().__init__(tools=[])
        self.ctx = ctx
        self.session: Optional[AgentSession] = None  # set after the session exists

    @llm.function_tool(
        description="Connect/transfer the customer to a human agent. Call this whenever the "
        "customer asks to talk to a human, a person, or a live/real agent, or asks to be "
        "transferred to a specific number. "
        "Pass `to_number` in international format (e.g. +9199...) to dial a specific number, "
        "OR `department` (sales / billing / support). If neither is given, the default line "
        "is used. Also pass `reason` with a short summary of the customer's issue and any key "
        "details so the human is briefed before they pick up."
    )
    async def connect_to_human_agent(
        self,
        to_number: Optional[str] = None,
        department: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        # --- resolve destination: explicit number > department > default ---
        if to_number:
            dest = to_number.strip().replace(" ", "")
            if not dest.startswith("+"):
                return "I need the number in international format, e.g. +9199xxxxxxxx."
        elif department:
            dest = os.getenv(f"TRANSFER_{department.upper()}") or os.getenv("DEFAULT_TRANSFER_NUMBER")
        else:
            dest = os.getenv("DEFAULT_TRANSFER_NUMBER")
        if not dest:
            return "Sorry, no human agent line is configured right now."
        if not OUTBOUND_TRUNK_ID:
            return "Sorry, the telephony trunk is not configured."

        room_name = self.ctx.room.name
        human_identity = f"human_{dest.replace('+', '')}"

        # --- build SIP headers to brief the receiving system (screen-pop / routing) ---
        # NOTE: Vobiz only forwards headers prefixed with "X-VH-"; others are dropped.
        # Keep values short, single-line, ASCII. A plain mobile won't show these — they're
        # for a SIP endpoint like Ameyo to read and display to the human agent.
        def _clean(v: str, n: int = 200) -> str:
            return "".join(c for c in v if c.isprintable())[:n]

        customer = next(
            (p.identity for p in self.ctx.room.remote_participants.values()), "unknown"
        )
        headers = {
            "X-VH-Assistant": "Vobiz-AI-Assistant",
            "X-VH-Source": "webrtc-handoff",
            "X-VH-Room": _clean(room_name, 64),
            "X-VH-Customer": _clean(customer, 64),
        }
        if department:
            headers["X-VH-Department"] = _clean(department, 32)
        if reason:
            headers["X-VH-Reason"] = _clean(reason)

        logger.info(
            f"[handoff] dialing {dest} INTO room {room_name} (additive, no REFER) "
            f"with headers={headers}"
        )

        # Speak BEFORE the blocking dial -- ringing can take several seconds.
        if self.session:
            await self.session.generate_reply(
                instructions="Tell the customer you're connecting them to a human agent "
                "now and to please hold for just a moment."
            )

        try:
            await self.ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=room_name,
                    sip_trunk_id=OUTBOUND_TRUNK_ID,
                    sip_call_to=dest,
                    participant_identity=human_identity,
                    participant_name="Human Agent",
                    wait_until_answered=True,  # block until the human picks up
                    headers=headers,  # custom X-VH-* headers on the outbound INVITE
                    include_headers=sip_protocol.SIP_X_HEADERS,
                )
            )
        except Exception as e:
            logger.error(f"[handoff] failed to connect human: {e}")
            return f"I couldn't reach a human agent right now ({e})."

        logger.info("[handoff] human answered -- now in the room with the customer.")

        if STEP_AWAY and self.session:
            self.ctx.shutdown()  # agent leaves entirely; room stays alive
            logger.info("[handoff] STEP_AWAY=true -> agent disconnected; room stays alive.")
            return "Human connected. Stepping away."

        # HARD-MUTE the agent (prompts alone don't reliably silence the LLM).
        # Cut any current speech, stop publishing TTS audio, and stop listening/STT
        # so the customer <-> human talk directly and the agent adds no audio/CPU load.
        if self.session:
            try:
                self.session.interrupt()
            except Exception as e:
                logger.debug(f"[handoff] interrupt noop: {e}")
            self.session.output.set_audio_enabled(False)  # agent stops speaking
            self.session.input.set_audio_enabled(False)   # agent stops hearing -> no more replies
            logger.info("[handoff] agent hard-muted (audio in/out disabled).")

        return "The human agent is now connected and in the call."


class HandoffAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="""
            You are a friendly Vobiz voice assistant on a web (WebRTC) call with a customer.
            Keep replies short and natural.
            If the customer asks to speak to a human, a person, or a live agent, immediately
            call the connect_to_human_agent tool. Do not ask for a phone number.
            Once a human has joined, stay silent unless you are directly asked something.
            """
        )


async def entrypoint(ctx: agents.JobContext):
    logger.info(f"[handoff] agent joining WebRTC room: {ctx.room.name}")

    fnc = HandoffFunctions(ctx)

    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=openai.LLM(model=os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")),
        tts=_build_tts(),
        tools=fnc.flatten(),
    )
    fnc.session = session  # give the tool a handle to speak / shut down

    await session.start(
        room=ctx.room,
        agent=HandoffAssistant(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),  # BVC for WebRTC (not telephony)
            # Keep room alive if the agent leaves, so a warm handoff (customer<->human) survives.
            close_on_disconnect=False,
        ),
    )

    await session.generate_reply(
        instructions="Greet the customer warmly, say you're the Vobiz assistant, and ask how you can help."
    )


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=AGENT_NAME,
        )
    )
