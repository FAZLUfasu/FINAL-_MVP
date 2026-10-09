
import asyncio
import base64
import io
import json
import html
import os
import re
import subprocess
import sys
import tempfile
import time
import warnings
import wave
from typing import Optional

warnings.filterwarnings("ignore", category=UserWarning, module="torch.*")
warnings.filterwarnings("ignore", category=UserWarning, module="TTS.*")
warnings.filterwarnings("ignore", message=".*attention_mask.*")

num_cores = str(os.cpu_count() or 4)
os.environ.setdefault("OMP_NUM_THREADS", num_cores)
os.environ.setdefault("MKL_NUM_THREADS", num_cores)

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.files.base import ContentFile
from faster_whisper import WhisperModel
import numpy as np
from pydub import AudioSegment
from calls.conversation_engine import generate_conversation_turn, generate_gemini_json


# Keep the same FFmpeg paths that are already working in the current backend.
AudioSegment.converter = (
    r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
)
AudioSegment.ffprobe = (
    r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
)

from calls.models import CallSession, CompanyScript, Contact, CustomVoice, SalesInsight, SystemSettings


# ---------------------------------------------------------------------------
# GLOBAL ENGINES
# ---------------------------------------------------------------------------

print("🧠 Loading Whisper Speech Engine inside Django...")

# Multilingual STT configuration for cellular speech.
# IMPORTANT: use a multilingual Whisper model (no `.en` suffix).
# Override without editing code, for example:
#   $env:TELICALL_WHISPER_MODEL="small"
# `base` is the low-latency multilingual default for CPU telephony.
WHISPER_MODEL_NAME = os.getenv(
    "TELICALL_WHISPER_MODEL",
    "small",
).strip()

whisper_model = WhisperModel(
    WHISPER_MODEL_NAME,
    device="cpu",
    compute_type="int8",
    cpu_threads=max(1, os.cpu_count() or 4),
)

print(
    f"✅ Whisper Bound to Django App! model={WHISPER_MODEL_NAME}"
)

# Languages officially enabled for Telicall Step 1.
SUPPORTED_LANGUAGES = {
    "en": "English",
    "ml": "Malayalam",
    "ta": "Tamil",
    "hi": "Hindi",
    "ar": "Arabic",
}

DEFAULT_LANGUAGE_CODE = "en"

# Only reliable automatic Whisper detections can influence language learning.
MIN_LANGUAGE_EVIDENCE_CONFIDENCE = 0.60
MIN_LANGUAGE_LOCK_SHARE = 0.60
MIN_LANGUAGE_LOCK_WINNER_TURNS = 2

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Default is deliberately Windows SAPI because XTTS-v2 on the user's CPU was
# taking 13-70 seconds per sentence. SAPI is offline and normally much faster.
#
# Optional environment variables:
#   BRAINEX_TTS_ENGINE=sapi
#   BRAINEX_SAPI_VOICE=<installed Windows voice name>
#   BRAINEX_SAPI_RATE=1
#
TTS_ENGINE = os.getenv("BRAINEX_TTS_ENGINE", "sapi").strip().lower()
SAPI_VOICE = os.getenv("BRAINEX_SAPI_VOICE", "").strip()
try:
    SAPI_RATE = max(-10, min(10, int(os.getenv("BRAINEX_SAPI_RATE", "1"))))
except ValueError:
    SAPI_RATE = 1
try:
    SAPI_VOLUME = max(0, min(100, int(os.getenv("BRAINEX_SAPI_VOLUME", "100"))))
except ValueError:
    SAPI_VOLUME = 100

# Azure Neural TTS is the multilingual speech path.
# Credentials are intentionally read from environment variables rather than
# stored in source code or the database.
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "").strip()
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "").strip()

AZURE_VOICE_BY_LANGUAGE = {
    "en": "en-IN-NeerjaNeural",
    "ml": "ml-IN-SobhanaNeural",
    "ta": "ta-IN-PallaviNeural",
    "hi": "hi-IN-AnanyaNeural",
    "ar": "ar-SA-ZariyahNeural",
}

AZURE_LOCALE_BY_LANGUAGE = {
    "en": "en-IN",
    "ml": "ml-IN",
    "ta": "ta-IN",
    "hi": "hi-IN",
    "ar": "ar-SA",
}

# 16 kHz, mono, PCM16 = 32,000 bytes/sec
PCM_SAMPLE_RATE = 16000
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * 2

# Send audio in modest chunks so barge-in can stop playback quickly.
AI_STREAM_CHUNK_BYTES = 16000  # ~0.5 s at 16 kHz PCM16 mono




# ---------------------------------------------------------------------------
# FAST OFFLINE TTS
# ---------------------------------------------------------------------------

def _sanitize_tts_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    cleaned = re.sub(r"[^\w\s.,?!'\-]", "", cleaned)
    return cleaned.strip()


async def generate_sapi_pcm_bytes(
    text: str,
    voice_name: str = "",
    rate: int = 1,
    volume: int = 100,
) -> bytes:
    """
    Windows-safe offline SAPI TTS.

    IMPORTANT:
    Daphne/Channels on this Windows setup can use an asyncio event loop that
    does not implement asyncio subprocess support. That caused:
        NotImplementedError
    at asyncio.create_subprocess_exec(...).

    This version runs normal subprocess.run(...) inside asyncio.to_thread().
    SAPI writes 16 kHz / mono / PCM16 directly, so no FFmpeg conversion is
    required before the bytes are sent to Flutter.
    """
    cleaned = _sanitize_tts_text(text)
    if not cleaned:
        return b""

    fd, temp_wav = tempfile.mkstemp(
        prefix="brainex_tts_",
        suffix=".wav",
    )
    os.close(fd)

    # Let SpeechSynthesizer create the WAV itself.
    try:
        os.remove(temp_wav)
    except OSError:
        pass

    env = os.environ.copy()
    env["BRAINEX_TTS_TEXT"] = cleaned
    env["BRAINEX_TTS_OUT"] = temp_wav
    env["BRAINEX_TTS_VOICE"] = str(voice_name or "").strip()
    env["BRAINEX_TTS_RATE"] = str(max(-10, min(10, int(rate))))
    env["BRAINEX_TTS_VOLUME"] = str(max(0, min(100, int(volume))))

    ps_script = r"""
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Speech

$text = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_TEXT')
$out = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_OUT')
$voice = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_VOICE')
$rateRaw = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_RATE')
$volumeRaw = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_VOLUME')

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

try {
    if ($voice -and $voice.Trim().Length -gt 0) {
        $synth.SelectVoice($voice)
    }

    $rate = 1
    $parsedRate = 0

    if ([int]::TryParse($rateRaw, [ref]$parsedRate)) {
        $rate = $parsedRate
    }

    if ($rate -lt -10) { $rate = -10 }
    if ($rate -gt 10) { $rate = 10 }

    $synth.Rate = $rate

    $volume = 100
    $parsedVolume = 100

    if ([int]::TryParse($volumeRaw, [ref]$parsedVolume)) {
        $volume = $parsedVolume
    }

    if ($volume -lt 0) { $volume = 0 }
    if ($volume -gt 100) { $volume = 100 }

    $synth.Volume = $volume

    # Generate exactly the format expected by Flutter/Android:
    # 16 kHz, mono, signed PCM16.
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )

    $synth.SetOutputToWaveFile($out, $format)
    $synth.Speak($text)
}
finally {
    $synth.Dispose()
}
"""

    def _run_sapi_sync():
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32"
                else 0
            ),
            timeout=20,
            check=False,
        )

    try:
        start_time = time.perf_counter()

        # Windows/Daphne-safe: no asyncio.create_subprocess_exec().
        result = await asyncio.to_thread(_run_sapi_sync)

        if result.returncode != 0:
            stderr = result.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()

            stdout = result.stdout.decode(
                "utf-8",
                errors="replace",
            ).strip()

            print(
                f"❌ [SAPI TTS ERROR] exit={result.returncode} | "
                f"stderr={stderr or '<empty>'} | "
                f"stdout={stdout or '<empty>'}"
            )
            return b""

        if not os.path.exists(temp_wav):
            print(
                "❌ [SAPI TTS ERROR] "
                "PowerShell completed but no WAV file was created."
            )
            return b""

        if os.path.getsize(temp_wav) <= 44:
            print(
                "❌ [SAPI TTS ERROR] "
                "Generated WAV contains no audio samples."
            )
            return b""

        # Read the WAV with Python's standard library.
        # Because SAPI generated 16k/mono/PCM16 directly, no conversion is
        # necessary.
        with wave.open(temp_wav, "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            pcm = wav_file.readframes(frame_count)

        print(
            f"🔎 [SAPI WAV FORMAT] "
            f"rate={sample_rate} | "
            f"channels={channels} | "
            f"width={sample_width} | "
            f"frames={frame_count}"
        )

        if (
            sample_rate != PCM_SAMPLE_RATE
            or channels != 1
            or sample_width != 2
        ):
            print(
                "❌ [SAPI FORMAT ERROR] "
                f"Expected 16000Hz/mono/16-bit but got "
                f"{sample_rate}Hz/{channels}ch/"
                f"{sample_width * 8}-bit."
            )
            return b""

        if len(pcm) % 2:
            pcm = pcm[:-1]

        elapsed = time.perf_counter() - start_time
        audio_seconds = (
            len(pcm) / PCM_BYTES_PER_SECOND
            if pcm
            else 0.0
        )
        rtf = (
            elapsed / audio_seconds
            if audio_seconds > 0
            else 0.0
        )

        print(
            f"⚡ [SAPI TTS READY] "
            f"chars={len(cleaned)} | "
            f"pcm={len(pcm)} | "
            f"audio={audio_seconds:.2f}s | "
            f"time={elapsed:.2f}s | "
            f"rtf={rtf:.2f}"
        )

        return pcm

    except asyncio.CancelledError:
        # asyncio.to_thread() cannot forcibly stop a thread already running,
        # but latest-turn generation IDs prevent its stale PCM from being sent.
        print("🛑 [SAPI TTS REQUEST CANCELLED]")
        raise

    except subprocess.TimeoutExpired:
        print(
            "❌ [SAPI TTS TIMEOUT] "
            "Windows speech synthesis exceeded 20 seconds."
        )
        return b""

    except FileNotFoundError:
        print(
            "❌ [SAPI TTS ERROR] powershell.exe was not found."
        )
        return b""

    except Exception as e:
        print(
            f"❌ [SAPI TTS EXCEPTION] "
            f"{type(e).__name__}: {repr(e)}"
        )
        return b""

    finally:
        try:
            os.remove(temp_wav)
        except OSError:
            pass


async def generate_azure_pcm_bytes(
    text: str,
    language_code: str = DEFAULT_LANGUAGE_CODE,
    voice_name: str = "",
) -> bytes:
    """
    Generate raw 16kHz / mono / PCM16 using Azure Neural TTS REST API.

    This intentionally uses the REST endpoint rather than the Azure Speech SDK,
    so the existing project only needs `requests`, which it already uses.
    """
    cleaned = _sanitize_tts_text(text)
    if not cleaned:
        return b""

    language_code = str(
        language_code or DEFAULT_LANGUAGE_CODE
    ).strip().lower()

    if language_code not in SUPPORTED_LANGUAGES:
        language_code = DEFAULT_LANGUAGE_CODE

    if not AZURE_SPEECH_KEY or not AZURE_SPEECH_REGION:
        print(
            "⚠️ [AZURE TTS NOT CONFIGURED] "
            "Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION. "
            "Using SAPI fallback."
        )
        return b""

    selected_voice = (
        str(voice_name or "").strip()
        or AZURE_VOICE_BY_LANGUAGE.get(
            language_code,
            AZURE_VOICE_BY_LANGUAGE[DEFAULT_LANGUAGE_CODE],
        )
    )

    locale = AZURE_LOCALE_BY_LANGUAGE.get(
        language_code,
        AZURE_LOCALE_BY_LANGUAGE[DEFAULT_LANGUAGE_CODE],
    )

    escaped_text = html.escape(cleaned, quote=False)

    ssml = (
        f"<speak version='1.0' xml:lang='{locale}'>"
        f"<voice name='{selected_voice}'>"
        f"{escaped_text}"
        f"</voice>"
        f"</speak>"
    )

    endpoint = (
        f"https://{AZURE_SPEECH_REGION}."
        "tts.speech.microsoft.com/cognitiveservices/v1"
    )

    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "raw-16khz-16bit-mono-pcm",
        "User-Agent": "TelicallVoiceAI",
    }

    def _request_sync():
        import requests
        return requests.post(
            endpoint,
            headers=headers,
            data=ssml.encode("utf-8"),
            timeout=15,
        )

    try:
        started = time.perf_counter()
        response = await asyncio.to_thread(_request_sync)

        if response.status_code != 200:
            print(
                f"❌ [AZURE TTS ERROR] "
                f"status={response.status_code} | "
                f"body={response.text[:300]!r}"
            )
            return b""

        pcm = bytes(response.content or b"")
        if len(pcm) % 2:
            pcm = pcm[:-1]

        elapsed = time.perf_counter() - started
        audio_seconds = (
            len(pcm) / PCM_BYTES_PER_SECOND
            if pcm
            else 0.0
        )

        print(
            f"⚡ [AZURE TTS READY] "
            f"language={language_code} | "
            f"voice={selected_voice} | "
            f"pcm={len(pcm)} | "
            f"audio={audio_seconds:.2f}s | "
            f"time={elapsed:.2f}s"
        )

        return pcm

    except asyncio.CancelledError:
        print("🛑 [AZURE TTS REQUEST CANCELLED]")
        raise

    except Exception as e:
        print(
            f"❌ [AZURE TTS EXCEPTION] "
            f"{type(e).__name__}: {e}"
        )
        return b""


