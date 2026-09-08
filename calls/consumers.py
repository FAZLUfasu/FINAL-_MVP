import asyncio
import io
import json
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
import ollama
from pydub import AudioSegment
from calls.conversation_engine import generate_conversation_turn


# Keep the same FFmpeg paths that are already working in the current backend.
AudioSegment.converter = (
    r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
)
AudioSegment.ffprobe = (
    r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
)

from calls.models import CallSession, CompanyScript, Contact, SalesInsight


# ---------------------------------------------------------------------------
# GLOBAL ENGINES
# ---------------------------------------------------------------------------

print("🧠 Loading Whisper Speech Engine inside Django...")

# Accuracy-first STT configuration for cellular speech.
# Override without editing code, for example:
#   $env:TELICALL_WHISPER_MODEL="medium.en"
# Low-latency default for CPU telephony. "base.en" is much faster than
# small.en while normally being more reliable than tiny for English calls.
# You can switch to "small.en" later when accuracy matters more than latency.
WHISPER_MODEL_NAME = os.getenv(
    "TELICALL_WHISPER_MODEL",
    "base.en",
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

# 16 kHz, mono, PCM16 = 32,000 bytes/sec
PCM_SAMPLE_RATE = 16000
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * 2

# Send audio in modest chunks so barge-in can stop playback quickly.
AI_STREAM_CHUNK_BYTES = 16000  # ~0.5 s at 16 kHz PCM16 mono


def initialize_llama_engine():
    print("🦙 Checking Llama Engine Status...")
    try:
        import requests

        res = requests.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
        if res.status_code == 200:
            print("✅ Llama Engine Online & Bound to Pipeline!")
            return
    except Exception:
        print("🚀 Llama Engine not detected. Auto-launching background process...")

    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            ),
        )
        time.sleep(1.5)
        print("✅ Llama Engine launch requested.")
    except Exception as e:
        print(f"❌ Error auto-starting Llama: {e}")


initialize_llama_engine()


# ---------------------------------------------------------------------------
# FAST OFFLINE TTS
# ---------------------------------------------------------------------------

def _sanitize_tts_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    cleaned = re.sub(r"[^\w\s.,?!'\-]", "", cleaned)
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rsplit(" ", 1)[0]
    return cleaned.strip()


