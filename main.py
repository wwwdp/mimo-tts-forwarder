import os
import json
import uuid
import base64
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import httpx
import aiofiles
from fastapi import FastAPI, Request, HTTPException, Query, Form, File, UploadFile, Response
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings

# ─── Version ────────────────────────────────────────────────
VERSION = "1.0.0"

# ─── Configuration ───────────────────────────────────────────

class Settings(BaseSettings):
    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    host: str = "0.0.0.0"
    port: int = 8765
    token: str = ""  # Access token for API protection
    data_dir: str = "/app/data"
    max_audio_size_mb: int = 10
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()

# ─── Logging ─────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mimo-tts")

# ─── Paths ───────────────────────────────────────────────────

DATA_DIR = Path(settings.data_dir)
VOICES_DIR = DATA_DIR / "voices"
META_FILE = DATA_DIR / "voices_meta.json"
CACHE_DIR = DATA_DIR / "cache"

VOICES_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ─── Audio Cache (same text + voice = same audio, avoid regenerating) ───

import hashlib

def get_cache_key(text: str, voice: str, model: str) -> str:
    """Generate a cache key from text + voice + model."""
    raw = f"{model}|{voice}|{text}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def get_cached_audio(cache_key: str) -> Optional[bytes]:
    """Return cached audio bytes if available."""
    cache_path = CACHE_DIR / f"{cache_key}.mp3"
    if cache_path.exists():
        return cache_path.read_bytes()
    return None

def save_cached_audio(cache_key: str, audio_bytes: bytes):
    """Save audio to cache."""
    cache_path = CACHE_DIR / f"{cache_key}.mp3"
    try:
        cache_path.write_bytes(audio_bytes)
    except Exception as e:
        logger.warning(f"Failed to cache audio: {e}")

def cleanup_cache(max_files: int = 500):
    """Remove oldest cache files if cache exceeds max_files."""
    try:
        files = sorted(CACHE_DIR.glob("*.mp3"), key=lambda f: f.stat().st_mtime)
        if len(files) > max_files:
            for f in files[: len(files) - max_files]:
                f.unlink(missing_ok=True)
                logger.info(f"Cleaned cache: {f.name}")
    except Exception as e:
        logger.warning(f"Cache cleanup error: {e}")

# ─── Voice Metadata ──────────────────────────────────────────

def load_voices_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_voices_meta(meta: dict):
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

voices_meta = load_voices_meta()

# ─── Preset Voices (MiMo-V2.5-TTS official) ──────────────────

PRESET_VOICES = {
    "mimo_default": {"name": "MiMo 默认", "lang": "zh-CN", "gender": "female"},
    "冰糖": {"name": "冰糖（中文女声）", "lang": "zh-CN", "gender": "female"},
    "茉莉": {"name": "茉莉（中文女声）", "lang": "zh-CN", "gender": "female"},
    "苏打": {"name": "苏打（中文男声）", "lang": "zh-CN", "gender": "male"},
    "白桦": {"name": "白桦（中文男声）", "lang": "zh-CN", "gender": "male"},
    "Mia": {"name": "Mia（English Female）", "lang": "en-US", "gender": "female"},
    "Chloe": {"name": "Chloe（English Female）", "lang": "en-US", "gender": "female"},
    "Milo": {"name": "Milo（English Male）", "lang": "en-US", "gender": "male"},
    "Dean": {"name": "Dean（English Male）", "lang": "en-US", "gender": "male"},
}

# Voice aliases: ASCII names for Legado compatibility (POST form encoding issues with Chinese)
VOICE_ALIASES = {
    "bingtang": "冰糖",
    "moli": "茉莉",
    "suda": "苏打",
    "baihua": "白桦",
    "default": "mimo_default",
}

def resolve_voice(voice: str) -> str:
    """Resolve voice name, supporting aliases and cloned voices."""
    # Check alias first
    if voice in VOICE_ALIASES:
        return VOICE_ALIASES[voice]
    # Check preset
    if voice in PRESET_VOICES:
        return voice
    # Check cloned
    if voice in voices_meta:
        return voice
    # Try URL-decoding in case Chinese chars got double-encoded
    try:
        from urllib.parse import unquote
        decoded = unquote(voice)
        if decoded in PRESET_VOICES:
            return decoded
        if decoded in VOICE_ALIASES:
            return VOICE_ALIASES[decoded]
    except Exception:
        pass
    # Default fallback
    return "冰糖"

# ─── App ──────────────────────────────────────────────────────

app = FastAPI(
    title="MiMO-TTS-Forwarder",
    description="TTS Forwarder with Voice Cloning via MiMo API - Compatible with Reading Apps",
    version=VERSION,
)