async def generate_custom_voice_pcm_bytes(text: str, settings: dict) -> bytes:
    """Custom voice integration point. Safely uses SAPI until a cloning engine is connected."""
    name = str(settings.get("custom_voice_name", "") or "").strip()
    samples = settings.get("sample_paths", []) or []
    print(f"🎙️ [CUSTOM VOICE SELECTED] name={name or '<unnamed>'} | samples={len(samples)}")
    for i, path in enumerate(samples, 1):
        print(f"🎧 [CUSTOM VOICE SAMPLE {i}] {path}")
    print("⚠️ [CUSTOM VOICE ENGINE NOT CONNECTED] Using SAPI fallback so the live call remains operational.")
    return await generate_sapi_pcm_bytes(
        text,
        voice_name=SAPI_VOICE,
        rate=settings.get("rate", SAPI_RATE),
        volume=settings.get("volume", SAPI_VOLUME),
    )


async def generate_gemini_pcm_bytes(
    text: str,
    language_code: str = DEFAULT_LANGUAGE_CODE,
    voice_name: str = "",
) -> bytes:
    """Generate mono PCM16 at the call pipeline's 16 kHz sample rate."""
    cleaned = _sanitize_tts_text(text)
    if not cleaned:
        return b""

    api_key = (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )
    if not api_key:
        print("[GEMINI TTS ERROR] GEMINI_API_KEY/GOOGLE_API_KEY is not configured.")
        return b""

    language_code = str(language_code or DEFAULT_LANGUAGE_CODE).strip().lower()
    language_name = SUPPORTED_LANGUAGES.get(
        language_code, SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE_CODE]
    )
    model = os.getenv(
        "BRAINEX_GEMINI_TTS_MODEL", "gemini-3.8-flash-tts"
    ).strip()
    voice = (
        str(voice_name or "").strip()
        or os.getenv("BRAINEX_GEMINI_TTS_VOICE", "Kore").strip()
        or "Kore"
    )

    def _generate_sync():
        from google import genai
        from scipy.signal import resample_poly
        import math

        client = genai.Client(api_key=api_key)
        response = client.interactions.create(
            model=model,
            input=[{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": cleaned,
                    "annotations": [{
                        "type": "speech_metadata",
                        "style": f"Speak clearly and naturally in {language_name}.",
                    }],
                }],
            }],
            response_format={"type": "audio"},
            generation_config={"speech_config": [{"voice": voice}]},
        )

        output_audio = getattr(response, "output_audio", None)
        encoded_audio = getattr(output_audio, "data", None)
        if not encoded_audio:
            raise RuntimeError("Gemini response did not contain audio data.")

        wav_bytes = base64.b64decode(encoded_audio)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            source_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            pcm = wav_file.readframes(frame_count)

        if sample_width != 2:
            raise RuntimeError(
                f"Expected 16-bit Gemini audio; received {sample_width * 8}-bit."
            )
        if channels < 1 or source_rate < 1 or not pcm:
            raise RuntimeError("Gemini returned an empty or invalid WAV file.")

        samples = np.frombuffer(pcm, dtype="<i2")
        if channels > 1:
            usable = (len(samples) // channels) * channels
            samples = samples[:usable].reshape(-1, channels).astype(np.int32)
            samples = np.rint(samples.mean(axis=1)).astype(np.int16)

        if source_rate != PCM_SAMPLE_RATE:
            divisor = math.gcd(source_rate, PCM_SAMPLE_RATE)
            samples = resample_poly(
                samples,
                PCM_SAMPLE_RATE // divisor,
                source_rate // divisor,
            )
            samples = np.clip(
                np.rint(samples), -32768, 32767
            ).astype(np.int16)

        result = samples.astype("<i2", copy=False).tobytes()
        print(
            f"[GEMINI TTS READY] language={language_code} | model={model} "
            f"| source_rate={source_rate} | output_rate={PCM_SAMPLE_RATE} "
            f"| pcm_bytes={len(result)}"
        )
        return result

    try:
        started = time.perf_counter()
        pcm = await asyncio.to_thread(_generate_sync)
        print(
            f"[GEMINI TTS COMPLETE] duration={time.perf_counter() - started:.2f}s"
        )
        return pcm
    except Exception as exc:
        # Do not log the API key or request headers.
        print(f"[GEMINI TTS ERROR] {type(exc).__name__}: {exc}")
        return b""


async def generate_voice_pcm_bytes(
    text: str,
    voice_settings: Optional[dict] = None,
    language_code: str = DEFAULT_LANGUAGE_CODE,
) -> bytes:
    settings = voice_settings or {}
    default_engine = (
        "sapi" if sys.platform == "win32" else "azure"
    )
    engine = str(
        settings.get("engine", TTS_ENGINE)
        or TTS_ENGINE
        or default_engine
    ).strip().lower()

    if sys.platform != "win32" and engine in ("sapi", "custom"):
        print(
            f"[TTS PLATFORM] Engine '{engine}' requires Windows; "
            "using Azure TTS on this platform."
        )
        engine = "azure"

    voice_name = str(
        settings.get("voice_name", SAPI_VOICE)
        or ""
    ).strip()

    language_code = str(
        language_code or DEFAULT_LANGUAGE_CODE
    ).strip().lower()

    if language_code not in SUPPORTED_LANGUAGES:
        language_code = DEFAULT_LANGUAGE_CODE

    try:
        rate = max(
            -10,
            min(
                10,
                int(settings.get("rate", SAPI_RATE)),
            ),
        )
    except (TypeError, ValueError):
        rate = SAPI_RATE

    try:
        volume = max(
            0,
            min(
                100,
                int(settings.get("volume", SAPI_VOLUME)),
            ),
        )
    except (TypeError, ValueError):
        volume = SAPI_VOLUME

    settings["rate"] = rate
    settings["volume"] = volume

    if engine == "gemini":
        gemini_voice = str(
            settings.get("gemini_voice_name", "")
            or settings.get("voice_name", "")
            or ""
        ).strip()
        print(
            f"[AI VOICE SETTINGS] engine=gemini | language={language_code} "
            f"| voice={gemini_voice or os.getenv('BRAINEX_GEMINI_TTS_VOICE', 'Kore')}"
        )
        pcm = await generate_gemini_pcm_bytes(
            text,
            language_code=language_code,
            voice_name=gemini_voice,
        )
        if pcm:
            return pcm

        print("[GEMINI TTS FALLBACK] Gemini returned no audio.")
        if AZURE_SPEECH_KEY and AZURE_SPEECH_REGION:
            engine = "azure"
        elif sys.platform == "win32":
            engine = "sapi"
        else:
            return b""

    if engine == "custom":
        return await generate_custom_voice_pcm_bytes(
            text,
            settings,
        )

    if engine == "azure":
        azure_voice = AZURE_VOICE_BY_LANGUAGE.get(
            language_code,
            AZURE_VOICE_BY_LANGUAGE[DEFAULT_LANGUAGE_CODE],
        )

        print(
            f"🔊 [AI VOICE SETTINGS] "
            f"engine=azure | "
            f"language={language_code} | "
            f"voice={azure_voice}"
        )

        pcm = await generate_azure_pcm_bytes(
            text,
            language_code=language_code,
            voice_name=azure_voice,
        )

        if pcm:
            return pcm

        print("[AZURE TTS ERROR] Azure returned no PCM.")
        if sys.platform != "win32":
            return b""

        print(
            "⚠️ [AZURE TTS FALLBACK] "
            "Azure returned no PCM; falling back to Windows SAPI."
        )

    print(
        f"🔊 [AI VOICE SETTINGS] "
        f"engine=sapi | "
        f"language={language_code} | "
        f"voice={voice_name or '<Windows Default>'} | "
        f"rate={rate} | volume={volume}"
    )

    return await generate_sapi_pcm_bytes(
        text,
        voice_name=voice_name,
        rate=rate,
        volume=volume,
    )



# ---------------------------------------------------------------------------
# CONTROLLED TELECALLING CONVERSATION
# ---------------------------------------------------------------------------

CONVERSATION_STAGES = {
    "WAIT_PERMISSION",
    "PROFILE_COLLECTION",
    "NEED_DISCOVERY",
    "PRODUCT_EXPLANATION",
    "DETAIL_COLLECTION",
    "INTEREST_CHECK",
    "FOLLOW_UP",
    "CLOSING",
    "FINISHED",
}

LEAD_OUTCOMES = {
    "UNDECIDED",
    "NOT_INTERESTED",
    "INTERESTED",
    "NEED_MORE_INFORMATION",
    "READY_TO_JOIN",
    "CALLBACK_REQUESTED",
}

PROFILE_KEYS = (
    "customer_type",
    "education",
    "occupation",
    "interest_area",
    "course_interest",
)


def _compact_text(value, limit=500):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


def _extract_json_object(raw_text):
    """Best-effort JSON extraction for local Llama output."""
    raw_text = str(raw_text or "").strip()
    if not raw_text:
        return None

    try:
        parsed = json.loads(raw_text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _bool_value(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"true", "yes", "1"}:
            return True
        if value in {"false", "no", "0"}:
            return False
    return default


# ---------------------------------------------------------------------------
# MEDIA STREAM CONSUMER
# ---------------------------------------------------------------------------

class MediaStreamConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        try:
            await self.accept()

            self.is_connected = True
            self.call_is_active = False
            self.greeting_sent = False

            # Initial greeting strategy:
            # - Wait for customer speech after the call is answered.
            # - Neutral openings such as "hello"/"hi"/"yes"/"ok" do NOT decide language.
            # - First meaningful supported utterance chooses the first greeting language.
            # - If the customer only repeats neutral openings 3 times, stop waiting and
            #   greet in the default language (English).
            self.greeting_neutral_opening_count = 0
            self.greeting_neutral_opening_limit = 3
            self.greeting_language = DEFAULT_LANGUAGE_CODE
            self.greeting_trigger_reason = ""

            self.is_ai_speaking = False
            self.is_tts_generating = False

            self.client_phone = None
            self.session_id = None
            self.lead_details = ""
            self.call_transcript_log = []
            self.start_time = time.time()

            # ----------------------------
            # Customer language
            # ----------------------------
            # English is only the safe initial state before the customer speaks.
            # Whisper updates this automatically after each supported utterance.
            self.detected_language = DEFAULT_LANGUAGE_CODE
            self.detected_language_name = SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE_CODE]
            self.language_probability = 0.0

            # STT language decision state.
            # We target 3 RELIABLE meaningful, language-bearing customer turns.
            # Neutral words and low-confidence Whisper detections do not count.
            # If the first 3 reliable turns conflict, learning continues until a
            # clear winner exists instead of locking the wrong language.
            self.language_final_locked = False
            self.language_evidence_count = 0
            self.language_lock_after_turns = 3
            self.language_scores = {
                code: 0.0
                for code in SUPPORTED_LANGUAGES

            }
            self.language_observations = []

            # Controlled telecalling state. Brainex is the current reference
            # configuration, but the state machine itself is company-agnostic.
            self.conversation_stage = "WAIT_PERMISSION"
            self.conversation_history = []
            self.customer_profile = {
                "customer_type": None,
                "education": None,
                "occupation": None,
                "interest_area": None,
                "course_interest": None,
                "interest_status": "UNDECIDED",
                "needs_more_information": False,
                "ready_to_join": False,
                "callback_requested": False,
            }
            self.final_call_report = None

            # Future multi-tenant/device fields. Current Brainex setup can
            # operate without them; later TelicallLine/device auth can bind
            # these values without changing the AI engine.
            self.company_id = None
            self.telicall_line_id = None
            self.device_token = None

            self.customer_pcm = bytearray()
            self.ai_pcm = bytearray()

            # ----------------------------
            # Customer VAD / endpointing
            # ----------------------------
            self.audio_buffer = bytearray()
            self.pre_roll_buffer = bytearray()
            self.silence_start_time = None
            self.is_user_talking = False

            # Cellular speech endpointing.
            # 16 kHz mono PCM16 = 32,000 bytes/sec.
            # The old MAX_BUFFER_BYTES=64,000 forcibly split speech every
            # ~2 seconds, which badly damaged sentence-level transcription.
            self.ENERGY_THRESHOLD = 300
            self.SILENCE_DURATION_SEC = 0.50
            self.MAX_BUFFER_BYTES = 256000   # ~8 seconds maximum utterance
            self.PRE_ROLL_MAX_BYTES = 12000  # ~375 ms pre-roll

            # ----------------------------
            # Inactivity
            # ----------------------------
            self.INACTIVITY_TIMEOUT_SECONDS = 180.0
            self.last_activity_time = time.time()

            # ----------------------------
            # STT remains serialized
            # ----------------------------
            self.stt_processing_lock = asyncio.Lock()

            # ----------------------------
            # Latest-turn-wins state
            # ----------------------------
            self.turn_generation = 0
            self.current_ai_task: Optional[asyncio.Task] = None
            self.greeting_task: Optional[asyncio.Task] = None
            self.last_customer_text = ""

            # Track general background tasks for clean disconnect.
            self.background_tasks = set()

            self.timeout_checker_task = self.create_background_task(
                self.monitor_inactivity_timeout()
            )

            print(
                "🌐 [WS CONNECT] Realtime latest-turn-wins pipeline established."
            )
            print(f"⚡ [TTS MODE] {TTS_ENGINE}")

        except Exception as e:
            print(f"❌ [WS CONNECT ERROR] {type(e).__name__}: {e}")
            self.is_connected = False
            await self.close()

    # ------------------------------------------------------------------
    # TASK HELPERS
    # ------------------------------------------------------------------

    def create_background_task(self, coro):
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def cancel_task(self, task: Optional[asyncio.Task], label: str):
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            print(f"🛑 [{label} CANCELLED]")
        except Exception as e:
            print(f"⚠️ [{label} CANCEL ERROR] {type(e).__name__}: {e}")

    async def cancel_current_ai(self, reason: str, notify_client: bool = True):
        """
        Cancel generation/playback for the old turn.

        Incrementing turn_generation also invalidates any stale code path that
        happens to finish after cancellation.
        """
        self.turn_generation += 1

        task = self.current_ai_task
        self.current_ai_task = None

        if task is not None and not task.done():
            print(f"🛑 [BARGE-IN] Cancelling old AI turn | reason={reason}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"⚠️ [AI CANCEL ERROR] {type(e).__name__}: {e}")

        self.is_ai_speaking = False
        self.is_tts_generating = False

        if notify_client and self.is_connected:
            # Existing clients can safely ignore this JSON event.
            # Once Flutter handles it, it can flush/stop native playback too.
            await self.safe_send({
                "event": "stop_ai_audio",
                "type": "stop_ai_audio",
                "reason": reason,
            })


    def _normalize_turn_text(self, text: str) -> str:
        # Unicode-safe so Malayalam/Tamil/Hindi/Arabic are preserved.
        return re.sub(
            r"[^\w\s]",
            "",
            str(text or "").lower(),
            flags=re.UNICODE,
        ).strip()

    def _is_permission_turn(self, text: str):
        normalized = self._normalize_turn_text(text)

        allow = {
            # English
            "yes", "yeah", "yep", "okay", "ok", "sure",
            "go ahead", "please tell me", "tell me",
            "continue", "you can", "please continue",
            # Malayalam
            "അതെ", "ശരി", "പറയൂ", "തുടരൂ",
            # Tamil
            "ஆம்", "ஆமாம்", "சரி", "சொல்லுங்கள்", "தொடருங்கள்",
            # Hindi
            "हाँ", "हां", "ठीक है", "बताइए", "बताओ", "जारी रखें",
            # Arabic
            "نعم", "حسنا", "حسنًا", "تفضل", "تابع",
        }

        refuse = {
            # English
            "no", "no thanks", "no thank you",
            "not interested", "stop", "dont call", "do not call",
            # Malayalam
            "വേണ്ട", "താൽപര്യമില്ല", "വിളിക്കേണ്ട", "നിർത്തൂ",
            # Tamil
            "வேண்டாம்", "ஆர்வம் இல்லை", "அழைக்க வேண்டாம்", "நிறுத்துங்கள்",
            # Hindi
            "नहीं", "रुचि नहीं", "फोन मत करना", "बंद करें",
            # Arabic
            "لا", "غير مهتم", "لا تتصل", "توقف",
        }

        if normalized in allow:
            return "ALLOW"

        if normalized in refuse:
            return "REFUSE"

        return None

    def _is_low_value_turn(self, text: str) -> bool:
        normalized = self._normalize_turn_text(text)

        return normalized in {
            "hello", "hi", "hey", "hii",
            "hmm", "hm", "uh", "um",
        }

    async def submit_user_turn(self, user_text: str):
        """
        Latest MEANINGFUL turn wins.

        Valid new customer questions/statements can supersede an older AI turn.
        But low-value greetings like "Hello" must not repeatedly cancel an
        important permission answer such as "Okay" while Llama is thinking.
        """
        user_text = (user_text or "").strip()

        if (
            not user_text
            or not self.is_connected
            or not self.call_is_active
        ):
            return

        active_task = (
            self.current_ai_task is not None
            and not self.current_ai_task.done()
        )

        pending_text = str(
            getattr(self, "last_customer_text", "") or ""
        ).strip()

        incoming_permission = self._is_permission_turn(user_text)
        pending_permission = self._is_permission_turn(pending_text)

        if active_task:
            # Protect a pending YES/OK/NO permission decision from a later
            # telephone greeting while the model is still generating.
            if (
                pending_permission is not None
                and self._is_low_value_turn(user_text)
                and incoming_permission is None
            ):
                print(
                    "🛡️ [TURN PROTECTED] "
                    f"pending='{pending_text}' | ignored='{user_text}'"
                )
                return

            # Do not let greetings repeatedly supersede greetings.
            if (
                self._is_low_value_turn(pending_text)
                and self._is_low_value_turn(user_text)
            ):
                print(
                    "🛡️ [LOW-VALUE TURN IGNORED] "
                    f"pending='{pending_text}' | incoming='{user_text}'"
                )
                return

            # Exact duplicate while its AI turn is still running.
            if (
                self._normalize_turn_text(user_text)
                == self._normalize_turn_text(pending_text)
            ):
                print(
                    f"🛡️ [DUPLICATE TURN IGNORED] '{user_text}'"
                )
                return

        # Meaningful validated customer speech still supersedes the old turn.
        await self.cancel_current_ai(
            reason=f"new_customer_turn:{user_text[:40]}",
            notify_client=True,
        )

        my_generation = self.turn_generation
        self.last_customer_text = user_text
        self.last_activity_time = time.time()

        task = asyncio.create_task(
            self.process_text_inference(user_text, my_generation)
        )

        self.current_ai_task = task
        self.background_tasks.add(task)

        def _done(t):
            self.background_tasks.discard(t)

            if self.current_ai_task is t:
                self.current_ai_task = None

        task.add_done_callback(_done)

    # ------------------------------------------------------------------
    # TIMEOUT
    # ------------------------------------------------------------------

    async def monitor_inactivity_timeout(self):
        try:
            while self.is_connected:
                await asyncio.sleep(5)

                if not self.call_is_active:
                    return

                # Never kill a session merely because the AI is actively
                # generating/speaking.
                ai_busy = (
                    self.is_ai_speaking
                    or self.is_tts_generating
                    or (
                        self.current_ai_task is not None
                        and not self.current_ai_task.done()
                    )
                )
                if ai_busy:
                    continue

                if (
                    time.time() - self.last_activity_time
                    > self.INACTIVITY_TIMEOUT_SECONDS
                ):
                    print("⏰ [TIMEOUT] Genuine inactive call threshold hit.")
                    await self.safe_send({
                        "event": "ai_token",
                        "text": "[SESSION TIMEOUT]",
                    })
                    await self.close()
                    return

        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # DISCONNECT
    # ------------------------------------------------------------------

    async def disconnect(self, close_code):
        print(f"🔌 [WS DISCONNECT] Code: {close_code}")

        self.is_connected = False
        self.call_is_active = False
        self.greeting_sent = False

        # Invalidate any old AI/TTS generation.
        self.turn_generation += 1

        # Stop inactivity watcher.
        if self.timeout_checker_task:
            self.timeout_checker_task.cancel()
            self.timeout_checker_task = None

        # Cancel dedicated AI/greeting tasks first.
        tasks_to_cancel = []

        for task in [self.current_ai_task, self.greeting_task]:
            if task is not None and not task.done():
                task.cancel()
                tasks_to_cancel.append(task)

        # Cancel every remaining tracked task except ourselves.
        current = asyncio.current_task()

        for task in list(self.background_tasks):
            if (
                task is not current
                and not task.done()
                and task not in tasks_to_cancel
            ):
                task.cancel()
                tasks_to_cancel.append(task)

        if tasks_to_cancel:
            print(
                f"🛑 [TASK CLEANUP] Cancelling "
                f"{len(tasks_to_cancel)} background task(s)..."
            )

            try:
                await asyncio.gather(
                    *tasks_to_cancel,
                    return_exceptions=True,
                )
            except Exception as e:
                print(
                    f"⚠️ [TASK CLEANUP ERROR] "
                    f"{type(e).__name__}: {e}"
                )

        self.background_tasks.clear()
        self.current_ai_task = None
        self.greeting_task = None

        duration = max(
            0.0,
            time.time() - self.start_time,
        )

        # ------------------------------------------------------------------
        # CRITICAL:
        # Save status + BOTH WAV recordings immediately.
        #
        # Do this BEFORE post-call Llama analysis. Daphne can kill a slow
        # disconnect handler, so recordings must already be persisted first.
        # ------------------------------------------------------------------
        if self.session_id:
            try:
                await self.finalize_call_session(
                    self.session_id,
                    duration,
                    self.call_transcript_log,
                    None,
                )

                print(
                    f"💾 [IMMEDIATE CALL SAVE COMPLETE] "
                    f"ID={self.session_id} | "
                    f"customer_pcm={len(self.customer_pcm)} bytes | "
                    f"ai_pcm={len(self.ai_pcm)} bytes"
                )

            except Exception as e:
                print(
                    f"❌ [IMMEDIATE CALL SAVE ERROR] "
                    f"{type(e).__name__}: {e}"
                )

        # ------------------------------------------------------------------
        # Slow post-call analysis comes AFTER the recordings are safe.
        # If Daphne cancels this section, status/audio remain saved.
        # ------------------------------------------------------------------
        if self.session_id:
            try:
                self.final_call_report = await self.analyze_completed_call()

                await self.save_sales_insight(
                    self.session_id,
                    self.final_call_report,
                )

                print(
                    f"✅ [POST-CALL REPORT SAVED] "
                    f"ID={self.session_id} | "
                    f"outcome={self.final_call_report.get('outcome')}"
                )

            except asyncio.CancelledError:
                print(
                    "⚠️ [POST-CALL ANALYSIS CANCELLED] "
                    "CallSession status and recordings are already saved."
                )

            except Exception as e:
                print(
                    f"❌ [POST-CALL REPORT ERROR] "
                    f"{type(e).__name__}: {e}"
                )
                print(
                    "✅ [CALL DATA SAFE] "
                    "CallSession status and recordings are already saved."
                )

        try:
            self.audio_buffer.clear()
        except Exception:
            pass

        try:
            self.pre_roll_buffer.clear()
        except Exception:
            pass

        print("✅ [WS CLEANUP COMPLETE]")

    # ------------------------------------------------------------------
    # RECEIVE
    # ------------------------------------------------------------------

    async def receive(self, text_data=None, bytes_data=None):

        if text_data is not None:
          print("📨 [WS TEXT RX] "f"length={len(text_data)}")
        # Raw audio means the transport/call is still alive.
        if bytes_data is not None:
            self.last_activity_time = time.time()
            await self.handle_customer_pcm(bytes_data)
            return

        if text_data is None:
            return

        self.last_activity_time = time.time()

        try:
            parsed_json = json.loads(text_data)

        except json.JSONDecodeError:
            clean_text = text_data.strip()
            if (
                clean_text
                and not clean_text.startswith("{")
                and clean_text not in {
                    "HELLO_SERVER",
                    "__SYSTEM_CONNECTION_INITIALIZED__",
                }
            ):
                await self.submit_user_turn(clean_text)
            return

        # Legacy/base64 customer audio.
        if "audio" in parsed_json:
            try:
                import base64

                decoded_audio = base64.b64decode(
                    parsed_json["audio"],
                    validate=True,
                )
                print(
                    f"📦 [BASE64 AUDIO DECODED] bytes={len(decoded_audio)}"
                )
                await self.handle_customer_pcm(decoded_audio)
            except Exception as e:
                print(
                    f"❌ [BASE64 AUDIO ERROR] "
                    f"{type(e).__name__}: {e}"
                )
            return

        event = str(parsed_json.get("event", "") or "").strip().lower()

        # Metadata binds websocket to one DB call/session.
        if (
            "client_phone_number" in parsed_json
            or event == "client_phone_number"
        ):
            self.client_phone = parsed_json.get("client_phone_number")
            self.lead_details = parsed_json.get(
                "lead_details",
                parsed_json.get("details", ""),
            )

            # Optional future-ready identifiers for Company -> TelicallLine
            # -> dedicated device binding. They are harmless in the current
            # Brainex-only database.
            self.company_id = parsed_json.get("company_id", self.company_id)
            self.telicall_line_id = parsed_json.get(
                "telicall_line_id",
                parsed_json.get("line_id", self.telicall_line_id),
            )
            self.device_token = parsed_json.get(
                "device_token",
                self.device_token,
            )

            if self.session_id is None:
                self.session_id = await self.create_call_session(
                    self.client_phone
                )

            print(
                f"📱 [METADATA BOUND] Phone={self.client_phone} | "
                f"DB ID={self.session_id}"
            )

            # Metadata only binds this websocket to the DB/session.
            # The outbound cellular call may still be dialing or ringing.
            # Do NOT start the greeting here.
            return

        if event == "call_answered":
            self.call_is_active = True
            print(
                "✅ [CALL ANSWER CONFIRMED] "
                "Waiting for the customer's first meaningful voice before greeting."
            )
            return

        if event == "call_state_changed":
            raw_state = str(parsed_json.get("state", "") or "").strip()
            state = raw_state.upper()

            print(f"📞 [CALL STATE] {state}")

            active_states = {"ACTIVE", "4", "ANSWERED"}
            ended_states = {
                "DISCONNECTED",
                "DISCONNECTING",
                "IDLE",
                "7",
                "10",
                "ENDED",
            }

            if state in active_states:
                self.call_is_active = True

                if not self.greeting_sent:
                    print(
                        "✅ [CALL ANSWER CONFIRMED] "
                        "Waiting for the customer's first meaningful voice before greeting."
                    )

            elif state == "OFFHOOK":
                # OFFHOOK can occur while an outgoing call is still dialing/
                # alerting on some Android devices. Hold the greeting.
                print(
                    "⏳ [CALL NOT ANSWERED YET] "
                    "OFFHOOK received; greeting is waiting for ACTIVE."
                )

            elif state in ended_states:
                self.call_is_active = False
                print("🛑 [REMOTE CALL ENDED] Cancelling AI immediately.")
                await self.cancel_current_ai(
                    reason="call_disconnected",
                    notify_client=True,
                )
                await self.close()
            return

        if event in {"call_ended", "call_disconnected", "disconnect_call"}:
            self.call_is_active = False
            print("🛑 [CALL END EVENT] Cancelling AI immediately.")
            await self.cancel_current_ai(
                reason=event,
                notify_client=True,
            )
            await self.close()
            return

        user_text = str(
            parsed_json.get(
                "text",
                parsed_json.get(
                    "message",
                    parsed_json.get("prompt", ""),
                ),
            )
            or ""
        ).strip()

        if user_text and user_text not in {
            "HELLO_SERVER",
            "__SYSTEM_CONNECTION_INITIALIZED__",
        }:
            await self.submit_user_turn(user_text)

    # ------------------------------------------------------------------
    # CUSTOMER PCM / VAD
    # ------------------------------------------------------------------

    async def handle_customer_pcm(self, bytes_data):
        if bytes_data is None:
            return

        if not isinstance(bytes_data, (bytes, bytearray, memoryview)):
            print(
                f"❌ [PCM TYPE ERROR] Unsupported type: "
                f"{type(bytes_data).__name__}"
            )
            return

        frame = bytes(bytes_data)
        if not frame:
            return

        if len(frame) % 2:
            frame = frame[:-1]
        if not frame:
            return

        self.customer_pcm.extend(frame)

        if not self.is_connected or not self.call_is_active:
            return

        audio_frame = np.frombuffer(frame, dtype=np.int16)
        if audio_frame.size == 0:
            return

        rms_energy = float(
            np.sqrt(np.mean(audio_frame.astype(np.float32) ** 2))
        )
        now = time.time()
        speech_frame = rms_energy >= self.ENERGY_THRESHOLD

        if not self.is_user_talking:
            self.pre_roll_buffer.extend(frame)
            if len(self.pre_roll_buffer) > self.PRE_ROLL_MAX_BYTES:
                del self.pre_roll_buffer[
                    :len(self.pre_roll_buffer) - self.PRE_ROLL_MAX_BYTES
                ]

            if speech_frame:
                self.is_user_talking = True
                self.silence_start_time = None
                self.audio_buffer.clear()
                self.audio_buffer.extend(self.pre_roll_buffer)
                self.pre_roll_buffer.clear()

                print(
                    f"🟢 [SPEECH START] rms={rms_energy:.2f} | "
                    f"buffer={len(self.audio_buffer)}"
                )

            else:

                return

        else:
            self.audio_buffer.extend(frame)

        if speech_frame:
            self.silence_start_time = None
        elif self.silence_start_time is None:
            self.silence_start_time = now

        silence_duration = (
            now - self.silence_start_time
            if self.silence_start_time is not None
            else 0.0
        )

        # Keep console readable: do not print every 2048-byte PCM frame.
        # Show only occasional speech diagnostics.
        if len(self.audio_buffer) % 16384 < len(frame):
            print(
                f"🎤 [SPEECH ACTIVE] "
                f"buffer={len(self.audio_buffer)} | "
                f"rms={rms_energy:.0f} | "
                f"silence={silence_duration:.2f}s"
            )

        should_dispatch = (
            silence_duration >= self.SILENCE_DURATION_SEC
            or len(self.audio_buffer) >= self.MAX_BUFFER_BYTES
        )

        if should_dispatch:
            raw_buffer = bytes(self.audio_buffer)

            self.audio_buffer.clear()
            self.pre_roll_buffer.clear()
            self.is_user_talking = False
            self.silence_start_time = None

            if len(raw_buffer) <= 4000:
                return

            print(
                f"🎙️ [SPEECH CHUNK READY] Processing "
                f"{len(raw_buffer)} bytes..."
            )
            self.create_background_task(
                self.safe_process_audio_transcription(raw_buffer)
            )

    # ------------------------------------------------------------------
    # STT
    # ------------------------------------------------------------------

    async def safe_process_audio_transcription(self, raw_audio_bytes):
        async with self.stt_processing_lock:

            if not self.is_connected or not self.call_is_active:
                return

            try:
                await self.process_audio_transcription(
                    raw_audio_bytes
                )

            except asyncio.CancelledError:
                print("⚠️ [STT TASK CANCELLED]")
                raise

            except Exception as e:
                print(
                    f"❌ [STT PIPELINE ERROR] "
                    f"{type(e).__name__}: {e}"
                )


    async def process_audio_transcription(self, raw_audio_bytes):
        try:
            start = time.perf_counter()

            print(
                f"🧠 [STT START] "
                f"PCM bytes={len(raw_audio_bytes)} | "
                f"ai_speaking={self.is_ai_speaking}"
            )

            # ==========================================================
            # 1. RAW PCM -> INT16
            # ==========================================================

            pcm_int16 = np.frombuffer(
                raw_audio_bytes,
                dtype=np.int16,
            )

            if pcm_int16.size == 0:
                print("⚠️ [STT EMPTY AUDIO]")
                return

            audio_seconds = (
                len(pcm_int16)
                / float(PCM_SAMPLE_RATE)
            )

            # ==========================================================
            # 2. AUDIO ENERGY
            # ==========================================================

            pcm_float = pcm_int16.astype(np.float32)

            rms = float(
                np.sqrt(
                    np.mean(
                        pcm_float ** 2
                    )
                )
            )

            peak_int16 = int(
                np.max(
                    np.abs(
                        pcm_float
                    )
                )
            )

            print(
                f"🧠 [STT INPUT] "
                f"duration={audio_seconds:.2f}s | "
                f"rms={rms:.2f} | "
                f"peak={peak_int16}"
            )

            # ==========================================================
            # 3. EARLY NOISE / SILENCE FILTER
            # ==========================================================

            STT_MIN_RMS = 180.0
            STT_MIN_DURATION = 0.35

            if audio_seconds < STT_MIN_DURATION:
                print(
                    f"⚠️ [STT SKIP TOO SHORT] "
                    f"duration={audio_seconds:.2f}s"
                )
                return

            if rms < STT_MIN_RMS:
                print(
                    f"⚠️ [STT SKIP LOW ENERGY] "
                    f"rms={rms:.2f} | "
                    f"threshold={STT_MIN_RMS:.0f}"
                )
                return

            # ==========================================================
            # 4. INT16 -> FLOAT32
            # ==========================================================

            audio_float32 = (
                pcm_int16.astype(np.float32)
                / 32768.0
            )

            max_peak = float(
                np.max(
                    np.abs(audio_float32)
                )
            )

            print(
                f"🧠 [STT AUDIO] "
                f"samples={len(pcm_int16)} | "
                f"duration={audio_seconds:.2f}s | "
                f"peak={max_peak:.4f} | "
                f"rms={rms:.2f}"
            )

            # ==========================================================
            # 5. QUIET SPEECH GAIN
            # ==========================================================

            if 0.01 <= max_peak < 0.08:

                target_peak = 0.18

                gain = min(
                    3.0,
                    target_peak / max_peak,
                )

                audio_float32 = np.clip(
                    audio_float32 * gain,
                    -1.0,
                    1.0,
                )

                print(
                    f"🔉 [STT QUIET AUDIO GAIN] "
                    f"gain={gain:.2f}x"
                )

            # ==========================================================
            # 6. STT LANGUAGE MODE - 5 TURN LEARNING WINDOW
            #
            # BEFORE final lock:
            #     language=None
            #     Whisper performs one automatic-language transcription.
            #
            # AFTER 3 meaningful language-bearing turns:
            #     language=<locked language>
            #     No more automatic language detection for this call.
            #
            # This avoids repeated language-decision work and prevents common
            # words such as yes/ok/hello from changing the call language.
            # ==========================================================

            language_final_locked = bool(
                getattr(
                    self,
                    "language_final_locked",
                    False,
                )
            )

            locked_language = getattr(
                self,
                "detected_language",
                DEFAULT_LANGUAGE_CODE,
            )

            requested_language = (
                locked_language
                if language_final_locked
                else None
            )

            evidence_count = int(
                getattr(
                    self,
                    "language_evidence_count",
                    0,
                )
                or 0
            )

            lock_after = max(
                1,
                int(
                    getattr(
                        self,
                        "language_lock_after_turns",
                        3,
                    )
                    or 3
                ),
            )

            print(
                f"🌐 [STT LANGUAGE MODE] "
                f"mode={'LOCKED' if language_final_locked else 'LEARNING'} | "
                f"requested={requested_language or 'auto'} | "
                f"evidence={evidence_count}/{lock_after} | "
                f"active={locked_language}"
            )

            # ==========================================================
            # 7. LOCAL WHISPER HELPER
            # ==========================================================

            async def _transcribe_once(language_code):
                transcribe_started = time.perf_counter()

                segs, info_obj = await asyncio.to_thread(
                    whisper_model.transcribe,
                    audio_float32,

                    beam_size=1,
                    best_of=1,

                    language=language_code,

                    task="transcribe",

                    condition_on_previous_text=False,

                    # Telephone VAD is already handled before this point.
                    vad_filter=False,

                    temperature=0.0,

                    compression_ratio_threshold=2.2,
                    log_prob_threshold=-1.0,
                    no_speech_threshold=0.65,

                    word_timestamps=False,
                )

                segs = list(segs)

                transcript = "".join(
                    segment.text
                    for segment in segs
                ).strip()

                if language_code is None:
                    lang = str(
                        getattr(
                            info_obj,
                            "language",
                            DEFAULT_LANGUAGE_CODE,
                        )
                        or DEFAULT_LANGUAGE_CODE
                    ).strip().lower()

                    lang_probability = float(
                        getattr(
                            info_obj,
                            "language_probability",
                            0.0,
                        )
                        or 0.0
                    )
                else:
                    # Faster-Whisper was explicitly guided, so the language
                    # used for decoding is the requested language.
                    lang = str(language_code).strip().lower()

                    lang_probability = float(
                        getattr(
                            info_obj,
                            "language_probability",
                            1.0,
                        )
                        or 1.0
                    )

                elapsed = (
                    time.perf_counter()
                    - transcribe_started
                )

                return {
                    "segments": segs,
                    "info": info_obj,
                    "text": transcript,
                    "language": lang,
                    "probability": lang_probability,
                    "elapsed": elapsed,
                    "guided": language_code is not None,
                }

            # ==========================================================
            # 8. TRANSCRIPT QUALITY HELPERS
            # ==========================================================

            def _clean_unicode_text(value):
                return re.sub(
                    r"[^\w\s]",
                    "",
                    str(value or "").lower(),
                    flags=re.UNICODE,
                ).strip()

            def _native_script_count(value, language_code):
                value = str(value or "")

                ranges = {
                    "ml": [(0x0D00, 0x0D7F)],
                    "ta": [(0x0B80, 0x0BFF)],
                    "hi": [(0x0900, 0x097F)],
                    # Arabic letters can occur across several Unicode blocks.
                    "ar": [
                        (0x0600, 0x06FF),
                        (0x0750, 0x077F),
                        (0x08A0, 0x08FF),
                        (0xFB50, 0xFDFF),
                        (0xFE70, 0xFEFF),
                    ],
                }

                language_ranges = ranges.get(language_code)

                if language_ranges is None:
                    return 0

                return sum(
                    1
                    for ch in value
                    if any(
                        low <= ord(ch) <= high
                        for low, high in language_ranges
                    )
                )

            def _looks_like_repetition(value):
                clean_value = _clean_unicode_text(value)

                if not clean_value:
                    return True

                words_local = clean_value.split()

                if (
                    len(words_local) >= 4
                    and len(set(words_local)) == 1
                ):
                    return True

                compact = re.sub(
                    r"\s+",
                    "",
                    clean_value,
                )

                if len(compact) >= 12:
                    counts = {}

                    for char in compact:
                        counts[char] = (
                            counts.get(char, 0)
                            + 1
                        )

                    dominant_ratio = (
                        max(counts.values())
                        / len(compact)
                    )

                    if dominant_ratio >= 0.55:
                        return True

                if (
                    len(compact) >= 20
                    and len(set(compact)) <= 4
                ):
                    return True

                return False

            def _guided_result_needs_auto_check(value, language_code):
                value = str(value or "").strip()

                if not value:
                    return True

                if _looks_like_repetition(value):
                    return True

                clean_value = _clean_unicode_text(value)
                words_local = clean_value.split()

                # For Malayalam/Tamil/Hindi/Arabic, a longer result containing
                # no characters from the active script can be a translation-like
                # hallucination. Code-mixed short replies remain allowed.
                if (
                    language_code in {"ml", "ta", "hi", "ar"}
                    and len(words_local) >= 3
                    and len(clean_value) >= 10
                    and _native_script_count(
                        value,
                        language_code,
                    ) == 0
                ):
                    return True

                return False

            # ==========================================================
            # 9. PRIMARY WHISPER PASS
            # ==========================================================

            result = await _transcribe_once(
                requested_language
            )

            print(
                f"🧠 [WHISPER PASS] "
                f"mode={'GUIDED' if result['guided'] else 'AUTO'} | "
                f"time={result['elapsed']:.2f}s | "
                f"segments={len(result['segments'])} | "
                f"language={result['language']} | "
                f"lang_prob={result['probability']:.2f}"
            )

            # ==========================================================
            # 10. SINGLE-PASS POLICY
            #
            # During the first 3 meaningful language-bearing turns we do NOT
            # re-run Whisper with a language hint. One AUTO pass is enough to
            # gather language evidence. This avoids doubling STT latency.
            #
            # Once the call language is finally locked, requested_language is
            # fixed and Whisper decodes directly in that language. Suspicious
            # guided results are rejected later instead of triggering another
            # automatic-language pass.
            # ==========================================================

            # ==========================================================
            # 12. FINAL RESULT FROM STT PASSES
            # ==========================================================

            segments = result["segments"]
            user_text = result["text"]
            detected_language = result["language"]
            language_probability = result["probability"]

            detected_language_name = (
                SUPPORTED_LANGUAGES.get(
                    detected_language,
                    "Unsupported",
                )
            )

            print(
                f"🧠 [WHISPER DONE] "
                f"{time.perf_counter() - start:.2f}s | "
                f"segments={len(segments)} | "
                f"language={detected_language} | "
                f"lang_prob={language_probability:.2f}"
            )

            print(
                f"🌐 [LANGUAGE DETECTED] "
                f"code={detected_language} | "
                f"name={detected_language_name} | "
                f"confidence={language_probability:.2f}"
            )

            print(
                f"🗣️ [WHISPER RESULT]: "
                f"'{user_text}'"
            )

            # ==========================================================
            # 13. EMPTY RESULT
            # ==========================================================

            if not user_text:
                print(
                    "⚠️ [WHISPER EMPTY RESULT]"
                )
                return

            # ==========================================================
            # 14. REJECT UNSUPPORTED AUTO-DETECTED LANGUAGE
            # ==========================================================

            if (
                detected_language
                not in SUPPORTED_LANGUAGES
            ):
                print(
                    f"🛑 [UNSUPPORTED TRANSCRIPT REJECTED] "
                    f"language={detected_language} | "
                    f"confidence="
                    f"{language_probability:.2f} | "
                    f"text='{user_text[:100]}'"
                )
                return

            # ==========================================================
            # 15. CLEAN TEXT
            # ==========================================================

            clean_check = _clean_unicode_text(
                user_text
            )

            if not clean_check:
                print(
                    f"⚠️ [WHISPER CLEAN RESULT EMPTY]: "
                    f"'{user_text}'"
                )
                return

            words = clean_check.split()

            # ==========================================================
            # 16. KNOWN WHISPER HALLUCINATIONS
            # ==========================================================

            hallucinations = {
                "you",
                "you you",
                "thank you for watching",
                "thanks for watching",
                "subtitles",
                "amaraorg",
                "amara org",
                "mb",
            }

            if clean_check in hallucinations:
                print(
                    f"⚠️ [KNOWN WHISPER HALLUCINATION] "
                    f"'{user_text}'"
                )
                return

            # ==========================================================
            # 17. REPETITION DETECTOR
            # ==========================================================

            compact_text = re.sub(
                r"\s+",
                "",
                clean_check,
            )

            if (
                len(words) >= 4
                and len(set(words)) == 1
            ):
                print(
                    f"⚠️ [WHISPER WORD REPETITION IGNORED] "
                    f"'{user_text[:120]}'"
                )
                return

            if len(compact_text) >= 12:

                char_counts = {}

                for char in compact_text:
                    char_counts[char] = (
                        char_counts.get(char, 0)
                        + 1
                    )

                most_common_count = max(
                    char_counts.values()
                )

                dominant_ratio = (
                    most_common_count
                    / len(compact_text)
                )

                if dominant_ratio >= 0.55:
                    print(
                        f"⚠️ [WHISPER CHARACTER REPETITION "
                        f"IGNORED] "
                        f"ratio={dominant_ratio:.2f} | "
                        f"'{user_text[:120]}'"
                    )
                    return

            if (
                len(compact_text) >= 20
                and len(set(compact_text)) <= 4
            ):
                print(
                    f"⚠️ [WHISPER LOW-DIVERSITY "
                    f"HALLUCINATION] "
                    f"unique_chars="
                    f"{len(set(compact_text))} | "
                    f"'{user_text[:120]}'"
                )
                return

            # ==========================================================
            # 17B. LOCKED-LANGUAGE GUIDED RESULT SAFETY
            #
            # After the final lock we intentionally do not run AUTO language
            # detection again. If a guided result is obviously suspicious,
            # reject it instead of spending another Whisper pass.
            # ==========================================================

            if (
                bool(getattr(self, "language_final_locked", False))
                and requested_language is not None
                and _guided_result_needs_auto_check(
                    user_text,
                    requested_language,
                )
            ):
                print(
                    f"🛑 [LOCKED LANGUAGE SUSPICIOUS RESULT REJECTED] "
                    f"language={requested_language} | "
                    f"text='{user_text[:120]}'"
                )
                return

            # ==========================================================
            # 18. ALLOWED SHORT ENGLISH WORDS
            # ==========================================================

            allowed_single_words = {
                "hello",
                "hi",
                "yes",
                "no",
                "okay",
                "ok",
                "sure",
                "correct",
                "right",
                "wait",
                "repeat",
                "bye",
                "continue",
                "stop",
            }

            # These are common conversational words/phrases used by speakers
            # of many languages. They are valid customer turns, but MUST NOT
            # be used to lock, confirm, or switch the caller language.
            language_neutral_utterances = {
                "hello",
                "hi",
                "hey",
                "hii",
                "yes",
                "yeah",
                "yep",
                "no",
                "okay",
                "ok",
                "sure",
                "right",
                "correct",
                "thanks",
                "thank you",
                "thanks a lot",
                "please",
                "wait",
                "repeat",
                "continue",
                "stop",
                "bye",
                "goodbye",
                "go ahead",
                "tell me",
                "please tell me",
            }

            is_language_neutral_turn = (
                clean_check in language_neutral_utterances
            )

            # ==========================================================
            # 18B. TRANSCRIPT QUALITY MUST PASS BEFORE LANGUAGE EVIDENCE
            # ==========================================================
            # This is intentionally before the language-evidence block. A text
            # that will later be rejected must never be allowed to vote for the
            # customer's permanent language.
            current_active_language = getattr(
                self,
                "detected_language",
                DEFAULT_LANGUAGE_CODE,
            )

            if (
                len(words) == 1
                and current_active_language == "en"
                and clean_check not in allowed_single_words
            ):
                print(
                    f"⚠️ [SHORT ENGLISH RESULT IGNORED] "
                    f"'{user_text}'"
                )
                return

            if (
                language_probability < 0.25
                and len(words) <= 2
            ):
                print(
                    f"⚠️ [LOW CONFIDENCE TRANSCRIPT IGNORED] "
                    f"confidence={language_probability:.2f} | "
                    f"text='{user_text}'"
                )
                return

            # ==========================================================
            # 19. LANGUAGE EVIDENCE + SAFE 3-TURN TARGET LOCK
            # ==========================================================

            current_language = getattr(
                self,
                "detected_language",
                DEFAULT_LANGUAGE_CODE,
            )

            language_final_locked = bool(
                getattr(
                    self,
                    "language_final_locked",
                    False,
                )
            )

            # ----------------------------------------------------------
            # Already locked: never reconsider the language.
            # ----------------------------------------------------------
            if language_final_locked:
                print(
                    f"🔒 [LANGUAGE FINAL LOCK ACTIVE] "
                    f"{self.detected_language_name} "
                    f"({self.detected_language}) | "
                    f"Whisper guided={detected_language}"
                )

            # ----------------------------------------------------------
            # Neutral utterance: valid customer speech, zero evidence.
            # ----------------------------------------------------------
            elif is_language_neutral_turn:
                print(
                    f"🟨 [LANGUAGE NEUTRAL TURN] "
                    f"text='{user_text}' | "
                    f"Whisper={detected_language}/"
                    f"{language_probability:.2f} | "
                    f"evidence remains="
                    f"{self.language_evidence_count}/"
                    f"{self.language_lock_after_turns}"
                )

            # ----------------------------------------------------------
            # Meaningful supported utterance: only RELIABLE Whisper language
            # detections may contribute evidence.
            # ----------------------------------------------------------
            elif language_probability < MIN_LANGUAGE_EVIDENCE_CONFIDENCE:
                print(
                    f"⚠️ [LANGUAGE EVIDENCE REJECTED] "
                    f"heard={detected_language}/"
                    f"{language_probability:.2f} | "
                    f"minimum={MIN_LANGUAGE_EVIDENCE_CONFIDENCE:.2f} | "
                    f"evidence remains="
                    f"{self.language_evidence_count}/"
                    f"{self.language_lock_after_turns} | "
                    f"text='{user_text[:120]}'"
                )

            else:
                score = min(1.0, float(language_probability))

                self.language_scores[detected_language] = (
                    float(
                        self.language_scores.get(
                            detected_language,
                            0.0,
                        )
                    )
                    + score
                )

                self.language_evidence_count += 1

                self.language_observations.append({
                    "turn": self.language_evidence_count,
                    "language": detected_language,
                    "confidence": language_probability,
                    "text": user_text[:120],
                })

                # Provisional leader is useful for logs/UI, but it does NOT
                # change Whisper decoding during the 5-turn learning window.
                provisional_language = max(
                    self.language_scores,
                    key=self.language_scores.get,
                )

                provisional_score = float(
                    self.language_scores.get(
                        provisional_language,
                        0.0,
                    )
                )

                self.detected_language = provisional_language
                self.detected_language_name = SUPPORTED_LANGUAGES[
                    provisional_language
                ]
                self.language_probability = provisional_score / max(
                    1,
                    self.language_evidence_count,
                )

                scores_text = ", ".join(
                    f"{code}={self.language_scores.get(code, 0.0):.2f}"
                    for code in SUPPORTED_LANGUAGES
                )

                print(
                    f"🗳️ [LANGUAGE EVIDENCE] "
                    f"turn={self.language_evidence_count}/"
                    f"{self.language_lock_after_turns} | "
                    f"heard={detected_language}/"
                    f"{language_probability:.2f} | "
                    f"leader={provisional_language} | "
                    f"scores=[{scores_text}]"
                )

                # ------------------------------------------------------
                # Target lock after 3 reliable meaningful turns.
                # A clear winner is required: >=60% of weighted evidence and
                # at least 2 reliable observations for the winning language.
                # If evidence conflicts, stay in learning mode rather than
                # permanently locking the wrong language.
                # ------------------------------------------------------
                if (
                    self.language_evidence_count
                    >= self.language_lock_after_turns
                ):
                    final_language = max(
                        self.language_scores,
                        key=self.language_scores.get,
                    )

                    final_score = float(
                        self.language_scores.get(
                            final_language,
                            0.0,
                        )
                    )

                    total_score = sum(
                        float(v)
                        for v in self.language_scores.values()
                    )

                    final_share = (
                        final_score / total_score
                        if total_score > 0
                        else 0.0
                    )

                    winner_turns = sum(
                        1
                        for obs in self.language_observations
                        if obs.get("language") == final_language
                    )

                    if (
                        final_share >= MIN_LANGUAGE_LOCK_SHARE
                        and winner_turns >= MIN_LANGUAGE_LOCK_WINNER_TURNS
                    ):
                        self.detected_language = final_language
                        self.detected_language_name = SUPPORTED_LANGUAGES[
                            final_language
                        ]
                        self.language_probability = final_share
                        self.language_final_locked = True

                        print(
                            f"🔐 [LANGUAGE FINAL LOCK] "
                            f"{self.detected_language_name} "
                            f"({self.detected_language}) | "
                            f"evidence_turns="
                            f"{self.language_evidence_count} | "
                            f"winner_turns={winner_turns} | "
                            f"score={final_score:.2f} | "
                            f"share={final_share:.2f}"
                        )
                    else:
                        print(
                            f"⏳ [LANGUAGE LOCK DEFERRED] "
                            f"leader={final_language} | "
                            f"winner_turns={winner_turns} | "
                            f"share={final_share:.2f} | "
                            f"need_share>={MIN_LANGUAGE_LOCK_SHARE:.2f} | "
                            f"need_winner_turns>="
                            f"{MIN_LANGUAGE_LOCK_WINNER_TURNS}"
                        )

            # ==========================================================
            # 20. FINAL ACCEPTED CUSTOMER TRANSCRIPT
            # ==========================================================

            print(
                f"✅ [CUSTOMER TRANSCRIPT] "
                f"[{self.detected_language_name}/"
                f"{self.detected_language}] "
                f"Whisper={detected_language}/"
                f"{language_probability:.2f}: "
                f"{user_text}"
            )

            self.call_transcript_log.append(
                f"Customer: {user_text}"
            )

            self.last_activity_time = (
                time.time()
            )

            # ==========================================================
            # 22. CUSTOMER OPENING -> START INITIAL GREETING
            #
            # Rules:
            # 1) Neutral words such as "hello", "hi", "yes", "ok" do NOT
            #    decide the customer's language.
            # 2) The first meaningful supported utterance chooses the language
            #    of the FIRST greeting immediately.
            # 3) The permanent STT language is still decided separately from
            #    the first 3 meaningful language-bearing customer turns.
            # 4) If the customer only repeats neutral opening words 3 times,
            #    stop waiting and greet in the default language (English).
            #
            # Neutral opening repetitions do NOT count toward the 5-turn
            # permanent language evidence window.
            # ==========================================================

            if not self.greeting_sent:
                if (
                    not is_language_neutral_turn
                    and language_probability < MIN_LANGUAGE_EVIDENCE_CONFIDENCE
                ):
                    print(
                        f"👂 [GREETING WAIT] Weak language detection "
                        f"{detected_language}/{language_probability:.2f} | "
                        f"need>={MIN_LANGUAGE_EVIDENCE_CONFIDENCE:.2f}"
                    )
                    return

                if is_language_neutral_turn:
                    self.greeting_neutral_opening_count = int(
                        getattr(
                            self,
                            "greeting_neutral_opening_count",
                            0,
                        )
                        or 0
                    ) + 1

                    neutral_limit = max(
                        1,
                        int(
                            getattr(
                                self,
                                "greeting_neutral_opening_limit",
                                3,
                            )
                            or 3
                        ),
                    )

                    print(
                        f"👂 [GREETING WAIT] "
                        f"Neutral opening heard='{user_text}' | "
                        f"count={self.greeting_neutral_opening_count}/"
                        f"{neutral_limit}"
                    )

                    if self.greeting_neutral_opening_count < neutral_limit:
                        return

                    self.greeting_sent = True
                    self.greeting_language = DEFAULT_LANGUAGE_CODE
                    self.greeting_trigger_reason = "neutral_opening_fallback"

                    print(
                        f"▶️ [GREETING DEFAULT FALLBACK] "
                        f"neutral_openings="
                        f"{self.greeting_neutral_opening_count} | "
                        f"language="
                        f"{SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE_CODE]}"
                    )

                    if self.is_connected and self.call_is_active:
                        self.greeting_task = self.create_background_task(
                            self.trigger_initial_greeting(
                                language_code=DEFAULT_LANGUAGE_CODE,
                                trigger_reason="neutral_opening_fallback",
                            )
                        )

                    return

                greeting_language = (
                    detected_language
                    if detected_language in SUPPORTED_LANGUAGES
                    else DEFAULT_LANGUAGE_CODE
                )

                self.greeting_sent = True
                self.greeting_language = greeting_language
                self.greeting_trigger_reason = "first_meaningful_customer_voice"

                print(
                    f"▶️ [GREETING TRIGGERED BY CUSTOMER] "
                    f"first_meaningful='{user_text[:120]}' | "
                    f"Whisper={detected_language}/"
                    f"{language_probability:.2f} | "
                    f"greeting_language="
                    f"{SUPPORTED_LANGUAGES.get(greeting_language, 'English')}"
                )

                if self.is_connected and self.call_is_active:
                    self.greeting_task = self.create_background_task(
                        self.trigger_initial_greeting(
                            language_code=greeting_language,
                            trigger_reason="first_meaningful_customer_voice",
                        )
                    )

                return

            # ==========================================================
            # 23. NORMAL CUSTOMER TURN -> SEND TO AI
            # ==========================================================

            if (
                self.is_connected
                and self.call_is_active
            ):

                await self.submit_user_turn(
                    user_text
                )

        except asyncio.CancelledError:
            raise

        except Exception as e:
            print(
                f"❌ [TRANSCRIPTION FAIL] "
                f"{type(e).__name__}: {e}"
            )

    # ------------------------------------------------------------------
    # GREETING
    # ------------------------------------------------------------------

    async def trigger_initial_greeting(
        self,
        language_code: str = DEFAULT_LANGUAGE_CODE,
        trigger_reason: str = "",
    ):
        try:
            # Small delay lets call state settle but avoids a long dead-air gap.
            await asyncio.sleep(0.15)

            if not self.is_connected or not self.call_is_active:
                return

            script_data = await self.get_runtime_configuration()

            language_code = str(
                language_code
                or DEFAULT_LANGUAGE_CODE
            ).strip().lower()

            if language_code not in SUPPORTED_LANGUAGES:
                language_code = DEFAULT_LANGUAGE_CODE

            multilingual_greetings = {
                "ml": (
                    "നമസ്കാരം. ഞാൻ Brainex AI Institute-ൽ നിന്നുള്ള AI calling "
                    "assistant ആണ്. ഞങ്ങളുടെ practical AI training programs-നെ "
                    "കുറിച്ച് ചുരുക്കമായി പറയട്ടേ?"
                ),
                "ta": (
                    "வணக்கம். நான் Brainex AI Institute-இன் AI calling assistant. "
                    "எங்களுடைய practical AI training programs பற்றி சுருக்கமாக "
                    "சொல்லலாமா?"
                ),
                "hi": (
                    "नमस्ते। मैं Brainex AI Institute का AI calling assistant हूँ। "
                    "क्या मैं आपको हमारे practical AI training programs के बारे "
                    "में संक्षेप में बता सकता हूँ?"
                ),
                "ar": (
                    "مرحباً. أنا المساعد الآلي للاتصالات من Brainex AI Institute. "
                    "هل يمكنني أن أشرح لك باختصار برامجنا التدريبية العملية في الذكاء الاصطناعي؟"
                ),
            }

            if language_code == "en":
                greeting = (
                    script_data["greeting"].strip()
                    if script_data and script_data.get("greeting")
                    else (
                        "Hello, I'm the AI calling assistant from Brainex AI "
                        "Institute. May I briefly tell you about our practical "
                        "AI training programs?"
                    )
                )
            else:
                greeting = multilingual_greetings.get(
                    language_code,
                    (
                        "Hello, I'm the AI calling assistant from Brainex AI "
                        "Institute. May I briefly tell you about our practical "
                        "AI training programs?"
                    ),
                )

            print(
                f"🌐 [GREETING LANGUAGE] "
                f"{SUPPORTED_LANGUAGES.get(language_code, 'English')} "
                f"({language_code}) | "
                f"reason={trigger_reason or 'unspecified'}"
            )

            # Telephone greeting guard: preserve the configured company
            # greeting, but prevent accidentally huge scripts.
            if len(greeting) > 260:
                greeting = greeting[:260].rsplit(" ", 1)[0].rstrip(" ,;:")
                if greeting and greeting[-1] not in ".!?":
                    greeting += "."

            self.conversation_stage = "WAIT_PERMISSION"
            self.conversation_history.append({
                "role": "assistant",
                "text": greeting,
            })

            print(f"🗣️ [INITIAL GREETING]: {greeting}")

            await self.safe_send({
                "type": "ai_response",
                "sender": "AI",
                "text": greeting,
            })

            my_generation = self.turn_generation
            self.is_tts_generating = True
            try:
                pcm = await generate_voice_pcm_bytes(
                    greeting,
                    script_data.get("tts_settings", {}),
                    language_code=language_code,
                )
            finally:
                self.is_tts_generating = False

            if (
                pcm
                and self.is_connected
                and self.call_is_active
                and my_generation == self.turn_generation
            ):
                self.call_transcript_log.append(f"AI Agent: {greeting}")
                await self.stream_pcm_to_client(pcm, my_generation)

        except asyncio.CancelledError:
            self.is_ai_speaking = False
            self.is_tts_generating = False
            print("⚠️ [GREETING TASK CANCELLED]")
            raise

        except Exception as e:
            self.is_ai_speaking = False
            self.is_tts_generating = False
            print(f"❌ [GREETING ERROR] {type(e).__name__}: {e}")

        finally:
            if self.greeting_task is asyncio.current_task():
                self.greeting_task = None
            print(
                "🎧 [CUSTOMER DOWNLINK READY] Waiting for remote customer audio..."
            )


    async def process_text_inference(
        self,
        user_text: str,
        my_generation: int,
    ):
        """
        Natural company telecalling pipeline.

        Flow:
        Customer text
            -> company configuration
            -> lead context
            -> shared natural conversation engine
            -> structured sales decision
            -> customer profile/state update
            -> TTS
            -> PCM stream to phone

        The shared conversation engine is responsible for:
        - natural conversation
        - company-fact grounding
        - avoiding robotic repeated replies
        - avoiding repeated customer-name usage
        - handling unavailable courses/services naturally
        - need discovery
        - structured lead qualification
        """

        try:
            # --------------------------------------------------------------
            # 1. Reject stale / inactive turns
            # --------------------------------------------------------------
            if (
                not self.is_connected
                or not self.call_is_active
                or my_generation != self.turn_generation
            ):
                return

            pipeline_start = time.perf_counter()

            print(
                f"⏱️ [PIPELINE START] gen={my_generation}"
            )

            print(
                f"🤖 [NATURAL AI TURN] "
                f"gen={my_generation} | "
                f"stage={self.conversation_stage} | "
                f"Customer='{user_text}'"
            )

            # --------------------------------------------------------------
            # 2. Send customer transcript to Flutter/client
            # --------------------------------------------------------------
            await self.safe_send({
                "type": "user_transcript",
                "sender": "Customer",
                "text": user_text,
            })

            # --------------------------------------------------------------
            # 3. Load active company configuration
            # --------------------------------------------------------------
            config = await self.get_runtime_configuration()

            if not config:
                config = self.default_brainex_configuration()

            # --------------------------------------------------------------
            # 4. Deterministic intent detection
            #
            # Explicit refusal/callback should always override uncertain LLM
            # classification.
            # --------------------------------------------------------------
            forced = self.apply_explicit_customer_intent(
                user_text
            )

            # --------------------------------------------------------------
            # 5. Build lead/customer context
            # --------------------------------------------------------------
            lead_name = (
                getattr(self, "lead_name", "")
                or getattr(self, "customer_name", "")
                or ""
            )

            lead_phone = (
                getattr(self, "client_phone", "")
                or ""
            )

            lead_details = (
                getattr(self, "lead_details", "")
                or ""
            )

            lead_context = {
                "name": lead_name,
                "phone_number": lead_phone,
                "details": lead_details,
            }

            # --------------------------------------------------------------
            # 6. Prepare clean conversation history
            # --------------------------------------------------------------
            history_for_engine = []

            for item in self.conversation_history[-12:]:
                if not isinstance(item, dict):
                    continue

                role = str(
                    item.get("role", "")
                ).strip().lower()

                text = _compact_text(
                    item.get("text", ""),
                    300,
                )

                if not text:
                    continue

                if role not in {
                    "user",
                    "assistant",
                }:
                    continue

                history_for_engine.append({
                    "role": role,
                    "text": text,
                })

            # --------------------------------------------------------------
            # 7. Call shared natural conversation engine
            # --------------------------------------------------------------
            llama_start = time.perf_counter()

            decision = await generate_conversation_turn(
                company_config=config,
                lead=lead_context,
                history=history_for_engine,
                customer_profile=self.customer_profile,
                stage=self.conversation_stage,
                user_text=user_text,
                response_language=getattr(
                    self,
                    "detected_language",
                    DEFAULT_LANGUAGE_CODE,
                ),
                language_locked=bool(
                    getattr(self, "language_final_locked", False)
                ),
            )

            # --------------------------------------------------------------
            # 8. Drop stale model result
            # --------------------------------------------------------------
            if my_generation != self.turn_generation:
                print(
                    f"🗑️ [STALE LLM RESULT DROPPED] "
                    f"gen={my_generation}"
                )
                return

            if not isinstance(decision, dict):
                print(
                    "⚠️ [INVALID CONVERSATION ENGINE RESULT] "
                    f"{type(decision).__name__}"
                )

                decision = {
                    "reply": (
                        "Could you please repeat that?"
                    ),
                    "next_stage": self.conversation_stage,
                    "customer_profile": {},
                    "interest_status": (
                        self.customer_profile.get(
                            "interest_status",
                            "UNDECIDED",
                        )
                    ),
                    "needs_more_information": False,
                    "ready_to_join": False,
                    "callback_requested": False,
                }

            # --------------------------------------------------------------
            # 9. Apply structured sales/profile extraction
            # --------------------------------------------------------------
            self.apply_ai_decision(
                decision
            )

            # --------------------------------------------------------------
            # 10. Deterministic explicit intent always wins
            # --------------------------------------------------------------
            if forced:
                self.apply_forced_intent(
                    forced,
                    config,
                )

            # --------------------------------------------------------------
            # 11. Select spoken reply
            # --------------------------------------------------------------
            reply = _compact_text(
                decision.get("reply"),
                320,
            )

            # --------------------------------------------------------------
            # 12. Explicit refusal/callback responses
            # --------------------------------------------------------------
            if forced == "NOT_INTERESTED":
                reply = (
                    config.get("closing")
                    or
                    "Thank you for your time. Have a good day."
                )

            elif forced == "CALLBACK_REQUESTED":
                reply = (
                    "Certainly. I'll note that you'd like a callback, "
                    "and our team can contact you."
                )

            # --------------------------------------------------------------
            # 13. Fallback if engine returned no reply
            # --------------------------------------------------------------
            if not reply:
                reply = (
                    "Could you please repeat that?"
                )

            # --------------------------------------------------------------
            # 14. Remove accidental repeated greeting/name usage
            #
            # This is a defensive guard.
            # The conversation engine should already prevent this,
            # but this protects the live call if Llama still produces it.
            # --------------------------------------------------------------
            if len(self.conversation_history) >= 2:

                # Remove repeated "Hello" from normal conversation turns.
                reply = re.sub(
                    r"^\s*hello[\s,!.:-]*",
                    "",
                    reply,
                    flags=re.IGNORECASE,
                ).strip()

                # Remove lead name if Llama unnecessarily starts with it.
                if lead_name:
                    escaped_name = re.escape(
                        lead_name.strip()
                    )

                    reply = re.sub(
                        rf"^\s*{escaped_name}"
                        rf"[\s,!.:-]*",
                        "",
                        reply,
                        flags=re.IGNORECASE,
                    ).strip()

            # --------------------------------------------------------------
            # 15. Detect exact repeated AI reply
            #
            # If Llama returned exactly the same reply as the previous AI
            # response, ask it to continue naturally instead of sending the
            # duplicate to the customer.
            # --------------------------------------------------------------
            previous_ai_reply = None

            for item in reversed(
                self.conversation_history
            ):
                if (
                    isinstance(item, dict)
                    and item.get("role") == "assistant"
                ):
                    previous_ai_reply = _compact_text(
                        item.get("text"),
                        320,
                    )
                    break

            if (
                previous_ai_reply
                and reply
                and reply.strip().lower()
                == previous_ai_reply.strip().lower()
            ):
                print(
                    "⚠️ [DUPLICATE AI REPLY DETECTED]"
                )

                retry_history = (
                    history_for_engine
                    + [
                        {
                            "role": "user",
                            "text": user_text,
                        },
                        {
                            "role": "assistant",
                            "text": reply,
                        },
                    ]
                )

                retry_decision = (
                    await generate_conversation_turn(
                        company_config=config,
                        lead=lead_context,
                        history=retry_history,
                        customer_profile=(
                            self.customer_profile
                        ),
                        stage=(
                            self.conversation_stage
                        ),
                        user_text=(
                            "The previous reply repeated information. "
                            "Answer the customer's actual latest question "
                            "naturally using a different response."
                        ),
                        response_language=getattr(
                            self,
                            "detected_language",
                            DEFAULT_LANGUAGE_CODE,
                        ),
                        language_locked=bool(
                            getattr(self, "language_final_locked", False)
                        ),
                    )
                )

                if (
                    isinstance(
                        retry_decision,
                        dict,
                    )
                    and retry_decision.get(
                        "reply"
                    )
                ):
                    retry_reply = _compact_text(
                        retry_decision.get(
                            "reply"
                        ),
                        320,
                    )

                    if retry_reply:
                        reply = retry_reply

            # --------------------------------------------------------------
            # 16. Telephone-friendly speech length
            # --------------------------------------------------------------
            if len(reply) > 320:
                reply = (
                    reply[:320]
                    .rsplit(
                        " ",
                        1,
                    )[0]
                    .rstrip(
                        " ,;:"
                    )
                )

                if (
                    reply
                    and reply[-1]
                    not in ".!?"
                ):
                    reply += "."

            # --------------------------------------------------------------
            # 17. Update conversation memory
            # --------------------------------------------------------------
            self.conversation_history.append({
                "role": "user",
                "text": user_text,
            })

            self.conversation_history.append({
                "role": "assistant",
                "text": reply,
            })

            if len(
                self.conversation_history
            ) > 24:
                self.conversation_history = (
                    self.conversation_history[-24:]
                )

            # --------------------------------------------------------------
            # 18. Log conversation state
            # --------------------------------------------------------------
            print(
                f"⚡ [NATURAL AI READY] "
                f"{time.perf_counter() - llama_start:.2f}s | "
                f"stage={self.conversation_stage} | "
                f"status="
                f"{self.customer_profile.get('interest_status', 'UNDECIDED')} | "
                f"{reply}"
            )

            # --------------------------------------------------------------
            # 19. Check call is still active before TTS
            # --------------------------------------------------------------
            if (
                not self.is_connected
                or not self.call_is_active
                or my_generation
                != self.turn_generation
            ):
                return

            # --------------------------------------------------------------
            # 20. Generate TTS
            # --------------------------------------------------------------
            self.is_tts_generating = True

            tts_start = time.perf_counter()

            try:
                pcm = (
                    await generate_voice_pcm_bytes(
                        reply,
                        config.get("tts_settings", {}),
                        language_code=getattr(
                            self,
                            "detected_language",
                            DEFAULT_LANGUAGE_CODE,
                        ),
                    )
                )

            finally:
                self.is_tts_generating = False

            # --------------------------------------------------------------
            # 21. Drop stale TTS
            # --------------------------------------------------------------
            if (
                my_generation
                != self.turn_generation
            ):
                print(
                    f"🗑️ [STALE TTS RESULT DROPPED] "
                    f"gen={my_generation}"
                )
                return

            print(
                f"⚡ [TTS PIPELINE READY] "
                f"{time.perf_counter() - tts_start:.2f}s"
            )

            if not pcm:
                print(
                    "⚠️ [TTS EMPTY] "
                    "No AI PCM generated."
                )
                return

            if (
                not self.is_connected
                or not self.call_is_active
            ):
                print(
                    "🗑️ [AI AUDIO DROPPED] "
                    "Call no longer active."
                )
                return

            # --------------------------------------------------------------
            # 22. Send text/state to Flutter
            # --------------------------------------------------------------
            await self.safe_send({
                "type": "ai_response",
                "sender": "AI",
                "text": reply,
                "stage": self.conversation_stage,
                "interest_status": (
                    self.customer_profile.get(
                        "interest_status",
                        "UNDECIDED",
                    )
                ),
                "customer_profile": (
                    self.customer_profile
                ),
            })

            # --------------------------------------------------------------
            # 23. Stream AI audio
            # --------------------------------------------------------------
            await self.stream_pcm_to_client(
                pcm,
                my_generation,
            )

            # --------------------------------------------------------------
            # 24. Record accepted AI turn
            # --------------------------------------------------------------
            if (
                self.is_connected
                and self.call_is_active
                and my_generation
                == self.turn_generation
            ):
                self.call_transcript_log.append(
                    f"AI Agent: {reply}"
                )

                print(
                    f"✅ [NATURAL TURN COMPLETE] "
                    f"total="
                    f"{time.perf_counter() - pipeline_start:.2f}s | "
                    f"gen={my_generation}"
                )

                print(
                    f"⏱️ [PIPELINE TOTAL] "
                    f"{time.perf_counter() - pipeline_start:.2f}s"
                )

        except asyncio.CancelledError:
            self.is_ai_speaking = False
            self.is_tts_generating = False

            print(
                f"🛑 [AI TURN CANCELLED] "
                f"gen={my_generation} | "
                f"customer='{user_text}'"
            )

            raise

        except Exception as e:
            self.is_ai_speaking = False
            self.is_tts_generating = False

            print(
                f"❌ [AI PIPELINE ERROR] "
                f"{type(e).__name__}: {e}"
            )
    def default_brainex_configuration(self):
        """
        Current reference-company fallback.

        Normal operation should use CompanyScript from Django Admin. This
        fallback prevents a generic chatbot response if no active script is
        configured yet.
        """
        return {
            "config_source": "brainex_fallback",
            "company": "Brainex AI Institute",
            "bot_name": "Brainex AI Assistant",
            "details": (
                "Brainex AI Institute focuses on practical AI training. "
                "Training can be relevant to students, working professionals, "
                "business owners, parents, educators, creators and others who "
                "want to use AI effectively. Known training durations include "
                "5 days, 10 days, 15 days, 1 month and 6 months. Areas may "
                "include AI tools, prompting, productivity, research, content "
                "creation, automation and AI-assisted workflows. Do not state "
                "fees, batch dates or other details unless they are supplied "
                "in the active CompanyScript."
            ),
            "greeting": (
                "Hello, I'm the AI calling assistant from Brainex AI "
                "Institute. We provide practical AI training programs. "
                "May I briefly tell you about them?"
            ),
            "closing": (
                "Thank you for your time. If you need further details, "
                "our Brainex team can contact you."
            ),
            "products": [],
            "collection_fields": [],
            "tts_settings": {
                "voice_name": SAPI_VOICE,
                "rate": SAPI_RATE,
                "volume": SAPI_VOLUME,
            },
            "company_id": self.company_id,
            "telicall_line_id": self.telicall_line_id,
        }

    def get_stage_objective(self, stage):
        objectives = {
            "WAIT_PERMISSION": (
                "Get permission to continue. If yes, move to "
                "PROFILE_COLLECTION. If no, close politely."
            ),
            "PROFILE_COLLECTION": (
                "Understand the customer's profile/background, then move to "
                "NEED_DISCOVERY."
            ),
            "NEED_DISCOVERY": (
                "Understand what the customer wants to achieve with AI or the "
                "company offering, then move to PRODUCT_EXPLANATION."
            ),
            "PRODUCT_EXPLANATION": (
                "Explain the most relevant known offering briefly, then move "
                "to DETAIL_COLLECTION or INTEREST_CHECK."
            ),
            "DETAIL_COLLECTION": (
                "Collect useful missing details one at a time, then move to "
                "INTEREST_CHECK."
            ),
            "INTEREST_CHECK": (
                "Determine lead intent: interested, needs more information, "
                "ready to join, callback, or not interested."
            ),
            "FOLLOW_UP": (
                "Confirm human/company follow-up and then move to CLOSING."
            ),
            "CLOSING": (
                "Close politely without opening a new sales topic."
            ),
            "FINISHED": "Do not continue selling.",
        }
        return objectives.get(stage, objectives["NEED_DISCOVERY"])

    def apply_explicit_customer_intent(self, user_text):
        clean = re.sub(r"\s+", " ", str(user_text or "").lower()).strip()

        explicit_stop = (
            "not interested",
            "don't call",
            "do not call",
            "stop calling",
            "remove my number",
            "drop the call",
            "end the call",
            "hang up",
        )
        if any(phrase in clean for phrase in explicit_stop):
            return "NOT_INTERESTED"

        callback = (
            "call me later",
            "call back later",
            "callback later",
            "call me tomorrow",
            "call me back",
            "contact me later",
        )
        if any(phrase in clean for phrase in callback):
            return "CALLBACK_REQUESTED"

        # A plain "no" is considered refusal only at the permission stage.
        if self.conversation_stage == "WAIT_PERMISSION":
            if clean in {"no", "no thanks", "no thank you", "not now"}:
                return "NOT_INTERESTED"

        return None

    def apply_forced_intent(self, intent, config):
        if intent == "NOT_INTERESTED":
            self.customer_profile["interest_status"] = "NOT_INTERESTED"
            self.customer_profile["needs_more_information"] = False
            self.customer_profile["ready_to_join"] = False
            self.customer_profile["callback_requested"] = False
            self.conversation_stage = "CLOSING"

        elif intent == "CALLBACK_REQUESTED":
            self.customer_profile["interest_status"] = "CALLBACK_REQUESTED"
            self.customer_profile["callback_requested"] = True
            self.conversation_stage = "CLOSING"

    def apply_ai_decision(self, decision):
        profile = decision.get("customer_profile")
        if isinstance(profile, dict):
            for key in PROFILE_KEYS:
                value = profile.get(key)
                if value is None:
                    continue
                value = _compact_text(value, 160)
                if value and value.lower() not in {
                    "null",
                    "none",
                    "unknown",
                    "not provided",
                }:
                    self.customer_profile[key] = value

        status = str(
            decision.get(
                "interest_status",
                self.customer_profile["interest_status"],
            )
            or ""
        ).strip().upper()

        if status in LEAD_OUTCOMES:
            self.customer_profile["interest_status"] = status

        self.customer_profile["needs_more_information"] = _bool_value(
            decision.get("needs_more_information"),
            self.customer_profile["needs_more_information"],
        )
        self.customer_profile["ready_to_join"] = _bool_value(
            decision.get("ready_to_join"),
            self.customer_profile["ready_to_join"],
        )
        self.customer_profile["callback_requested"] = _bool_value(
            decision.get("callback_requested"),
            self.customer_profile["callback_requested"],
        )

        if self.customer_profile["ready_to_join"]:
            self.customer_profile["interest_status"] = "READY_TO_JOIN"
        elif self.customer_profile["callback_requested"]:
            self.customer_profile["interest_status"] = "CALLBACK_REQUESTED"
        elif self.customer_profile["needs_more_information"]:
            self.customer_profile["interest_status"] = (
                "NEED_MORE_INFORMATION"
            )

        next_stage = str(
            decision.get("next_stage", self.conversation_stage) or ""
        ).strip().upper()

        if next_stage in CONVERSATION_STAGES:
            self.conversation_stage = next_stage

        # Protect the sales flow from obvious contradictory states.
        if self.customer_profile["interest_status"] == "NOT_INTERESTED":
            self.conversation_stage = "CLOSING"
        elif self.customer_profile["interest_status"] in {
            "READY_TO_JOIN",
            "CALLBACK_REQUESTED",
            "NEED_MORE_INFORMATION",
        } and self.conversation_stage == "FINISHED":
            self.conversation_stage = "CLOSING"

    def build_fallback_report(self):
        status = self.customer_profile.get(
            "interest_status",
            "UNDECIDED",
        )
        follow_up = status in {
            "INTERESTED",
            "NEED_MORE_INFORMATION",
            "READY_TO_JOIN",
            "CALLBACK_REQUESTED",
        }

        priority = "LOW"
        if status == "READY_TO_JOIN":
            priority = "HOT"
        elif status in {"INTERESTED", "CALLBACK_REQUESTED"}:
            priority = "HIGH"
        elif status == "NEED_MORE_INFORMATION":
            priority = "MEDIUM"

        return {
            "outcome": status,
            "interest_status": status,
            "customer_type": self.customer_profile.get("customer_type"),
            "education": self.customer_profile.get("education"),
            "occupation": self.customer_profile.get("occupation"),
            "interest_area": self.customer_profile.get("interest_area"),
            "interested_course": self.customer_profile.get("course_interest"),
            "ready_to_join": bool(
                self.customer_profile.get("ready_to_join")
            ),
            "needs_more_information": bool(
                self.customer_profile.get("needs_more_information")
            ),
            "callback_requested": bool(
                self.customer_profile.get("callback_requested")
            ),
            "follow_up_required": follow_up,
            "lead_priority": priority,
            "customer_summary": (
                "Structured report generated from live call state."
            ),
            "next_action": (
                "Company team follow-up required."
                if follow_up
                else "No follow-up required."
            ),
            "conversation_stage_at_end": self.conversation_stage,
            "company_id": self.company_id,
            "telicall_line_id": self.telicall_line_id,
        }

    async def analyze_completed_call(self):
        """
        STEP 2: separate post-call analyzer.

        Produces a sales report without changing the spoken conversation.
        Uses current structured state as the fallback/source of truth when
        transcript information is incomplete.
        """
        fallback = self.build_fallback_report()
        transcript = "\n".join(self.call_transcript_log).strip()

        if not transcript:
            print("⚠️ [POST CALL ANALYZER] No transcript; using live state.")
            return fallback

        config = await self.get_runtime_configuration()
        config = config or self.default_brainex_configuration()

        analyzer_prompt = f"""
You are a post-call sales analyst for {config['company']}.

Analyze ONLY the supplied call transcript and live extracted state.
Do not invent missing customer facts.

Allowed outcome values:
- NOT_INTERESTED
- INTERESTED
- NEED_MORE_INFORMATION
- READY_TO_JOIN
- CALLBACK_REQUESTED
- UNDECIDED

Lead priority:
- HOT: customer clearly wants to join/buy/proceed now
- HIGH: strong interest or explicit callback request
- MEDIUM: needs more information / possible interest
- LOW: undecided or not interested

LIVE STATE
{json.dumps(self.customer_profile, ensure_ascii=False)}

TRANSCRIPT
{transcript[-7000:]}

Return ONLY valid JSON:
{{
  "outcome": "UNDECIDED",
  "interest_status": "UNDECIDED",
  "customer_type": null,
  "education": null,
  "occupation": null,
  "interest_area": null,
  "interested_course": null,
  "ready_to_join": false,
  "needs_more_information": false,
  "callback_requested": false,
  "follow_up_required": false,
  "lead_priority": "LOW",
  "customer_summary": "brief factual summary",
  "next_action": "brief recommended next action"
}}
"""

        try:
            response_text, model_name = await generate_gemini_json(
                analyzer_prompt,
                temperature=0.1,
                max_output_tokens=400,
            )

            parsed = _extract_json_object(response_text)
            if not parsed:
                print(
                    "⚠️ [POST CALL ANALYZER] Invalid JSON; using fallback."
                )
                return fallback

            report = dict(fallback)

            for key in (
                "customer_type",
                "education",
                "occupation",
                "interest_area",
                "interested_course",
                "customer_summary",
                "next_action",
            ):
                value = parsed.get(key)
                if value not in {None, "", "null", "unknown"}:
                    report[key] = value

            outcome = str(
                parsed.get("outcome")
                or parsed.get("interest_status")
                or fallback["outcome"]
            ).strip().upper()
            if outcome not in LEAD_OUTCOMES:
                outcome = fallback["outcome"]

            report["outcome"] = outcome
            report["interest_status"] = outcome
            report["ready_to_join"] = _bool_value(
                parsed.get("ready_to_join"),
                fallback["ready_to_join"],
            )
            report["needs_more_information"] = _bool_value(
                parsed.get("needs_more_information"),
                fallback["needs_more_information"],
            )
            report["callback_requested"] = _bool_value(
                parsed.get("callback_requested"),
                fallback["callback_requested"],
            )
            report["follow_up_required"] = _bool_value(
                parsed.get("follow_up_required"),
                fallback["follow_up_required"],
            )

            priority = str(
                parsed.get("lead_priority", fallback["lead_priority"])
                or ""
            ).strip().upper()
            if priority not in {"LOW", "MEDIUM", "HIGH", "HOT"}:
                priority = fallback["lead_priority"]
            report["lead_priority"] = priority

            print(
                "📊 [POST CALL REPORT] "
                f"outcome={report['outcome']} | "
                f"priority={report['lead_priority']} | "
                f"followup={report['follow_up_required']}"
            )
            return report

        except Exception as e:
            print(
                f"⚠️ [POST CALL ANALYZER ERROR] "
                f"{type(e).__name__}: {e} | using fallback"
            )
            return fallback

    async def stream_pcm_to_client(
        self,
        pcm: bytes,
        my_generation: int,
    ):
        """
        Pace PCM in ~0.5-second chunks.

        Benefits:
        - avoids one giant 100-250 KB playback block
        - lets customer barge-in stop the remaining response quickly
        - stops sending immediately if the call ends
        """
        if not pcm:
            return

        self.is_ai_speaking = True
        sent = 0

        try:
            for offset in range(0, len(pcm), AI_STREAM_CHUNK_BYTES):
                if (
                    not self.is_connected
                    or not self.call_is_active
                    or my_generation != self.turn_generation
                ):
                    print(
                        f"🛑 [AI PCM STREAM ABORTED] "
                        f"sent={sent}/{len(pcm)}"
                    )
                    return

                chunk = pcm[offset:offset + AI_STREAM_CHUNK_BYTES]
                if len(chunk) % 2:
                    chunk = chunk[:-1]
                if not chunk:
                    continue

                self.ai_pcm.extend(chunk)
                await self.send(bytes_data=chunk)
                sent += len(chunk)

                # Pace near real-time instead of dumping whole audio instantly.
                chunk_seconds = len(chunk) / PCM_BYTES_PER_SECOND
                await asyncio.sleep(max(0.0, chunk_seconds * 0.92))

            print(f"🔊 [AI PCM STREAM COMPLETE] bytes={sent}")

        except asyncio.CancelledError:
            print(
                f"🛑 [AI PCM STREAM CANCELLED] sent={sent}/{len(pcm)}"
            )
            raise

        finally:
            self.is_ai_speaking = False

    # ------------------------------------------------------------------
    # SAFE SEND
    # ------------------------------------------------------------------

    async def safe_send(self, payload: dict):
        if not self.is_connected:
            return
        try:
            await self.send(text_data=json.dumps(payload))
        except Exception as e:
            print(f"⚠️ [WS SEND ERROR] {type(e).__name__}: {e}")
            self.is_connected = False

    # ------------------------------------------------------------------
    # DATABASE
    # ------------------------------------------------------------------

    @database_sync_to_async
    def create_call_session(self, phone_number):
        try:
            if not phone_number:
                return None

            contact, _ = Contact.objects.get_or_create(
                phone_number=phone_number
            )
            session = CallSession.objects.create(
                contact=contact,
                status="active",
            )
            return session.id

        except Exception as e:
            print(f"⚠️ [DB CREATE SESSION ERROR] {e}")
            return None

    @database_sync_to_async
    def mark_call_session_completed(
        self,
        session_id,
        duration,
    ):
        """
        Immediately mark the CallSession as completed when the call/WebSocket
        ends.

        IMPORTANT:
        This runs BEFORE slow post-call AI analysis and recording/report
        finalization so Django Admin does not leave ended calls stuck as ACTIVE.
        """
        try:
            if not session_id:
                return False

            updated = CallSession.objects.filter(
                id=session_id
            ).update(
                status="completed",
                duration_seconds=max(
                    0,
                    int(duration),
                ),
            )

            if updated:
                print(
                    f"✅ [CALL SESSION CLOSED] "
                    f"ID={session_id} | "
                    f"duration={int(duration)}s | "
                    f"status=completed"
                )
                return True

            print(
                f"⚠️ [CALL SESSION CLOSE] "
                f"Session ID={session_id} not found"
            )
            return False

        except Exception as e:
            print(
                f"❌ [CALL SESSION CLOSE ERROR] "
                f"{type(e).__name__}: {e}"
            )
            return False

    @database_sync_to_async
    def finalize_call_session(
        self,
        session_id,
        duration,
        transcript_log,
        report=None,
    ):
        try:
            if not session_id:
                return

            session = CallSession.objects.get(id=session_id)
            session.duration_seconds = int(duration)
            session.status = "completed"

            timestamp = int(time.time())

            if self.customer_pcm and not session.recording_file:
                raw = bytes(self.customer_pcm)
                if len(raw) % 2:
                    raw = raw[:-1]

                buf = io.BytesIO()
                with wave.open(buf, "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(PCM_SAMPLE_RATE)
                    wav_file.writeframes(raw)

                session.recording_file.save(
                    f"customer_{session_id}_{timestamp}.wav",
                    ContentFile(buf.getvalue()),
                    save=False,
                )

            if self.ai_pcm and not session.ai_recording_file:
                raw = bytes(self.ai_pcm)
                if len(raw) % 2:
                    raw = raw[:-1]

                buf = io.BytesIO()
                with wave.open(buf, "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(PCM_SAMPLE_RATE)
                    wav_file.writeframes(raw)

                session.ai_recording_file.save(
                    f"ai_{session_id}_{timestamp}.wav",
                    ContentFile(buf.getvalue()),
                    save=False,
                )

            session.save()

            # STEP 3: CallSession remains the parent call record. The existing
            # SalesInsight one-to-one stores the complete structured JSON
            # report without requiring a database migration.
            if report:
                SalesInsight.objects.update_or_create(
                    call_session=session,
                    defaults={
                        "extracted_data": report,
                        "needs_followup": bool(
                            report.get("follow_up_required", False)
                        ),
                    },
                )

            print(
                f"✅ [DB SESSION SAVED] Dual recordings + structured report "
                f"saved for ID: {session_id}"
            )

        except Exception as e:
            print(f"⚠️ [DB FINALIZE ERROR] {e}")

    @database_sync_to_async
    def save_sales_insight(
        self,
        session_id,
        report,
    ):
        """
        Save only the structured post-call report.

        Recording files are already persisted before the slow Llama analysis,
        so this method intentionally does not rewrite either WAV file.
        """
        try:
            if not session_id or not report:
                return False

            session = CallSession.objects.get(id=session_id)

            SalesInsight.objects.update_or_create(
                call_session=session,
                defaults={
                    "extracted_data": report,
                    "needs_followup": bool(
                        report.get("follow_up_required", False)
                    ),
                },
            )

            print(
                f"📊 [SALES INSIGHT SAVED] "
                f"CallSession ID={session_id}"
            )
            return True

        except Exception as e:
            print(
                f"⚠️ [SALES INSIGHT SAVE ERROR] "
                f"{type(e).__name__}: {e}"
            )
            return False

    @database_sync_to_async
    def get_runtime_configuration(self):
        """
        Configuration boundary for STEP 4.

        TODAY:
            CompanyScript -> Brainex reference configuration.

        LATER:
            authenticated device_token
              -> TelicallDevice
              -> TelicallLine
              -> Company
              -> products / collection fields / rules

        Keeping this boundary means Whisper/Llama/TTS/call audio code does not
        need to be rewritten when multi-company models are introduced.
        """
        try:
            settings = SystemSettings.get_settings()

            use_default_voice = bool(
                getattr(settings, "use_system_default_voice", True)
            )

            configured_voice = str(
                getattr(settings, "tts_voice_name", "") or ""
            ).strip()

            voice_name = "" if use_default_voice else configured_voice

            try:
                tts_rate = max(
                    -10,
                    min(10, int(getattr(settings, "tts_rate", 1))),
                )
            except (TypeError, ValueError):
                tts_rate = 1

            try:
                tts_volume = max(
                    0,
                    min(100, int(getattr(settings, "tts_volume", 100))),
                )
            except (TypeError, ValueError):
                tts_volume = 100

            configured_tts_engine = str(
                getattr(settings, "tts_engine", "sapi") or "sapi"
            ).strip().lower()

            # Environment variable can override DB selection for deployment.
            tts_engine = str(
                os.getenv(
                    "BRAINEX_TTS_ENGINE",
                    configured_tts_engine,
                )
                or configured_tts_engine
                or "sapi"
            ).strip().lower()

            if tts_engine not in {"sapi", "custom", "azure", "gemini"}:
                print(
                    f"⚠️ [TTS CONFIG] Unsupported engine={tts_engine!r}; "
                    "using SAPI."
                )
                tts_engine = "sapi"

            custom_voice = getattr(settings, "custom_voice", None)
            custom_voice_id = getattr(custom_voice, "id", None) if custom_voice else None
            custom_voice_name = str(
                getattr(custom_voice, "name", "") or ""
            ).strip() if custom_voice else ""
            custom_voice_samples = []

            if custom_voice:
                for field_name in ("sample_1", "sample_2", "sample_3"):
                    sample = getattr(custom_voice, field_name, None)
                    if sample:
                        try:
                            custom_voice_samples.append(sample.path)
                        except Exception:
                            pass

            if tts_engine == "custom" and (not custom_voice or not custom_voice_samples):
                print("⚠️ [CUSTOM VOICE CONFIG] Missing selected voice/sample; using SAPI fallback.")
                tts_engine = "sapi"

            tts_settings = {
                "engine": tts_engine,
                "voice_name": voice_name,
                "custom_voice_id": custom_voice_id,
                "custom_voice_name": custom_voice_name,
                "sample_paths": custom_voice_samples,
                "rate": tts_rate,
                "volume": tts_volume,
            }

            script = CompanyScript.objects.filter(is_active=True).first()
            if script:
                return {
                    "config_source": "company_script",
                    "bot_name": getattr(
                        script,
                        "bot_name",
                        "AI Assistant",
                    ) or "AI Assistant",
                    "company": getattr(
                        script,
                        "company_name",
                        "Brainex AI Institute",
                    ) or "Brainex AI Institute",
                    "details": (
                        script.build_ai_knowledge()
                        if hasattr(script, "build_ai_knowledge")
                        else getattr(script, "company_details", "") or ""
                    ),
                    "greeting": getattr(
                        script,
                        "opening_greeting",
                        "",
                    ) or "",
                    "closing": getattr(
                        script,
                        "closing_statement",
                        "",
                    ) or "",
                    "followup_message": getattr(
                        script,
                        "human_followup_message",
                        "",
                    ) or "",
                    "default_language": getattr(
                        script,
                        "default_language",
                        "English",
                    ) or "English",
                    "company_phone": getattr(
                        script,
                        "company_phone",
                        "",
                    ) or "",
                    "whatsapp_number": getattr(
                        script,
                        "whatsapp_number",
                        "",
                    ) or "",
                    "company_email": getattr(
                        script,
                        "company_email",
                        "",
                    ) or "",
                    "company_address": getattr(
                        script,
                        "company_address",
                        "",
                    ) or "",
                    "website_url": getattr(
                        script,
                        "website_url",
                        "",
                    ) or "",
                    "google_maps_url": getattr(
                        script,
                        "google_maps_url",
                        "",
                    ) or "",
                    "whatsapp_url": getattr(
                        script,
                        "whatsapp_url",
                        "",
                    ) or "",
                    "instagram_url": getattr(
                        script,
                        "instagram_url",
                        "",
                    ) or "",
                    "facebook_url": getattr(
                        script,
                        "facebook_url",
                        "",
                    ) or "",
                    "youtube_url": getattr(
                        script,
                        "youtube_url",
                        "",
                    ) or "",
                    "products": [],
                    "collection_fields": [],
                    "tts_settings": tts_settings,
                    "company_id": self.company_id,
                    "telicall_line_id": self.telicall_line_id,
                }

        except Exception as e:
            print(f"⚠️ [DB CONFIG FETCH ERROR] {e}")

        return None

    async def get_active_script(self):
        """Backward-compatible alias for older code paths."""
        return await self.get_runtime_configuration()
