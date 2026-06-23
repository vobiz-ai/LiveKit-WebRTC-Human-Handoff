# Vobiz trunk webhooks (call events + quality)

The SIP headers brief the **human** (agent → Ameyo). Webhooks are the opposite direction:
**Vobiz → your server**, notifying you about each call leg. They're how you log handoffs and
**measure call quality** (MOS / jitter) — directly useful for answering "was the audio
actually bad, or did it just feel bad?".

> Webhooks are **informational and one-way**. Your response cannot accept/reject or reroute
> a call — that's what SIP headers + routing are for. See [`SIP-HEADERS.md`](SIP-HEADERS.md).

---

## Configuration

Per **outbound trunk**, in **[Vobiz Console → SIP → Outbound Trunks](https://console.vobiz.ai/app/sip/out/trunks)**
(or via the trunk API):

| Field | Notes |
|---|---|
| `webhook_url` | Public HTTPS endpoint (≤500 chars). **localhost & private IPs are blocked** (SSRF protection) — use a tunnel (ngrok/cloudflared) in dev. |
| `webhook_method` | `POST` (default) or `GET` |

Because the URL must be public, **verify every request** (see §HMAC below).

---

## Events

### `CallInitiated` — fired on every outbound attempt (admit or reject)
Use for real-time monitoring and **rejection alerting** (insufficient balance, CLI
validation, KYC/Aadhaar pending, rate/concurrency limit, no routes).

```json
{
  "Event": "CallInitiated",
  "CallUUID": "aabbccdd-...",
  "From": "+919876543210",
  "To": "+918012345678",
  "Direction": "outbound",
  "Status": "initiated",
  "TrunkID": "...",
  "Allowed": true,
  "Reason": ""
}
```
Rejected looks the same with `"Allowed": false, "Reason": "Insufficient balance: 0.50 INR"`.

### `Hangup` — fired when a call ends (the quality goldmine)

```json
{
  "Event": "Hangup",
  "CallUUID": "aabbccdd-...",
  "To": "+918012345678",
  "Status": "completed",
  "Reason": "NORMAL_CLEARING",
  "Duration": 300,     // ring + talk, seconds
  "Billsec": 295,      // connected seconds (billable)
  "RingTime": 5,       // seconds before answer
  "Cost": 1.50,
  "Currency": "INR",
  "MOS": 4.2,          // ★ voice quality 1.0–5.0 (higher is better)
  "Jitter": 15         // ★ network jitter ms (lower is better)
}
```

**MOS / Jitter** quantify the human leg's quality:

| MOS | Perception |
|---|---|
| 4.3–5.0 | excellent |
| 4.0–4.3 | good (toll quality) |
| 3.6–4.0 | fair |
| < 3.6 | users notice problems |

If a handoff "felt choppy," read the `Hangup` MOS/jitter for that `CallUUID`. **Low MOS /
high jitter → it's the trunk/network path** (carrier, codec, congestion), *not* the agent.
A healthy MOS means look elsewhere (browser mic, local CPU, the agent not yet muted).

---

## Request headers Vobiz sends

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `User-Agent` | `Vobiz-Vapor/1.0` |
| `X-Vobiz-Event` | `CallInitiated` or `Hangup` |
| `X-Vobiz-Request-ID` | unique id — use it to de-dupe retries and tie the two events of one call |

Timeouts: CallInitiated 10s, Hangup→Vapor 5s, Vapor→your endpoint 10s. Delivery is
**async** and **fail-open** — a slow/erroring webhook never affects the call.

---

## Verifying the payload (HMAC-SHA256)

Compute HMAC-SHA256 over the **raw** body with your trunk's signing secret and compare in
constant time against the signature header. **Confirm the exact header name + secret in the
Console** before enforcing.

```python
import hmac, hashlib
from flask import Flask, request, abort

app = Flask(__name__)
SIGNING_SECRET = "your-trunk-signing-secret"   # from the Console

def verify(raw: bytes, sig: str) -> bool:
    expected = hmac.new(SIGNING_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig or "")

@app.post("/vobiz/trunk-webhook")
def handle():
    raw = request.get_data()                       # raw bytes, BEFORE json parsing
    if not verify(raw, request.headers.get("X-Vobiz-Signature", "")):
        abort(401)
    e = request.get_json()
    if e["Event"] == "Hangup":
        app.logger.info("call %s  MOS=%s jitter=%sms  cost=%s%s",
                        e["CallUUID"], e.get("MOS"), e.get("Jitter"),
                        e.get("Cost"), e.get("Currency"))
    return "", 200
```

Always hash the **raw** body (not re-serialized JSON), keep the compare constant-time, and
de-dupe on `X-Vobiz-Request-ID`.

---

## How this fits the handoff

```
agent dials human ──► Vobiz ──► CallInitiated webhook  (Allowed? balance/KYC/limits)
                                     │
                          human answers / talks
                                     │
        call ends   ──────────────► Hangup webhook      (Duration, Cost, MOS, Jitter)
```

Practical wins:
- **Quality dashboards** — store MOS/jitter per transfer; alert on MOS < 3.6.
- **Failed-handoff visibility** — `CallInitiated{Allowed:false}` tells you *why* a transfer
  dial never connected, instead of a silent failure.
- **Billing/CDR** — `Cost`, `Billsec`, `RingTime` per leg.

> Dev tip: localhost is blocked, so expose your receiver with a tunnel
> (`cloudflared tunnel --url http://localhost:5005`) and paste the public URL into the trunk
> config.