async def generate_sapi_pcm_bytes(text: str) -> bytes:
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
    env["BRAINEX_TTS_VOICE"] = SAPI_VOICE
    env["BRAINEX_TTS_RATE"] = str(SAPI_RATE)

    ps_script = r"""
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Speech

$text = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_TEXT')
$out = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_OUT')
$voice = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_VOICE')
$rateRaw = [Environment]::GetEnvironmentVariable('BRAINEX_TTS_RATE')

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


async def generate_voice_pcm_bytes(text: str) -> bytes:
    # This release intentionally defaults to fast offline SAPI.
    # XTTS CPU was the primary latency bottleneck in the supplied logs.
    if TTS_ENGINE == "sapi":
        return await generate_sapi_pcm_bytes(text)

    print(
        f"⚠️ [TTS ENGINE] Unsupported BRAINEX_TTS_ENGINE='{TTS_ENGINE}'. "
        "Falling back to Windows SAPI."
    )
    return await generate_sapi_pcm_bytes(text)



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
            self.call_is_active = True
            self.greeting_sent = False

            self.is_ai_speaking = False
            self.is_tts_generating = False

            self.client_phone = None
            self.session_id = None
            self.lead_details = ""
            self.call_transcript_log = []
            self.start_time = time.time()

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

    async def submit_user_turn(self, user_text: str):
        """
        LATEST TURN WINS:
        - no FIFO ai_processing_lock
        - old AI generation/playback is cancelled
        - only the newest recognized customer turn gets a response
        """
        user_text = (user_text or "").strip()
        if not user_text or not self.is_connected or not self.call_is_active:
            return

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
        self.turn_generation += 1

        if self.timeout_checker_task:
            self.timeout_checker_task.cancel()

        # Cancel dedicated AI/greeting tasks first.
        tasks_to_cancel = []
        for task in [self.current_ai_task, self.greeting_task]:
            if task is not None and not task.done():
                task.cancel()
                tasks_to_cancel.append(task)

        # Cancel every remaining tracked task except ourselves.
        current = asyncio.current_task()
        for task in list(self.background_tasks):
            if task is not current and not task.done() and task not in tasks_to_cancel:
                task.cancel()
                tasks_to_cancel.append(task)

        if tasks_to_cancel:
            print(
                f"🛑 [TASK CLEANUP] Cancelling "
                f"{len(tasks_to_cancel)} background task(s)..."
            )
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        self.background_tasks.clear()
        self.current_ai_task = None
        self.greeting_task = None

        duration = time.time() - self.start_time

        if self.session_id:
            try:
                # STEP 2: analyze the completed call into a structured sales
                # report. If Llama is temporarily unavailable, the analyzer
                # returns a deterministic fallback based on live call state.
                self.final_call_report = await self.analyze_completed_call()

                # STEP 3: save recordings + structured report under the
                # CallSession via its existing SalesInsight one-to-one model.
                await self.finalize_call_session(
                    self.session_id,
                    duration,
                    self.call_transcript_log,
                    self.final_call_report,
                )
                print(
                    f"✅ [SESSION FINALIZED] ID: {self.session_id} | "
                    f"outcome={self.final_call_report.get('outcome')}"
                )
            except Exception as e:
                print(
                    f"❌ [FINALIZE SESSION ERROR] "
                    f"{type(e).__name__}: {e}"
                )

        self.audio_buffer.clear()
        self.pre_roll_buffer.clear()
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

            if not self.greeting_sent:
                self.greeting_sent = True
                self.greeting_task = self.create_background_task(
                    self.trigger_initial_greeting()
                )
            return

        if event == "call_answered":
            self.call_is_active = True
            if not self.greeting_sent:
                self.greeting_sent = True
                self.greeting_task = self.create_background_task(
                    self.trigger_initial_greeting()
                )
            return

        if event == "call_state_changed":
            raw_state = str(parsed_json.get("state", "") or "").strip()
            state = raw_state.upper()

            print(f"📞 [CALL STATE] {state}")

            active_states = {"ACTIVE", "4", "OFFHOOK", "ANSWERED"}
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

                # True barge-in starts as soon as real customer speech starts,
                # not after Whisper finishes the utterance.
                if (
                    self.is_ai_speaking
                    or self.is_tts_generating
                    or (
                        self.current_ai_task is not None
                        and not self.current_ai_task.done()
                    )
                    or (
                        self.greeting_task is not None
                        and not self.greeting_task.done()
                    )
                ):
                    print("✋ [BARGE-IN DETECTED] Customer interrupted AI.")

                    # Cancel greeting separately if still running.
                    if (
                        self.greeting_task is not None
                        and not self.greeting_task.done()
                    ):
                        self.greeting_task.cancel()

                    await self.cancel_current_ai(
                        reason="customer_barge_in",
                        notify_client=True,
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
                await self.process_audio_transcription(raw_audio_bytes)
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
                f"🧠 [STT START] PCM bytes={len(raw_audio_bytes)} | "
                f"ai_speaking={self.is_ai_speaking}"
            )

            pcm_int16 = np.frombuffer(raw_audio_bytes, dtype=np.int16)
            if pcm_int16.size == 0:
                return

            rms = float(
                np.sqrt(np.mean(pcm_int16.astype(np.float32) ** 2))
            )
            if rms < 20:
                print(f"⚠️ [STT SKIP SILENCE] rms={rms:.2f}")
                return

            audio_float32 = pcm_int16.astype(np.float32) / 32768.0
            max_peak = float(np.max(np.abs(audio_float32)))

            audio_seconds = len(pcm_int16) / float(PCM_SAMPLE_RATE)

            print(
                f"🧠 [STT AUDIO] samples={len(pcm_int16)} | "
                f"duration={audio_seconds:.2f}s | "
                f"peak={max_peak:.4f} | rms={rms:.2f}"
            )

            # IMPORTANT:
            # Do not normalize every telephone utterance to full scale.
            # The old `audio_float32 / max_peak` amplified line noise,
            # background noise and codec artifacts together with speech.
            # Only apply a conservative gain to genuinely quiet recordings.
            if 0.003 <= max_peak < 0.08:
                target_peak = 0.18
                gain = min(4.0, target_peak / max_peak)
                audio_float32 = np.clip(
                    audio_float32 * gain,
                    -1.0,
                    1.0,
                )
                print(
                    f"🔉 [STT QUIET AUDIO GAIN] "
                    f"gain={gain:.2f}x"
                )

            segments, info = await asyncio.to_thread(
                whisper_model.transcribe,
                audio_float32,
                beam_size=1,
                best_of=1,
                language="en",
                task="transcribe",
                condition_on_previous_text=False,

                # We already perform cellular VAD/endpointing above.
                # Running Whisper VAD a second time can clip short words,
                # especially "yes", "no", names, and telephone speech.
                vad_filter=False,

                temperature=0.0,
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                no_speech_threshold=0.6,
                word_timestamps=False,
            )

            segments = list(segments)
            user_text = "".join(s.text for s in segments).strip()

            detected_language = getattr(info, "language", "en")
            language_probability = getattr(
                info,
                "language_probability",
                0.0,
            )

            print(
                f"🧠 [WHISPER DONE] "
                f"{time.perf_counter() - start:.2f}s | "
                f"segments={len(segments)} | "
                f"language={detected_language} | "
                f"lang_prob={language_probability:.2f}"
            )
            print(f"🗣️ [WHISPER RESULT]: '{user_text}'")

            clean_check = re.sub(
                r"[^\w\s]",
                "",
                user_text.lower(),
            ).strip()

            hallucinations = {
                "you you",
                "thank you",
                "subtitles",
                "amaraorg",
                "mb",
                "thank you for watching",
                "thanks for watching",
            }

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
            }

            words = clean_check.split()
            useful = (
                bool(user_text)
                and len(clean_check) >= 2
                and clean_check not in hallucinations
                and (
                    len(words) >= 2
                    or clean_check in allowed_single_words
                )
            )

            if not useful:
                print(f"⚠️ [WHISPER IGNORED]: '{user_text}'")
                return

            print(f"✅ [CUSTOMER TRANSCRIPT]: {user_text}")
            self.call_transcript_log.append(f"Customer: {user_text}")
            self.last_activity_time = time.time()

            if self.is_connected and self.call_is_active:
                await self.submit_user_turn(user_text)

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

    async def trigger_initial_greeting(self):
        try:
            # Small delay lets call state settle but avoids a long dead-air gap.
            await asyncio.sleep(0.15)

            if not self.is_connected or not self.call_is_active:
                return

            script_data = await self.get_runtime_configuration()

            greeting = (
                script_data["greeting"].strip()
                if script_data and script_data.get("greeting")
                else (
                    "Hello, I'm the AI calling assistant from Brainex AI "
                    "Institute. May I briefly tell you about our practical "
                    "AI training programs?"
                )
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
                pcm = await generate_voice_pcm_bytes(greeting)
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

    # ------------------------------------------------------------------
    # LLM + TTS
    # ------------------------------------------------------------------

#     async def process_text_inference(
#         self,
#         user_text: str,
#         my_generation: int,
#     ):
#         """
#         STEP 1:
#         Controlled company telecalling with:
#         - conversation memory
#         - explicit stages
#         - structured customer information extraction
#         - sales/qualification behavior

