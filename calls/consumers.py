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

# Keep the same FFmpeg paths that are already working in the current backend.
AudioSegment.converter = (
    r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
)
AudioSegment.ffprobe = (
    r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
)

from calls.models import CallSession, CompanyScript, Contact


# ---------------------------------------------------------------------------
# GLOBAL ENGINES
# ---------------------------------------------------------------------------

print("🧠 Loading Whisper Speech Engine inside Django...")
whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
print("✅ Whisper Bound to Django App!")

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

            self.customer_pcm = bytearray()
            self.ai_pcm = bytearray()

            # ----------------------------
            # Customer VAD / endpointing
            # ----------------------------
            self.audio_buffer = bytearray()
            self.pre_roll_buffer = bytearray()
            self.silence_start_time = None
            self.is_user_talking = False

            self.ENERGY_THRESHOLD = 300
            self.SILENCE_DURATION_SEC = 0.45
            self.MAX_BUFFER_BYTES = 64000
            self.PRE_ROLL_MAX_BYTES = 8192

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
                await self.finalize_call_session(
                    self.session_id,
                    duration,
                    self.call_transcript_log,
                )
                print(f"✅ [SESSION FINALIZED] ID: {self.session_id}")
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

            print(
                f"🧠 [STT AUDIO] samples={len(pcm_int16)} | "
                f"peak={max_peak:.4f} | rms={rms:.2f}"
            )

            if max_peak > 0.01:
                audio_float32 = audio_float32 / max_peak

            segments, _ = await asyncio.to_thread(
                whisper_model.transcribe,
                audio_float32,
                beam_size=1,
                language="en",
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 250},
            )

            segments = list(segments)
            user_text = "".join(s.text for s in segments).strip()

            print(
                f"🧠 [WHISPER DONE] {time.perf_counter() - start:.2f}s | "
                f"segments={len(segments)}"
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

            script_data = await self.get_active_script()

            greeting = (
                script_data["greeting"].strip()
                if script_data and script_data.get("greeting")
                else "Hello. How can I help you?"
            )

            # Keep initial greeting short for telephone responsiveness.
            if len(greeting) > 100:
                greeting = "Hello. How can I help you today?"

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

    async def process_text_inference(
        self,
        user_text: str,
        my_generation: int,
    ):
        try:
            if (
                not self.is_connected
                or not self.call_is_active
                or my_generation != self.turn_generation
            ):
                return

            pipeline_start = time.perf_counter()
            print(
                f"🤖 [LATEST AI TURN] gen={my_generation} | "
                f"Customer='{user_text}'"
            )

            await self.safe_send({
                "type": "user_transcript",
                "sender": "Customer",
                "text": user_text,
            })

            script_data = await self.get_active_script()

            if script_data:
                system_prompt = (
                    f"You are {script_data['bot_name']}, a phone-call AI agent "
                    f"for {script_data['company']}. "
                    f"Company details: {script_data['details']}. "
                    "Reply naturally in ONE short spoken sentence, ideally "
                    "5-12 words. Never give long explanations. Do not mention "
                    "being an AI unless asked."
                )
            else:
                system_prompt = (
                    "You are a helpful phone-call sales assistant. "
                    "Reply naturally in ONE short spoken sentence, ideally "
                    "5-12 words. Never give a long explanation."
                )

            prompt = (
                f"{system_prompt}\n"
                f"Lead context: {self.lead_details}\n"
                f"Customer: {user_text}\n"
                "Assistant:"
            )

            llama_start = time.perf_counter()
            client = ollama.AsyncClient()

            response = await client.generate(
                model="llama3.2",
                prompt=prompt,
                stream=False,
                options={
                    "temperature": 0.4,
                    "num_predict": 40,
                },
            )

            if my_generation != self.turn_generation:
                print(
                    f"🗑️ [STALE LLM RESULT DROPPED] gen={my_generation}"
                )
                return

            reply = str(response.get("response", "") or "").strip()
            reply = re.sub(r"\s+", " ", reply)

            # Hard telephone-length guard.
            if len(reply) > 150:
                reply = reply[:150].rsplit(" ", 1)[0].rstrip(" ,;:")
                if reply and reply[-1] not in ".!?":
                    reply += "."

            if not reply:
                reply = "Could you please repeat that?"

            print(
                f"⚡ [LLAMA READY] "
                f"{time.perf_counter() - llama_start:.2f}s | {reply}"
            )

            if (
                not self.is_connected
                or not self.call_is_active
                or my_generation != self.turn_generation
            ):
                return

            self.is_tts_generating = True
            tts_start = time.perf_counter()
            try:
                pcm = await generate_voice_pcm_bytes(reply)
            finally:
                self.is_tts_generating = False

            if my_generation != self.turn_generation:
                print(
                    f"🗑️ [STALE TTS RESULT DROPPED] gen={my_generation}"
                )
                return

            print(
                f"⚡ [TTS PIPELINE READY] "
                f"{time.perf_counter() - tts_start:.2f}s"
            )

            if not pcm:
                print("⚠️ [TTS EMPTY] No AI PCM generated.")
                return

            if not self.is_connected or not self.call_is_active:
                print("🗑️ [AI AUDIO DROPPED] Call no longer active.")
                return

            # Send text before/alongside playback for UI visibility.
            await self.safe_send({
                "type": "ai_response",
                "sender": "AI",
                "text": reply,
            })

            await self.stream_pcm_to_client(pcm, my_generation)

            if (
                self.is_connected
                and self.call_is_active
                and my_generation == self.turn_generation
            ):
                self.call_transcript_log.append(f"AI Agent: {reply}")
                print(
                    f"✅ [REALTIME TURN COMPLETE] "
                    f"total={time.perf_counter() - pipeline_start:.2f}s | "
                    f"gen={my_generation}"
                )

        except asyncio.CancelledError:
            self.is_ai_speaking = False
            self.is_tts_generating = False
            print(
                f"🛑 [AI TURN CANCELLED] gen={my_generation} | "
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
            print(
                f"✅ [DB SESSION SAVED] Dual recordings saved for "
                f"ID: {session_id}"
            )

        except Exception as e:
            print(f"⚠️ [DB FINALIZE ERROR] {e}")

    @database_sync_to_async
    def get_active_script(self):
        try:
            script = CompanyScript.objects.filter(is_active=True).first()
            if script:
                return {
                    "bot_name": script.bot_name,
                    "company": script.company_name,
                    "details": script.company_details,
                    "greeting": script.opening_greeting,
                    "closing": script.closing_statement,
                }

        except Exception as e:
            print(f"⚠️ [DB SCRIPT FETCH ERROR] {e}")

        return None
