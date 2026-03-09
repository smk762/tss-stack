from fastapi import APIRouter

from app.core import config


router = APIRouter(tags=["capabilities"])


@router.get("/capabilities")
async def get_capabilities():
    # Keep these neutral and UI-safe; clamp per deployment via env in the future.
    return {
        "api_version": "v1",
        "stt": {
            "enabled": True,
            "accepts": {"multipart": True, "json_base64": True},
            "audio": {"max_bytes": config.STT_MAX_BYTES, "supported_mime_types": config.STT_SUPPORTED_MIME_TYPES},
            "output_formats": config.STT_OUTPUT_FORMATS,
            "features": {"diarization": False, "timestamps": True},
        },
        "tts": {
            "enabled": True,
            "output_formats": config.TTS_OUTPUT_FORMATS,
            "voices": {"supports_list": True, "supports_custom_upload": False},
            "controls": {
                "speed": {"min": 0.7, "max": 1.6, "default": 1.0, "step": 0.05, "enabled": True},
                "pitch_semitones": {"min": -6.0, "max": 6.0, "default": 0.0, "step": 0.5, "enabled": True},
                "formant_shift": {"min": -1.0, "max": 1.0, "default": 0.0, "step": 0.1, "enabled": True},
                "energy": {"min": 0.0, "max": 1.0, "default": 0.5, "step": 0.05, "enabled": True},
                "pause_ms": {"min": 0, "max": 1000, "default": 0, "step": 25, "enabled": True},
                "sentence_pause_ms": {"min": 0, "max": 600, "default": 120, "step": 20, "enabled": True},
                "pause_variance_ms": {"min": 0, "max": 120, "default": 20, "step": 5, "enabled": True},
                "loudness_db": {"min": -12.0, "max": 6.0, "default": 0.0, "step": 0.5, "enabled": True},
                "clarity_boost": {"min": 0.0, "max": 1.0, "default": 0.5, "step": 0.05, "enabled": True},
                "breathiness": {"min": 0.0, "max": 1.0, "default": 0.2, "step": 0.05, "enabled": True},
                "post_eq_profile": {"options": ["neutral", "warm", "broadcast", "crisp"], "default": "neutral", "enabled": True},
                # staged / not yet implemented
                "prosody_depth": {"min": 0.0, "max": 1.0, "default": 0.4, "step": 0.05, "enabled": False},
                "tempo_variance": {"min": 0.0, "max": 0.05, "default": 0.015, "step": 0.005, "enabled": False},
                "nasality": {"min": 0.0, "max": 0.6, "default": 0.0, "step": 0.05, "enabled": True},
                "intensity": {"min": 0.0, "max": 1.0, "default": 0.4, "step": 0.05, "enabled": False},
                "emphasis_strength": {"min": 0.0, "max": 1.0, "default": 0.5, "step": 0.05, "enabled": True},
                "variation": {"min": 0.0, "max": 1.0, "default": 0.3, "step": 0.05, "enabled": False},
                "articulation": {"min": 0.0, "max": 1.0, "default": 0.6, "step": 0.05, "enabled": False},
                "punctuation_weight": {"min": 0.0, "max": 1.0, "default": 0.7, "step": 0.05, "enabled": True},
                "sentence_split_aggressiveness": {"min": 0.0, "max": 1.0, "default": 0.5, "step": 0.05, "enabled": True},
                "repeat_emphasis": {"min": 0.0, "max": 1.0, "default": 0.4, "step": 0.05, "enabled": True},
                "latency_mode": {"options": ["quality", "balanced", "realtime"], "default": "balanced", "enabled": True},
                "stream_chunk_ms": {"min": 40, "max": 400, "default": 120, "step": 20, "enabled": True},
                "engine_temperature": {"min": 0.0, "max": 1.0, "default": 0.7, "step": 0.05, "enabled": True},
                "stability": {"min": 0.0, "max": 1.0, "default": 0.5, "step": 0.05, "enabled": False},
            },
        },
        "jobs": {"poll_interval_ms_default": 750, "result_url_ttl_seconds": config.RESULT_URL_TTL_SECONDS},
    }