#         One Llama call returns both the spoken reply and structured state so
#         we do not add a second LLM latency hit on every customer turn.
#         """
#         try:
#             if (
#                 not self.is_connected
#                 or not self.call_is_active
#                 or my_generation != self.turn_generation
#             ):
#                 return

#             pipeline_start = time.perf_counter()
#             print(
#                 f"⏱️ [PIPELINE START] gen={my_generation}"
#             )
#             print(
#                 f"🤖 [CONTROLLED AI TURN] gen={my_generation} | "
#                 f"stage={self.conversation_stage} | "
#                 f"Customer='{user_text}'"
#             )

#             await self.safe_send({
#                 "type": "user_transcript",
#                 "sender": "Customer",
#                 "text": user_text,
#             })

#             config = await self.get_runtime_configuration()
#             config = config or self.default_brainex_configuration()

#             # Fast deterministic rules for explicit opt-out/callback phrases.
#             # These override uncertain model classifications.
#             forced = self.apply_explicit_customer_intent(user_text)

#             history_for_prompt = self.conversation_history[-6:]
#             history_text = "\n".join(
#                 f"{'AI' if item.get('role') == 'assistant' else 'Customer'}: "
#                 f"{_compact_text(item.get('text'), 160)}"
#                 for item in history_for_prompt
#             ) or "(No previous turns)"