# ─── Auth Middleware ──────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if settings.token:
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/v1/"):
            auth = request.headers.get("Authorization", "")
            query_token = request.query_params.get("token", "")
            if auth != f"Bearer {settings.token}" and query_token != settings.token:
                return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    response = await call_next(request)
    return response

# ─── Helper: Detect audio MIME type ──────────────────────────

def get_audio_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
    }.get(ext, "audio/mpeg")

# ─── MiMo API Client ─────────────────────────────────────────

# ─── Concurrency Control & Rate Limit ──────────────────────

# Concurrency control
# MiMo voiceclone RPM limit: 100/min → ~1.67/sec, supports multiple concurrent requests
# Set higher to allow Legado to prefetch multiple segments in parallel
API_SEMAPHORE = asyncio.Semaphore(10)  # Max 10 concurrent TTS requests

async def call_mimo_tts(
    text: str,
    voice: str = "冰糖",
    model: str = "mimo-v2.5-tts",
    output_format: str = "mp3",
    reference_audio_b64: Optional[str] = None,
    reference_audio_mime: str = "audio/mpeg",
    style_instruction: str = "",
) -> bytes:
    """
    Call MiMo TTS API and return audio bytes.
    Includes retry logic with exponential backoff for 429 rate limits.

    For mimo-v2.5-tts: voice is a preset name like 冰糖, 茉莉, etc.
    For mimo-v2.5-tts-voiceclone: voice must be "data:{MIME};base64,{BASE64_AUDIO}"
    """

    messages = []

    # For voice clone: MUST always have user message first (content="" if no style instruction)
    # For preset voice: target text goes in assistant message only
    if model == "mimo-v2.5-tts-voiceclone":
        messages.append({"role": "user", "content": style_instruction or ""})
        messages.append({"role": "assistant", "content": text})
    else:
        messages.append({"role": "assistant", "content": text})

    # Build audio config
    if model == "mimo-v2.5-tts-voiceclone" and reference_audio_b64:
        voice_value = f"data:{reference_audio_mime};base64,{reference_audio_b64}"
    else:
        voice_value = voice

    audio_config = {"format": output_format, "voice": voice_value}

    payload = {
        "model": model,
        "messages": messages,
        "audio": audio_config,
    }

    headers = {
        "Authorization": f"Bearer {settings.mimo_api_key}",
        "Content-Type": "application/json",
    }

    max_retries = 5
    base_delay = 1.0

    async with API_SEMAPHORE:
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=120.0) as http_client:
                    logger.info(f"Calling MiMo API: model={model}, voice={voice if model != 'mimo-v2.5-tts-voiceclone' else '(clone_audio)'}, text_len={len(text)}, attempt={attempt+1}")
                    resp = await http_client.post(
                        f"{settings.mimo_base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )

                    if resp.status_code == 429:
                        # Rate limited - retry with exponential backoff
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"MiMo API 429 rate limited, retrying in {delay}s (attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(delay)
                        continue

                    if resp.status_code != 200:
                        error_text = resp.text
                        logger.error(f"MiMo API error: {resp.status_code} - {error_text[:500]}")
                        raise HTTPException(status_code=resp.status_code, detail=f"MiMo API error: {error_text[:500]}")

                    data = resp.json()

                    # Extract audio from response
                    try:
                        audio_data = data["choices"][0]["message"]["audio"]["data"]
                        return base64.b64decode(audio_data)
                    except (KeyError, IndexError) as e:
                        logger.error(f"Unexpected MiMo response format: {json.dumps(data, ensure_ascii=False)[:500]}")
                        raise HTTPException(status_code=502, detail=f"Unexpected API response format: {str(e)}")

            except HTTPException:
                raise
            except httpx.TimeoutException:
                logger.warning(f"MiMo API timeout, attempt {attempt+1}/{max_retries}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt))
                    continue
                raise HTTPException(status_code=504, detail="MiMo API timeout after retries")
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"MiMo API error: {e}, retrying (attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(base_delay)
                    continue
                raise

        # All retries exhausted
        raise HTTPException(status_code=429, detail="MiMo API rate limit: all retries exhausted, please try again later")


# ─── Legacy API (compatible with ms-ra-forwarder / reading apps) ───

async def _do_tts(text: str, voice: str) -> Response:
    """Core TTS logic shared by GET and POST endpoints."""
    if not text:
        raise HTTPException(status_code=400, detail="text parameter is required")

    # Resolve voice name (aliases + URL-decoding)
    voice = resolve_voice(voice)

    model = "mimo-v2.5-tts"
    ref_audio_b64 = None
    ref_audio_mime = "audio/mpeg"
    style_instruction = ""

    # Check if it's a cloned voice
    voice_info = voices_meta.get(voice)
    if voice_info:
        model = "mimo-v2.5-tts-voiceclone"
        # Load reference audio from local storage
        audio_path = VOICES_DIR / voice_info.get("audio_file", f"{voice}.mp3")
        if not audio_path.exists():
            audio_path = VOICES_DIR / f"{voice}.mp3"
        if not audio_path.exists():
            audio_path = VOICES_DIR / f"{voice}.wav"
        if audio_path.exists():
            async with aiofiles.open(audio_path, "rb") as f:
                audio_bytes = await f.read()
            ref_audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            ref_audio_mime = get_audio_mime(audio_path.name)
            style_instruction = voice_info.get("reference_text", "")
            logger.info(f"Using cloned voice: {voice}, audio={audio_path.name}, size={len(audio_bytes)} bytes")
        else:
            logger.error(f"Clone voice audio file not found: {audio_path}")
            raise HTTPException(status_code=404, detail=f"Cloned voice audio file not found: {voice}")

    # Check audio cache (same text + voice = reuse previous result)
    cache_key = get_cache_key(text, voice, model)
    cached = get_cached_audio(cache_key)
    if cached:
        logger.info(f"Cache hit: voice={voice}, text_len={len(text)}")
        return Response(
            content=cached,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": "inline; filename=tts_output.mp3",
                "Cache-Control": "public, max-age=86400",
                "X-Cache": "HIT",
            },
        )

    try:
        # For voiceclone: voice param is the reference audio data (set in call_mimo_tts via reference_audio_b64)
        # For preset: voice param is the preset voice name
        audio_bytes = await call_mimo_tts(
            text=text,
            voice=voice if not voice_info else "unused",
            model=model,
            output_format="mp3",
            reference_audio_b64=ref_audio_b64,
            reference_audio_mime=ref_audio_mime,
            style_instruction=style_instruction,
        )
        # Save to cache for future use
        save_cached_audio(cache_key, audio_bytes)
        # Periodic cache cleanup
        cleanup_cache(max_files=500)

        return Response(
            content=audio_bytes,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": "inline; filename=tts_output.mp3",
                "Cache-Control": "no-cache",
                "X-Cache": "MISS",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/text-to-speech")
async def legacy_tts_get(
    text: str = Query("", description="要合成的文本"),
    voice: str = Query("bingtang", description="音色别名或名称"),
    rate: int = Query(0, description="语速（暂不支持）"),
    volume: int = Query(0, description="音量（暂不支持）"),
    pitch: int = Query(0, description="音调（暂不支持）"),
):
    """Legacy GET TTS endpoint compatible with ms-ra-forwarder."""
    return await _do_tts(text, voice)


@app.post("/api/text-to-speech")
async def legacy_tts_post(
    text: str = Form("", description="要合成的文本"),
    voice: str = Form("bingtang", description="音色别名或名称"),
    rate: str = Form("0", description="语速"),
    volume: str = Form("0", description="音量"),
    pitch: str = Form("0", description="音调"),
    speakSpeed: str = Form("", description="Legado语速变量"),
):
    """Legacy POST TTS endpoint for Legado app (form body)."""
    # Legado may send speakSpeed instead of rate
    return await _do_tts(text, voice)


# ─── OpenAI-Compatible API ────────────────────────────────────

class TTSRequest(BaseModel):
    model: str = "mimo-v2.5-tts"
    input: str
    voice: str = "冰糖"
    response_format: str = "mp3"
    speed: float = 1.0

@app.post("/v1/audio/speech")
async def openai_tts(req: TTSRequest):
    """OpenAI-compatible TTS endpoint."""

    model = req.model
    ref_audio_b64 = None
    ref_audio_mime = "audio/mpeg"
    style_instruction = ""

    # Check if cloned voice
    voice_info = voices_meta.get(req.voice)
    if voice_info:
        model = "mimo-v2.5-tts-voiceclone"
        audio_path = VOICES_DIR / voice_info.get("audio_file", f"{req.voice}.mp3")
        if not audio_path.exists():
            audio_path = VOICES_DIR / f"{req.voice}.mp3"
        if not audio_path.exists():
            audio_path = VOICES_DIR / f"{req.voice}.wav"
        if audio_path.exists():
            async with aiofiles.open(audio_path, "rb") as f:
                audio_bytes = await f.read()
            ref_audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            ref_audio_mime = get_audio_mime(audio_path.name)
            style_instruction = voice_info.get("reference_text", "")

    audio_bytes = await call_mimo_tts(
        text=req.input,
        voice=req.voice if not voice_info else "unused",
        model=model,
        output_format=req.response_format,
        reference_audio_b64=ref_audio_b64,
        reference_audio_mime=ref_audio_mime,
        style_instruction=style_instruction,
    )

    content_type = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "pcm16": "audio/pcm",
    }.get(req.response_format, "audio/mpeg")

    return Response(content=audio_bytes, media_type=content_type)


@app.get("/v1/models")
async def list_models():
    """List available TTS models."""
    return {
        "object": "list",
        "data": [
            {"id": "mimo-v2.5-tts", "object": "model", "owned_by": "xiaomi", "description": "预置音色TTS"},
            {"id": "mimo-v2.5-tts-voicedesign", "object": "model", "owned_by": "xiaomi", "description": "文字描述生成音色"},
            {"id": "mimo-v2.5-tts-voiceclone", "object": "model", "owned_by": "xiaomi", "description": "声音克隆"},
        ],
    }


@app.get("/v1/voices")
async def list_voices():
    """List available voices (preset + cloned)."""
    voice_list = []
    for vid, info in PRESET_VOICES.items():
        voice_list.append({
            "id": vid,
            "name": info["name"],
            "type": "preset",
            "lang": info["lang"],
            "gender": info.get("gender", ""),
        })
    for vid, info in voices_meta.items():
        voice_list.append({
            "id": vid,
            "name": info.get("name", vid),
            "type": "cloned",
            "lang": info.get("lang", "zh-CN"),
            "gender": info.get("gender", ""),
            "created": info.get("created", ""),
            "reference_text": info.get("reference_text", ""),
        })
    return {"object": "list", "data": voice_list}


# ─── Voice Management API ────────────────────────────────────

@app.post("/v1/voices/create")
async def create_voice(
    audio: UploadFile = File(..., description="参考音频文件（wav/mp3，建议3-10秒清晰语音）"),
    name: str = Form(..., description="音色名称"),
    reference_text: str = Form("", description="参考文本（音频中说的内容，可选）"),
    lang: str = Form("zh-CN", description="语言代码"),
):
    """Upload a reference audio to create a cloned voice."""

    # Generate voice ID
    voice_id = f"clone_{uuid.uuid4().hex[:8]}"

    # Read and validate audio
    audio_bytes = await audio.read()
    max_size = settings.max_audio_size_mb * 1024 * 1024
    if len(audio_bytes) > max_size:
        raise HTTPException(status_code=400, detail=f"Audio file too large (max {settings.max_audio_size_mb}MB)")

    if len(audio_bytes) == 0:
        raise HTTPException(status_code=400, detail="Audio file is empty")

    # Save audio file with original extension
    orig_name = audio.filename or "audio.wav"
    ext = Path(orig_name).suffix.lower() or ".wav"
    audio_filename = f"{voice_id}{ext}"
    audio_path = VOICES_DIR / audio_filename
    async with aiofiles.open(audio_path, "wb") as f:
        await f.write(audio_bytes)

    # If no reference text, mark as auto
    if not reference_text:
        reference_text = ""

    # Save metadata
    meta = load_voices_meta()
    meta[voice_id] = {
        "name": name,
        "lang": lang,
        "reference_text": reference_text,
        "audio_file": audio_filename,
        "created": datetime.now().isoformat(),
        "audio_size": len(audio_bytes),
    }
    save_voices_meta(meta)

    # Update global reference
    global voices_meta
    voices_meta = meta

    logger.info(f"Created cloned voice: {voice_id} ({name}), size={len(audio_bytes)} bytes")
    return {"voice_id": voice_id, "name": name, "status": "created"}


@app.get("/v1/voices/custom")
async def list_custom_voices():
    """List all custom (cloned) voices."""
    meta = load_voices_meta()
    return {"voices": meta}


@app.delete("/v1/voices/{voice_id}")
async def delete_voice(voice_id: str):
    """Delete a custom voice."""
    meta = load_voices_meta()
    if voice_id not in meta:
        raise HTTPException(status_code=404, detail="Voice not found")

    # Delete audio file
    audio_filename = meta[voice_id].get("audio_file", f"{voice_id}.wav")
    audio_path = VOICES_DIR / audio_filename
    if audio_path.exists():
        audio_path.unlink()

    # Remove from metadata
    del meta[voice_id]
    save_voices_meta(meta)

    global voices_meta
    voices_meta = meta

    logger.info(f"Deleted voice: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}


@app.post("/v1/voices/test")
async def test_voice(
    voice_id: str = Query(...),
    text: str = Query("这是一段测试语音，用于验证声音克隆效果。"),
):
    """Test a voice by generating a sample."""

    meta = load_voices_meta()
    if voice_id not in meta and voice_id not in PRESET_VOICES:
        raise HTTPException(status_code=404, detail="Voice not found")

    model = "mimo-v2.5-tts"
    ref_audio_b64 = None
    ref_audio_mime = "audio/mpeg"
    style_instruction = ""
    actual_voice = voice_id

    if voice_id in meta:
        model = "mimo-v2.5-tts-voiceclone"
        actual_voice = "unused"
        audio_filename = meta[voice_id].get("audio_file", f"{voice_id}.mp3")
        audio_path = VOICES_DIR / audio_filename
        if not audio_path.exists():
            audio_path = VOICES_DIR / f"{voice_id}.mp3"
        if not audio_path.exists():
            audio_path = VOICES_DIR / f"{voice_id}.wav"
        if audio_path.exists():
            async with aiofiles.open(audio_path, "rb") as f:
                audio_bytes = await f.read()
            ref_audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            ref_audio_mime = get_audio_mime(audio_path.name)
            style_instruction = meta[voice_id].get("reference_text", "")

    audio_bytes = await call_mimo_tts(
        text=text,
        voice=actual_voice,
        model=model,
        output_format="mp3",
        reference_audio_b64=ref_audio_b64,
        reference_audio_mime=ref_audio_mime,
        style_instruction=style_instruction,
    )

    return Response(content=audio_bytes, media_type="audio/mpeg")


# ─── Health Check ─────────────────────────────────────────────

@app.get("/health")
async def health():
    meta = load_voices_meta()
    return {
        "status": "healthy",
        "version": VERSION,
        "preset_voices": len(PRESET_VOICES),
        "custom_voices": len(meta),
        "api_key_configured": bool(settings.mimo_api_key),
    }


# ─── Web Management Page ────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Web management interface."""
    meta = load_voices_meta()
    all_voices = []
    for vid, info in PRESET_VOICES.items():
        all_voices.append({"id": vid, **info, "type": "preset"})
    for vid, info in meta.items():
        all_voices.append({"id": vid, **info, "type": "cloned"})

    html = ADMIN_HTML_TEMPLATE.replace("{{VOICES_JSON}}", json.dumps(all_voices, ensure_ascii=False))
    html = html.replace("{{API_KEY_CONFIGURED}}", str(bool(settings.mimo_api_key)).lower())
    html = html.replace("{{TOKEN}}", settings.token)
    return HTMLResponse(content=html)


# ─── Admin HTML Template ──────────────────────────────────────

ADMIN_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MiMO TTS Forwarder</title>
<style>
:root {
    --primary: #4f46e5;
    --primary-hover: #4338ca;
    --bg: #f8fafc;
    --card: #ffffff;
    --text: #1e293b;
    --text-muted: #64748b;
    --border: #e2e8f0;
    --success: #10b981;
    --danger: #ef4444;
    --warning: #f59e0b;
    --radius: 12px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
}
.header {
    background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
    color: white;
    padding: 2rem;
    text-align: center;
}
.header h1 { font-size: 1.8rem; margin-bottom: 0.5rem; }
.header p { opacity: 0.9; font-size: 0.95rem; }
.container { max-width: 960px; margin: 0 auto; padding: 1.5rem; }
.card {
    background: var(--card);
    border-radius: var(--radius);
    border: 1px solid var(--border);
    padding: 1.5rem;
    margin-bottom: 1.5rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
.card h2 {
    font-size: 1.2rem;
    margin-bottom: 1rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.status-dot {
    width: 10px; height: 10px; border-radius: 50%; display: inline-block;
}
.status-dot.ok { background: var(--success); }
.status-dot.warn { background: var(--warning); }
.status-dot.err { background: var(--danger); }
.form-group { margin-bottom: 1rem; }
.form-group label {
    display: block; font-weight: 600; margin-bottom: 0.3rem; font-size: 0.9rem; color: var(--text-muted);
}
.form-group input, .form-group select, .form-group textarea {
    width: 100%; padding: 0.6rem 0.8rem; border: 1px solid var(--border);
    border-radius: 8px; font-size: 0.95rem; font-family: inherit;
    transition: border-color 0.2s;
}
.form-group input:focus, .form-group select:focus, .form-group textarea:focus {
    outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(79,70,229,0.1);
}
.btn {
    display: inline-flex; align-items: center; gap: 0.4rem;
    padding: 0.6rem 1.2rem; border-radius: 8px; border: none;
    font-size: 0.9rem; font-weight: 600; cursor: pointer;
    transition: all 0.2s; font-family: inherit;
}
.btn-primary { background: var(--primary); color: white; }
.btn-primary:hover { background: var(--primary-hover); }
.btn-danger { background: var(--danger); color: white; }
.btn-danger:hover { background: #dc2626; }
.btn-success { background: var(--success); color: white; }
.btn-success:hover { background: #059669; }
.btn-outline {
    background: transparent; color: var(--primary); border: 1px solid var(--primary);
}
.btn-outline:hover { background: var(--primary); color: white; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.voice-list { display: grid; gap: 0.8rem; }
.voice-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 1rem; border: 1px solid var(--border); border-radius: 8px;
    transition: border-color 0.2s;
}
.voice-item:hover { border-color: var(--primary); }
.voice-info { flex: 1; }
.voice-info .name { font-weight: 600; font-size: 1rem; }
.voice-info .meta { font-size: 0.85rem; color: var(--text-muted); margin-top: 0.2rem; }
.voice-actions { display: flex; gap: 0.5rem; }
.badge {
    display: inline-block; padding: 0.15rem 0.5rem; border-radius: 20px;
    font-size: 0.75rem; font-weight: 600;
}
.badge-preset { background: #dbeafe; color: #1d4ed8; }
.badge-cloned { background: #fce7f3; color: #be185d; }
.test-section { margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--border); }
.audio-player { margin-top: 0.5rem; }
.audio-player audio { width: 100%; margin-top: 0.5rem; }
.api-info { font-family: 'SF Mono', 'Cascadia Code', monospace; font-size: 0.85rem; }
.api-info code {
    background: #f1f5f9; padding: 0.2rem 0.4rem; border-radius: 4px; font-size: 0.85rem;
}
.api-info pre {
    background: #1e293b; color: #e2e8f0; padding: 1rem; border-radius: 8px;
    overflow-x: auto; margin-top: 0.5rem; font-size: 0.82rem; line-height: 1.5;
}
.tabs { display: flex; gap: 0; margin-bottom: 1rem; border-bottom: 2px solid var(--border); }
.tab {
    padding: 0.6rem 1.2rem; cursor: pointer; font-weight: 500;
    border-bottom: 2px solid transparent; margin-bottom: -2px;
    color: var(--text-muted); transition: all 0.2s;
}
.tab.active { color: var(--primary); border-bottom-color: var(--primary); }
.tab:hover { color: var(--primary); }
.tab-content { display: none; }
.tab-content.active { display: block; }
.loading { display: none; align-items: center; gap: 0.5rem; color: var(--text-muted); font-size:0.9rem; }
.loading.show { display: inline-flex; }
.spinner {
    width: 16px; height: 16px; border: 2px solid var(--border);
    border-top-color: var(--primary); border-radius: 50%;
    animation: spin 0.6s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.toast {
    position: fixed; bottom: 2rem; right: 2rem; padding: 0.8rem 1.2rem;
    border-radius: 8px; color: white; font-size: 0.9rem; z-index: 999;
    transform: translateY(100px); opacity: 0; transition: all 0.3s;
}
.toast.show { transform: translateY(0); opacity: 1; }
.toast.success { background: var(--success); }
.toast.error { background: var(--danger); }
.empty-state { text-align: center; padding: 2rem; color: var(--text-muted); }
</style>
</head>
<body>

<div class="header">
    <h1>MiMO TTS Forwarder</h1>
    <p>声音克隆 TTS 引擎 - 兼容阅读 App 调用 | 基于 MiMo-V2.5-TTS</p>
</div>

<div class="container">

    <!-- Status Card -->
    <div class="card">
        <h2><span class="status-dot" id="statusDot"></span> 服务状态</h2>
        <div id="statusInfo"></div>
    </div>

    <!-- Tabs -->
    <div class="tabs">
        <div class="tab active" onclick="switchTab('voices')">音色管理</div>
        <div class="tab" onclick="switchTab('test')">在线测试</div>
        <div class="tab" onclick="switchTab('api')">API 文档</div>
    </div>

    <!-- Voices Tab -->
    <div id="tab-voices" class="tab-content active">
        <div class="card">
            <h2>上传参考音频（声音克隆）</h2>
            <form id="uploadForm" onsubmit="return uploadVoice(event)">
                <div class="form-group">
                    <label>音色名称</label>
                    <input type="text" id="voiceName" name="name" placeholder="例：我的声音" required>
                </div>
                <div class="form-group">
                    <label>参考音频（建议 3-10 秒清晰语音，WAV/MP3 格式，最大 10MB）</label>
                    <input type="file" id="voiceAudio" name="audio" accept="audio/*" required>
                </div>
                <div class="form-group">
                    <label>参考文本（音频中说的内容，可选，有助于提升克隆质量）</label>
                    <input type="text" id="voiceRefText" name="reference_text" placeholder="音频中说的内容">
                </div>
                <div class="form-group">
                    <label>语言</label>
                    <select id="voiceLang" name="lang">
                        <option value="zh-CN">中文</option>
                        <option value="en-US">English</option>
                        <option value="ja-JP">日本語</option>
                        <option value="ko-KR">한국어</option>
                    </select>
                </div>
                <button type="submit" class="btn btn-primary">上传并创建音色</button>
                <span id="uploadLoading" class="loading"><span class="spinner"></span>上传中...</span>
            </form>
        </div>

        <div class="card">
            <h2>音色列表</h2>
            <div id="voiceList" class="voice-list"></div>
        </div>
    </div>

    <!-- Test Tab -->
    <div id="tab-test" class="tab-content">
        <div class="card">
            <h2>语音合成测试</h2>
            <div class="form-group">
                <label>选择音色</label>
                <select id="testVoice"></select>
            </div>
            <div class="form-group">
                <label>输入文本</label>
                <textarea id="testText" rows="3" placeholder="输入要合成的文本">这是一段语音合成测试，用于验证声音克隆效果。</textarea>
            </div>
            <button class="btn btn-primary" onclick="testTTS()">开始合成</button>
            <span id="testLoading" class="loading"><span class="spinner"></span>合成中，请稍候（声音克隆可能需要较长时间）...</span>
            <div id="testResult" class="test-section" style="display:none">
                <label>合成结果</label>
                <div class="audio-player">
                    <audio id="testAudio" controls></audio>
                </div>
            </div>
        </div>
    </div>

    <!-- API Tab -->
    <div id="tab-api" class="tab-content">
        <div class="card">
            <h2>阅读 App（Legado）配置</h2>
            <p style="margin-bottom:0.5rem;color:var(--text-muted)">
                在阅读 App 中点击"+"添加朗读引擎，URL 填写以下格式：
            </p>
            <div style="margin-bottom:1rem">
                <label style="font-weight:600;font-size:0.85rem;color:var(--text-muted)">预置音色（冰糖/茉莉/苏打/白桦等）：</label>
                <div class="api-info" style="margin-top:0.3rem">
                    <pre id="legadoUrlPreset">http://YOUR_SERVER:8765/api/text-to-speech,{"method":"POST","body":"text={{encodeURIComponent(speakText)}}&voice=bingtang"}</pre>
                </div>
            </div>
            <div style="margin-bottom:1rem">
                <label style="font-weight:600;font-size:0.85rem;color:var(--text-muted)">克隆音色（替换 clone_xxxx 为你的音色ID）：</label>
                <div class="api-info" style="margin-top:0.3rem">
                    <pre id="legadoUrlClone">http://YOUR_SERVER:8765/api/text-to-speech,{"method":"POST","body":"text={{encodeURIComponent(speakText)}}&voice=clone_xxxx"}</pre>
                </div>
            </div>
            <p style="margin-bottom:0.5rem;color:var(--text-muted);font-size:0.85rem">
                其他字段全部留空。Content-Type 不需要填写。<br>
                语速建议在阅读 App 内设置为 2.5 左右。<br>
                音色别名对照：bingtang=冰糖（女）、moli=茉莉（女）、suda=苏打（男）、baihua=白桦（男）、default=默认、Mia=英女、Chloe=英女、Milo=英男、Dean=英男
            </p>
        </div>

        <div class="card">
            <h2>兼容 ms-ra-forwarder 的 GET 接口</h2>
            <div class="api-info">
                <p>浏览器/脚本可直接调用：</p>
                <pre>GET /api/text-to-speech?voice=冰糖&amp;text=你好世界

预置音色：冰糖、茉莉、苏打、白桦、Mia、Chloe、Milo、Dean
克隆音色：使用 clone_xxxx 格式的音色ID

参数：
  voice  - 音色名称（如 冰糖）或克隆音色ID（clone_xxxx）
  text   - 要合成的文本
  rate/volume/pitch - 暂不支持，忽略

也支持 POST 方式（Legado 使用此格式）：
  POST /api/text-to-speech
  Content-Type: application/x-www-form-urlencoded
  Body: text=你好世界&voice=冰糖</pre>
            </div>
        </div>

        <div class="card">
            <h2>OpenAI 兼容接口</h2>
            <div class="api-info">
                <pre>POST /v1/audio/speech
Content-Type: application/json

{
  "model": "mimo-v2.5-tts",
  "input": "你好世界",
  "voice": "冰糖",
  "response_format": "mp3"
}

model 可选值：
  mimo-v2.5-tts           - 预置音色合成
  mimo-v2.5-tts-voiceclone - 声音克隆
  mimo-v2.5-tts-voicedesign - 文字描述生成音色</pre>
            </div>
        </div>

        <div class="card">
            <h2>声音克隆接口</h2>
            <div class="api-info">
                <pre>POST /v1/voices/create
Content-Type: multipart/form-data

参数：
  audio          - 参考音频文件（wav/mp3，建议3-10秒）
  name           - 音色名称
  reference_text - 参考文本（可选，有助于提升克隆质量）
  lang           - 语言代码（默认 zh-CN）</pre>
            </div>
        </div>

        <div class="card">
            <h2>其他接口</h2>
            <div class="api-info">
                <pre>GET  /v1/voices          - 列出所有音色
GET  /v1/voices/custom  - 列出自定义音色
DEL  /v1/voices/{id}    - 删除音色
POST /v1/voices/test    - 测试音色
GET  /v1/models         - 列出可用模型
GET  /health            - 健康检查</pre>
            </div>
        </div>
    </div>

</div>

<div id="toast" class="toast"></div>

<script>
const VOICES = {{VOICES_JSON}};
const API_CONFIGURED = {{API_KEY_CONFIGURED}};

// Init status
(function() {
    const dot = document.getElementById('statusDot');
    const info = document.getElementById('statusInfo');
    if (API_CONFIGURED) {
        dot.classList.add('ok');
        info.innerHTML = '<p style="color:var(--success)">API Key 已配置，服务正常运行</p>';
    } else {
        dot.classList.add('err');
        info.innerHTML = '<p style="color:var(--danger)">API Key 未配置！请在 .env 文件中设置 MIMO_API_KEY</p>';
    }
})();

// Tab switching
function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    const idx = name === 'voices' ? 0 : name === 'test' ? 1 : 2;
    document.querySelectorAll('.tab')[idx].classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
}

// Toast notification
function showToast(msg, type = 'success') {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + type + ' show';
    setTimeout(() => t.classList.remove('show'), 3000);
}

// Render voice list
function renderVoices() {
    const list = document.getElementById('voiceList');
    const testSelect = document.getElementById('testVoice');

    if (VOICES.length === 0) {
        list.innerHTML = '<div class="empty-state">暂无音色，请上传参考音频创建克隆音色</div>';
    } else {
        list.innerHTML = VOICES.map(v => {
            const isPreset = v.type === 'preset';
            const genderStr = v.gender === 'female' ? '女' : v.gender === 'male' ? '男' : '';
            return `
            <div class="voice-item">
                <div class="voice-info">
                    <div class="name">${v.name || v.id} <span class="badge badge-${v.type}">${isPreset ? '预置' : '克隆'}</span></div>
                    <div class="meta">ID: ${v.id} | ${v.lang || 'zh-CN'} ${genderStr ? '| ' + genderStr : ''} ${v.reference_text ? '| ' + v.reference_text.substring(0, 30) : ''}</div>
                </div>
                <div class="voice-actions">
                    <button class="btn btn-outline" onclick="quickTest('${v.id}')">测试</button>
                    ${!isPreset ? `<button class="btn btn-danger" onclick="deleteVoice('${v.id}')">删除</button>` : ''}
                </div>
            </div>`;
        }).join('');
    }

    testSelect.innerHTML = VOICES.map(v =>
        `<option value="${v.id}">${v.name || v.id} (${v.type === 'preset' ? '预置' : '克隆'})</option>`
    ).join('');
}

// Upload voice
async function uploadVoice(e) {
    e.preventDefault();
    const form = document.getElementById('uploadForm');
    const fd = new FormData(form);
    document.getElementById('uploadLoading').classList.add('show');

    try {
        const resp = await fetch('/v1/voices/create', { method: 'POST', body: fd });
        const data = await resp.json();
        if (resp.ok) {
            showToast('音色创建成功: ' + data.voice_id);
            setTimeout(() => location.reload(), 1500);
        } else {
            showToast('创建失败: ' + (data.detail || '未知错误'), 'error');
        }
    } catch (err) {
        showToast('上传出错: ' + err.message, 'error');
    } finally {
        document.getElementById('uploadLoading').classList.remove('show');
    }
    return false;
}

// Delete voice
async function deleteVoice(id) {
    if (!confirm('确定要删除此音色吗？')) return;
    try {
        const resp = await fetch('/v1/voices/' + id, { method: 'DELETE' });
        if (resp.ok) {
            showToast('已删除');
            setTimeout(() => location.reload(), 1000);
        } else {
            showToast('删除失败', 'error');
        }
    } catch (err) {
        showToast('删除出错: ' + err.message, 'error');
    }
}

// Test TTS
async function testTTS() {
    const voice = document.getElementById('testVoice').value;
    const text = document.getElementById('testText').value;
    if (!text) { showToast('请输入文本', 'error'); return; }

    document.getElementById('testLoading').classList.add('show');
    document.getElementById('testResult').style.display = 'none';
    try {
        const resp = await fetch(`/api/text-to-speech?voice=${encodeURIComponent(voice)}&text=${encodeURIComponent(text)}`);
        if (resp.ok) {
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            document.getElementById('testAudio').src = url;
            document.getElementById('testResult').style.display = 'block';
            showToast('合成成功');
        } else {
            const errText = await resp.text();
            showToast('合成失败: ' + resp.status + ' ' + errText.substring(0, 100), 'error');
        }
    } catch (err) {
        showToast('合成出错: ' + err.message, 'error');
    } finally {
        document.getElementById('testLoading').classList.remove('show');
    }
}

// Quick test from voice list
function quickTest(voiceId) {
    switchTab('test');
    document.getElementById('testVoice').value = voiceId;
    document.getElementById('testText').value = '你好，这是一段声音克隆的测试语音。';
}

// Init
renderVoices();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
