"""
GraphSentinel Voice - Twilio/Teams Voice Alert Service
Companion service to GraphSentinel for delivering voice alerts
"""
import os
import json
import httpx
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel

app = FastAPI(
    title="GraphSentinel Voice",
    description="Voice Alert Delivery Service - Twilio & Teams Integration",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Config
ELEVENLABS_KEY = os.getenv("ELEVENLABS_KEY", "")
ELEVENLABS_VOICE = os.getenv("ELEVENLABS_VOICE", "21m00Tcm4TlvDq8ikWAM")
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
TEAMS_WEBHOOK = os.getenv("TEAMS_WEBHOOK", "")

# Audio cache
audio_cache = {}


class VoiceRequest(BaseModel):
    """Voice generation request."""
    message: str
    language: str = "de"
    threat_id: Optional[str] = None
    

class CallRequest(BaseModel):
    """Phone call request."""
    to_number: str
    message: str
    threat_id: Optional[str] = None


class TeamsRequest(BaseModel):
    """Teams message request."""
    message: str
    threat_id: Optional[str] = None
    include_audio: bool = True


@app.get("/")
async def root():
    return {
        "service": "GraphSentinel Voice",
        "status": "operational",
        "capabilities": {
            "elevenlabs": bool(ELEVENLABS_KEY),
            "twilio": bool(TWILIO_SID and TWILIO_TOKEN),
            "teams": bool(TEAMS_WEBHOOK)
        }
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.post("/api/generate")
async def generate_voice(req: VoiceRequest):
    """Generate voice audio from text via ElevenLabs."""
    if not ELEVENLABS_KEY:
        raise HTTPException(503, "ElevenLabs not configured")
    
    threat_id = req.threat_id or f"VOC-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE}",
            headers={
                "xi-api-key": ELEVENLABS_KEY,
                "Content-Type": "application/json"
            },
            json={
                "text": req.message,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75
                }
            },
            timeout=30.0
        )
        
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"ElevenLabs error: {resp.text}")
        
        audio_cache[threat_id] = resp.content
        
        return {
            "threat_id": threat_id,
            "audio_url": f"/api/audio/{threat_id}",
            "duration_estimate": len(req.message) // 15  # rough seconds
        }


@app.get("/api/audio/{threat_id}")
async def get_audio(threat_id: str):
    """Retrieve generated audio."""
    if threat_id not in audio_cache:
        raise HTTPException(404, "Audio not found")
    
    return Response(
        content=audio_cache[threat_id],
        media_type="audio/mpeg",
        headers={"Content-Disposition": f"inline; filename={threat_id}.mp3"}
    )


@app.post("/api/call")
async def make_call(req: CallRequest):
    """Initiate phone call via Twilio."""
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM]):
        raise HTTPException(503, "Twilio not configured")
    
    # First generate audio
    voice_req = VoiceRequest(message=req.message, threat_id=req.threat_id)
    voice_result = await generate_voice(voice_req)
    
    # Create TwiML that plays the audio
    # In production: host audio on public URL or use Twilio's say with SSML
    twiml = f"""
    <Response>
        <Say voice="Polly.Vicki" language="de-DE">{req.message}</Say>
    </Response>
    """
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={
                "To": req.to_number,
                "From": TWILIO_FROM,
                "Twiml": twiml
            }
        )
        
        if resp.status_code not in [200, 201]:
            raise HTTPException(resp.status_code, f"Twilio error: {resp.text}")
        
        call_data = resp.json()
        
        return {
            "status": "initiated",
            "call_sid": call_data.get("sid"),
            "to": req.to_number,
            "threat_id": voice_result["threat_id"]
        }


@app.post("/api/teams")
async def send_teams(req: TeamsRequest):
    """Send alert to Microsoft Teams channel."""
    if not TEAMS_WEBHOOK:
        raise HTTPException(503, "Teams webhook not configured")
    
    threat_id = req.threat_id or f"TMS-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    # Generate audio if requested
    audio_url = None
    if req.include_audio and ELEVENLABS_KEY:
        voice_result = await generate_voice(VoiceRequest(
            message=req.message,
            threat_id=threat_id
        ))
        # In production: upload to blob storage for public URL
        audio_url = voice_result["audio_url"]
    
    # Adaptive Card for Teams
    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "FF4444",
        "summary": "Security Alert from GraphSentinel",
        "sections": [{
            "activityTitle": "🛡️ GraphSentinel Security Alert",
            "activitySubtitle": f"Threat ID: {threat_id}",
            "facts": [
                {"name": "Time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")},
                {"name": "Status", "value": "Auto-remediated"}
            ],
            "text": req.message,
            "markdown": True
        }],
        "potentialAction": [{
            "@type": "OpenUri",
            "name": "View Dashboard",
            "targets": [{"os": "default", "uri": "https://graphsentinel.railway.app"}]
        }]
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TEAMS_WEBHOOK,
            json=card,
            timeout=10.0
        )
        
        if resp.status_code not in [200, 201]:
            raise HTTPException(resp.status_code, f"Teams error: {resp.text}")
    
    return {
        "status": "sent",
        "threat_id": threat_id,
        "channel": "teams",
        "audio_generated": audio_url is not None
    }


@app.post("/api/alert")
async def send_alert(
    message: str,
    channels: list[str] = ["teams"],
    phone: Optional[str] = None,
    threat_id: Optional[str] = None
):
    """Send alert to multiple channels."""
    results = {}
    threat_id = threat_id or f"ALT-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    if "teams" in channels and TEAMS_WEBHOOK:
        try:
            results["teams"] = await send_teams(TeamsRequest(
                message=message,
                threat_id=threat_id
            ))
        except Exception as e:
            results["teams"] = {"error": str(e)}
    
    if "call" in channels and phone and TWILIO_SID:
        try:
            results["call"] = await make_call(CallRequest(
                to_number=phone,
                message=message,
                threat_id=threat_id
            ))
        except Exception as e:
            results["call"] = {"error": str(e)}
    
    if "voice" in channels and ELEVENLABS_KEY:
        try:
            results["voice"] = await generate_voice(VoiceRequest(
                message=message,
                threat_id=threat_id
            ))
        except Exception as e:
            results["voice"] = {"error": str(e)}
    
    return {
        "threat_id": threat_id,
        "channels": results
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