#             profile_for_prompt = json.dumps(
#                 self.customer_profile,
#                 ensure_ascii=False,
#             )

#             stage_objective = self.get_stage_objective(
#                 self.conversation_stage
#             )

#             system_prompt = f"""
# You are the official AI telecalling assistant for {config['company']}.

# IDENTITY
# - Company: {config['company']}
# - Caller name: {config['bot_name']}
# - This is a commercial telephone conversation.
# - Be transparent that you are the company's AI calling assistant if identity
#   becomes relevant. Never pretend to be a human.

# COMPANY / PRODUCT KNOWLEDGE
# {config['details']}

# CURRENT CAMPAIGN / LEAD CONTEXT
# {self.lead_details or 'No additional lead context supplied.'}

# CURRENT CONVERSATION STAGE
# {self.conversation_stage}

# CURRENT STAGE OBJECTIVE
# {stage_objective}

# KNOWN CUSTOMER PROFILE
# {profile_for_prompt}

# SALES BEHAVIOUR
# - Your purpose is to understand whether the customer is interested in the
#   company's products/services and qualify the lead respectfully.
# - Keep every spoken reply natural and short for a phone call, normally
#   1-2 sentences and preferably under 25 spoken words.
# - Ask only ONE main question at a time.
# - Do not repeat a question whose answer is already known.
# - Do not behave like a general-purpose chatbot.
# - Keep unrelated questions brief and bring the discussion back to the
#   company offering.
# - Never invent fees, batches, schedules, discounts, guarantees, products,
#   approvals, locations, or any fact not present in COMPANY / PRODUCT
#   KNOWLEDGE or lead context.
# - If exact information is unavailable, say the company team can provide it.
# - If the customer clearly refuses, stop selling and close politely.
# - If the customer requests a callback, acknowledge it and close politely.
# - If the customer wants more details from a person, mark follow-up required.
# - If the customer clearly wants to purchase/join, mark READY_TO_JOIN.
# - Do not pressure the customer.

# STAGE GUIDANCE
# WAIT_PERMISSION:
#   Determine whether the customer permits the conversation to continue.
# PROFILE_COLLECTION:
#   Identify the customer type or relevant background.
# NEED_DISCOVERY:
#   Understand what they want to achieve or why they may need the offering.
# PRODUCT_EXPLANATION:
#   Explain only the most relevant product/service information.
# DETAIL_COLLECTION:
#   Collect useful customer details naturally, one question at a time.
# INTEREST_CHECK:
#   Find whether they are interested, need more information, want a callback,
#   or are ready to proceed.
# FOLLOW_UP:
#   Confirm that the company team should contact them.
# CLOSING:
#   Give a short polite closing; do not start another sales question.
# FINISHED:
#   Do not continue selling.

# Return ONLY a valid JSON object with exactly this shape:
# {{
#   "reply": "short spoken response",
#   "next_stage": "WAIT_PERMISSION|PROFILE_COLLECTION|NEED_DISCOVERY|PRODUCT_EXPLANATION|DETAIL_COLLECTION|INTEREST_CHECK|FOLLOW_UP|CLOSING|FINISHED",
#   "customer_profile": {{
#     "customer_type": null,
#     "education": null,
#     "occupation": null,
#     "interest_area": null,
#     "course_interest": null
#   }},
#   "interest_status": "UNDECIDED|NOT_INTERESTED|INTERESTED|NEED_MORE_INFORMATION|READY_TO_JOIN|CALLBACK_REQUESTED",
#   "needs_more_information": false,
#   "ready_to_join": false,
#   "callback_requested": false
# }}

# Rules for extraction:
# - Preserve previously known values unless the customer clearly changes them.
# - Use null for information not actually stated or safely inferred.
# - Never manufacture customer details.
# """

#             prompt = (
#                 f"{system_prompt}\n\n"
#                 f"RECENT CONVERSATION\n{history_text}\n\n"
#                 f"CURRENT CUSTOMER MESSAGE\nCustomer: {user_text}\n\n"
#                 "JSON:"
#             )

#             llama_start = time.perf_counter()
#             client = ollama.AsyncClient()

#             response = await client.generate(
#                 model="llama3.2",
#                 prompt=prompt,
#                 stream=False,
#                 format="json",
#                 keep_alive="30m",
#                 options={
#                     "temperature": 0.1,
#                     "num_predict": 110,
#                     "num_ctx": 2048,
#                     "num_thread": max(1, (os.cpu_count() or 4) - 1),
#                 },
#             )

#             if my_generation != self.turn_generation:
#                 print(
#                     f"🗑️ [STALE LLM RESULT DROPPED] gen={my_generation}"
#                 )
#                 return

#             raw_result = str(response.get("response", "") or "").strip()
#             decision = _extract_json_object(raw_result)

#             if not decision:
#                 print(
#                     "⚠️ [CONTROL JSON PARSE FAIL] "
#                     f"raw={_compact_text(raw_result, 300)}"
#                 )
#                 decision = {
#                     "reply": raw_result or "Could you please repeat that?",
#                     "next_stage": self.conversation_stage,
#                     "customer_profile": {},
#                     "interest_status": self.customer_profile[
#                         "interest_status"
#                     ],
#                 }

#             # Apply structured extraction before selecting final behavior.
#             self.apply_ai_decision(decision)

#             # Deterministic explicit user intent always wins.
#             if forced:
#                 self.apply_forced_intent(forced, config)

#             reply = _compact_text(decision.get("reply"), 220)

#             if forced == "NOT_INTERESTED":
#                 reply = (
#                     config.get("closing")
#                     or "Thank you for your time. Have a good day."
#                 )
#             elif forced == "CALLBACK_REQUESTED":
#                 reply = (
#                     "Certainly. I'll note that you would like a callback, "
#                     "and our team can contact you."
#                 )

#             if not reply:
#                 reply = "Could you please repeat that?"

#             # Keep configured/LLM speech telephone-friendly.
#             if len(reply) > 220:
#                 reply = reply[:220].rsplit(" ", 1)[0].rstrip(" ,;:")
#                 if reply and reply[-1] not in ".!?":
#                     reply += "."

#             # Memory is updated only for the latest accepted generation.
#             self.conversation_history.append({
#                 "role": "user",
#                 "text": user_text,
#             })
#             self.conversation_history.append({
#                 "role": "assistant",
#                 "text": reply,
#             })
#             if len(self.conversation_history) > 24:
#                 self.conversation_history = self.conversation_history[-24:]

#             print(
#                 f"⚡ [LLAMA CONTROL READY] "
#                 f"{time.perf_counter() - llama_start:.2f}s | "
#                 f"stage={self.conversation_stage} | "
#                 f"status={self.customer_profile['interest_status']} | "
#                 f"{reply}"
#             )

#             if (
#                 not self.is_connected
#                 or not self.call_is_active
#                 or my_generation != self.turn_generation
#             ):
#                 return

#             self.is_tts_generating = True
#             tts_start = time.perf_counter()
#             try:
#                 pcm = await generate_voice_pcm_bytes(reply)
#             finally:
#                 self.is_tts_generating = False

#             if my_generation != self.turn_generation:
#                 print(
#                     f"🗑️ [STALE TTS RESULT DROPPED] gen={my_generation}"
#                 )
#                 return

#             print(
#                 f"⚡ [TTS PIPELINE READY] "
#                 f"{time.perf_counter() - tts_start:.2f}s"
#             )

#             if not pcm:
#                 print("⚠️ [TTS EMPTY] No AI PCM generated.")
#                 return

#             if not self.is_connected or not self.call_is_active:
#                 print("🗑️ [AI AUDIO DROPPED] Call no longer active.")
#                 return

#             await self.safe_send({
#                 "type": "ai_response",
#                 "sender": "AI",
#                 "text": reply,
#                 "stage": self.conversation_stage,
#                 "interest_status": self.customer_profile[
#                     "interest_status"
#                 ],
#                 "customer_profile": self.customer_profile,
#             })

#             await self.stream_pcm_to_client(pcm, my_generation)

#             if (
#                 self.is_connected
#                 and self.call_is_active
#                 and my_generation == self.turn_generation
#             ):
#                 self.call_transcript_log.append(f"AI Agent: {reply}")
#                 print(
#                     f"✅ [CONTROLLED TURN COMPLETE] "
#                     f"total={time.perf_counter() - pipeline_start:.2f}s | "
#                     f"gen={my_generation}"
#                 )
#                 print(
#                     f"⏱️ [PIPELINE TOTAL] "
#                     f"{time.perf_counter() - pipeline_start:.2f}s"
#                 )

#         except asyncio.CancelledError:
#             self.is_ai_speaking = False
#             self.is_tts_generating = False
#             print(
#                 f"🛑 [AI TURN CANCELLED] gen={my_generation} | "
#                 f"customer='{user_text}'"
#             )
#             raise

#         except Exception as e:
#             self.is_ai_speaking = False
#             self.is_tts_generating = False
#             print(
#                 f"❌ [AI PIPELINE ERROR] "
#                 f"{type(e).__name__}: {e}"
#             )
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
                        reply
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
            client = ollama.AsyncClient()
            response = await client.generate(
                model="llama3.2",
                prompt=analyzer_prompt,
                stream=False,
                format="json",
                keep_alive="30m",
                options={
                    "temperature": 0.1,
                    "num_predict": 180,
                    "num_ctx": 2048,
                    "num_thread": max(1, (os.cpu_count() or 4) - 1),
                },
            )

            parsed = _extract_json_object(
                response.get("response", "")
            )
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

            if self.customer_pcm:
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

            if self.ai_pcm:
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
            script = CompanyScript.objects.filter(is_active=True).first()
            if script:
                return {
                    "config_source": "company_script",
                    "bot_name": script.bot_name,
                    "company": script.company_name,
                    "details": script.build_ai_knowledge(),
                    "greeting": script.opening_greeting,
                    "closing": script.closing_statement,
                    "followup_message": script.human_followup_message,
                    "default_language": script.default_language,
                    "company_phone": script.company_phone,
                    "whatsapp_number": script.whatsapp_number,
                    "company_email": script.company_email,
                    "company_address": script.company_address,
                    "website_url": script.website_url,
                    "google_maps_url": script.google_maps_url,
                    "whatsapp_url": script.whatsapp_url,
                    "instagram_url": script.instagram_url,
                    "facebook_url": script.facebook_url,
                    "youtube_url": script.youtube_url,
                    "products": [],
                    "collection_fields": [],
                    "company_id": self.company_id,
                    "telicall_line_id": self.telicall_line_id,
                }

        except Exception as e:
            print(f"⚠️ [DB CONFIG FETCH ERROR] {e}")

        return None

    async def get_active_script(self):
        """Backward-compatible alias for older code paths."""
        return await self.get_runtime_configuration()

