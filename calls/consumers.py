
import asyncio
import gc
import io
import json
import os
import re
import subprocess
import sys
import time
import warnings
import wave

# 🟢 Suppress non-critical PyTorch, TTS, and Transformer warnings
warnings.filterwarnings("ignore", category=UserWarning, module="torch.*")
warnings.filterwarnings("ignore", category=UserWarning, module="TTS.*")
warnings.filterwarnings("ignore", message=".*attention_mask.*")

# Set CPU thread limits prior to loading AI frameworks
num_cores = str(os.cpu_count() or 4)
os.environ["OMP_NUM_THREADS"] = num_cores
os.environ["MKL_NUM_THREADS"] = num_cores

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.files.base import ContentFile
from faster_whisper import WhisperModel
import numpy as np
import ollama
from pydub import AudioSegment
import requests
import soundfile as sf
import torch
import torchaudio
from TTS.api import TTS

from calls.models import CallSession, CompanyScript, Contact, SalesInsight

# Configure pydub binaries for Windows environment
AudioSegment.converter = (
    r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
)
AudioSegment.ffprobe = (
    r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
)


# Patch torchaudio load for systems with missing FFmpeg DLLs
def patched_torchaudio_load(filepath, *args, **kwargs):
  data, samplerate = sf.read(filepath, dtype="float32")
  tensor = torch.from_numpy(data).t()
  if tensor.ndim == 1:
    tensor = tensor.unsqueeze(0)
  return tensor, samplerate


torchaudio.load = patched_torchaudio_load
os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"

print("🧠 Loading Whisper Speech Engine inside Django...")
whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
print("✅ Whisper Bound to Django App!")

print("🗣️ Loading Coqui XTTS v2 Voice Cloning Model...")
custom_tts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(
    "cpu"
)
print("✅ Voice Cloning Engine Online!")

# Resolve path to custom_voice.wav in project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM_VOICE_SAMPLE = os.path.join(BASE_DIR, "custom_voice.wav")


def initialize_llama_engine():
  print("🦙 Checking Llama Engine Status...")
  try:
    res = requests.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
    if res.status_code == 200:
      print("✅ Llama Engine Online & Bound to Pipeline!")
      return
  except Exception:
    print("🚀 Llama Engine not detected. Auto-launching background process...")

  try:
    subprocess.Popen(
        ["ollama", "run", "llama3.2"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        ),
    )
    time.sleep(2)
    print("✅ Llama Engine automatically started & bound!")
  except Exception as e:
    print(f"❌ Error auto-starting Llama: {e}")


initialize_llama_engine()


async def generate_voice_pcm_bytes(text: str) -> bytes:
  """Generates cloned speech using Coqui XTTS v2 and outputs 16kHz PCM 16-bit mono."""
  cleaned_text = re.sub(r"[^\w\s.,?!']", "", text).strip()

  if not cleaned_text or len(cleaned_text) < 2:
    return b""

  if len(cleaned_text) > 250:
    cleaned_text = cleaned_text[:250]

  if cleaned_text[-1] not in [".", "!", "?", ","]:
    cleaned_text += "."

  if not os.path.exists(CUSTOM_VOICE_SAMPLE):
    print(f"❌ Custom voice sample missing at: {CUSTOM_VOICE_SAMPLE}")
    return b""

  try:

    def _synthesize_cloned_voice():
      output_wav = io.BytesIO()

      # Faster inference configuration for CPU execution
      custom_tts_model.tts_to_file(
          text=cleaned_text,
          speaker_wav=CUSTOM_VOICE_SAMPLE,
          language="en",
          speed=1.2,
          gpt_cond_len=3,
          file_path=output_wav,
      )

      output_wav.seek(0)
      audio = AudioSegment.from_file(output_wav, format="wav")
      audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
      raw_data = audio.raw_data

      if len(raw_data) % 2 != 0:
        raw_data = raw_data[:-1]

      return raw_data

    return await asyncio.to_thread(_synthesize_cloned_voice)

  except Exception as e:
    print(f"⚠️ Custom voice generation error: {e}")
    return b""


def split_into_sentences(text_buffer: str):
  sentences = re.split(r"(?<=[.!?])\s+", text_buffer)
  if len(sentences) > 1:
    return sentences[:-1], sentences[-1]
  return [], text_buffer


class MediaStreamConsumer(AsyncWebsocketConsumer):

  async def connect(self):
    try:
      await self.accept()

      self.is_connected = True
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
      self.audio_buffer = bytearray()

      self.silence_start_time = None
      self.is_user_talking = False

      # Customer VAD / endpointing
      # 16 kHz PCM16 mono endpointing.
      self.ENERGY_THRESHOLD = 300
      self.SILENCE_DURATION_SEC = 0.5
      self.MAX_BUFFER_BYTES = 64000
      self.PRE_ROLL_MAX_BYTES = 8192
      self.pre_roll_buffer = bytearray()

      # Inactivity watchdog
      self.INACTIVITY_TIMEOUT_SECONDS = 120.0
      self.last_activity_time = time.time()

      # Serialize expensive pipeline stages
      self.stt_processing_lock = asyncio.Lock()
      self.ai_processing_lock = asyncio.Lock()
      self.tts_processing_lock = asyncio.Lock()

      # Track tasks so disconnect can cancel cleanly
      self.background_tasks = set()

      self.timeout_checker_task = self.create_background_task(
          self.monitor_inactivity_timeout()
      )

      print(
          "🌐 [WS CONNECT] "
          "Single synchronized pipeline channel established."
      )

    except Exception as e:
      print(
          f"❌ [WS CONNECT ERROR]: "
          f"{type(e).__name__}: {e}"
      )
      self.is_connected = False
      await self.close()

  def create_background_task(self, coro):
    task = asyncio.create_task(coro)

    self.background_tasks.add(task)

    task.add_done_callback(
        self.background_tasks.discard
    )

    return task
  async def monitor_inactivity_timeout(self):
    try:
      while self.is_connected:
        await asyncio.sleep(5)
        current_time = time.time()
        if (
            current_time - self.last_activity_time
        ) > self.INACTIVITY_TIMEOUT_SECONDS:
          print("⏰ [TIMEOUT] Inactivity threshold hit.")
          await self.safe_send(
              {"event": "ai_token", "text": "[SESSION TIMEOUT]"}
          )
          await self.close()
          break
    except asyncio.CancelledError:
      pass


  async def disconnect(self, close_code):
    print(
        f"🔌 [WS DISCONNECT] "
        f"Connection severed. Code: {close_code}"
    )

    # --------------------------------------------------
    # 1. MARK CONNECTION CLOSED
    # --------------------------------------------------
    self.is_connected = False
    self.greeting_sent = False

    # --------------------------------------------------
    # 2. CANCEL TIMEOUT CHECKER
    # --------------------------------------------------
    if hasattr(self, "timeout_checker_task"):
        self.timeout_checker_task.cancel()

    # --------------------------------------------------
    # 3. CANCEL STT / LLAMA / TTS / GREETING TASKS
    # --------------------------------------------------
    if hasattr(self, "background_tasks"):
        current_task = asyncio.current_task()

        tasks_to_cancel = [
            task
            for task in list(self.background_tasks)
            if task is not current_task
            and not task.done()
        ]

        for task in tasks_to_cancel:
            task.cancel()

        if tasks_to_cancel:
            print(
                f"🛑 [TASK CLEANUP] "
                f"Cancelling {len(tasks_to_cancel)} "
                f"background task(s)..."
            )

            await asyncio.gather(
                *tasks_to_cancel,
                return_exceptions=True,
            )

        self.background_tasks.clear()

    # --------------------------------------------------
    # 4. CALCULATE CALL DURATION
    # --------------------------------------------------
    duration = time.time() - self.start_time

    # --------------------------------------------------
    # 5. SAVE RECORDINGS + SESSION
    # --------------------------------------------------
    if self.session_id:
        try:
            await self.finalize_call_session(
                self.session_id,
                duration,
                self.call_transcript_log,
            )

            print(
                f"✅ [SESSION FINALIZED] "
                f"ID: {self.session_id}"
            )

        except Exception as e:
            print(
                f"❌ [FINALIZE SESSION ERROR]: "
                f"{type(e).__name__}: {e}"
            )

    # --------------------------------------------------
    # 6. CLEAR STT BUFFER
    # --------------------------------------------------
    self.audio_buffer.clear()
    if hasattr(self, "pre_roll_buffer"):
        self.pre_roll_buffer.clear()

    print(
        "✅ [WS CLEANUP COMPLETE]"
    )

  async def receive(self, text_data=None, bytes_data=None):
    # This is intentionally the first diagnostic in receive().
    # It proves whether Daphne/Channels delivered a text or binary WS frame.
    print(
        "📨 [WS RECEIVE ENTER] "
        f"text={'YES' if text_data is not None else 'NO'} | "
        f"bytes={'YES' if bytes_data is not None else 'NO'} | "
        f"byte_len={len(bytes_data) if bytes_data is not None else 0}"
    )

    self.last_activity_time = time.time()

    # ==========================================================
    # 1. RAW BINARY WEBSOCKET FRAME = CUSTOMER PCM
    # ==========================================================
    # Handle binary FIRST so no JSON/text return can bypass it.
    if bytes_data is not None:
      await self.handle_customer_pcm(bytes_data)
      return

    # ==========================================================
    # 2. TEXT / JSON WEBSOCKET FRAME
    # ==========================================================
    if text_data is None:
      return

    try:
      parsed_json = json.loads(text_data)

    except json.JSONDecodeError:
      clean_text = text_data.strip()

      if (
          clean_text
          and not clean_text.startswith("{")
          and clean_text not in [
              "HELLO_SERVER",
              "__SYSTEM_CONNECTION_INITIALIZED__",
          ]
      ):
        self.create_background_task(
            self.safe_process_text_inference(clean_text)
        )
      return

    # Legacy/base64 customer audio support. Decode it and route it
    # through exactly the same PCM handler as raw WS binary frames.
    if "audio" in parsed_json:
      try:
        import base64

        decoded_audio = base64.b64decode(
            parsed_json["audio"],
            validate=True,
        )

        print(
            f"📦 [BASE64 AUDIO DECODED] "
            f"bytes={len(decoded_audio)}"
        )

        await self.handle_customer_pcm(decoded_audio)

      except Exception as e:
        print(
            f"❌ [BASE64 AUDIO ERROR]: "
            f"{type(e).__name__}: {e}"
        )
      return

    # Metadata binds the websocket to one call/session.
    if (
        "client_phone_number" in parsed_json
        or parsed_json.get("event") == "client_phone_number"
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
          f"📱 [METADATA BOUND] "
          f"Session active. Phone: {self.client_phone} | "
          f"DB ID: {self.session_id}"
      )

      if not self.greeting_sent:
        self.greeting_sent = True
        self.create_background_task(
            self.trigger_initial_greeting()
        )
      return

    if parsed_json.get("event") == "call_answered":
      if not self.greeting_sent:
        self.greeting_sent = True
        self.create_background_task(
            self.trigger_initial_greeting()
        )
      return

    if parsed_json.get("event") == "call_state_changed":
      print(
          "📞 [TELEMETRY REGISTRY] Hardware Line Changed: "
          f"{parsed_json.get('state')}"
      )
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

    if user_text and user_text not in [
        "HELLO_SERVER",
        "__SYSTEM_CONNECTION_INITIALIZED__",
    ]:
      self.create_background_task(
          self.safe_process_text_inference(user_text)
      )

  async def handle_customer_pcm(self, bytes_data):
    """Store, meter, endpoint and dispatch one customer PCM frame."""
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

    if len(frame) % 2 != 0:
      frame = frame[:-1]
    if not frame:
      return

    # Preserve the complete downlink for the final customer WAV.
    self.customer_pcm.extend(frame)

    if not self.is_connected:
      return

    audio_frame = np.frombuffer(frame, dtype=np.int16)
    if audio_frame.size == 0:
      return

    rms_energy = float(
        np.sqrt(np.mean(audio_frame.astype(np.float32) ** 2))
    )
    current_time = time.time()
    speech_frame = rms_energy >= self.ENERGY_THRESHOLD

    # While idle, retain only a short pre-roll. Silence is no longer
    # allowed to grow the Whisper buffer to ~96 KB.
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
        print(
            f"📥 [PCM RX] frame={len(frame)} | "
            f"recorded={len(self.customer_pcm)} | "
            f"stt_buffer=0 | rms={rms_energy:.2f} | "
            f"ai_speaking={self.is_ai_speaking}"
        )
        return
    else:
      self.audio_buffer.extend(frame)

    if speech_frame:
      self.silence_start_time = None
    elif self.silence_start_time is None:
      self.silence_start_time = current_time

    silence_duration = (
        current_time - self.silence_start_time
        if self.silence_start_time is not None
        else 0.0
    )

    print(
        f"📥 [PCM RX] frame={len(frame)} | "
        f"recorded={len(self.customer_pcm)} | "
        f"stt_buffer={len(self.audio_buffer)} | "
        f"rms={rms_energy:.2f} | "
        f"silence={silence_duration:.2f}s | "
        f"ai_speaking={self.is_ai_speaking}"
    )

    should_dispatch = (
        silence_duration >= self.SILENCE_DURATION_SEC
        or len(self.audio_buffer) >= self.MAX_BUFFER_BYTES
    )

    if should_dispatch:
      raw_buffer = bytes(self.audio_buffer)
      self.audio_buffer.clear()
      self.is_user_talking = False
      self.silence_start_time = None
      self.pre_roll_buffer.clear()

      if len(raw_buffer) <= 4000:
        return

      print(
          f"🎙️ [SPEECH CHUNK READY] "
          f"Processing {len(raw_buffer)} bytes..."
      )
      self.create_background_task(
          self.safe_process_audio_transcription(raw_buffer)
      )

  async def safe_process_audio_transcription(
    self,
    raw_audio_bytes,
):
    async with self.stt_processing_lock:

        if not self.is_connected:
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
                f"❌ [STT PIPELINE ERROR]: "
                f"{type(e).__name__}: {e}"
            )
  
  async def process_audio_transcription(
      self,
      raw_audio_bytes,
  ):
      try:
          print(
              f"🧠 [STT START] "
              f"PCM bytes={len(raw_audio_bytes)} | "
              f"ai_speaking={self.is_ai_speaking}"
          )

          pcm_int16 = np.frombuffer(
              raw_audio_bytes,
              dtype=np.int16,
          )

          if len(pcm_int16) == 0:
              print(
                  "⚠️ [STT EMPTY] "
                  "No PCM samples received."
              )
              return

          audio_float32 = (
              pcm_int16.astype(np.float32)
              / 32768.0
          )

          max_peak = np.max(
              np.abs(audio_float32)
          )

          rms = np.sqrt(
              np.mean(
                  pcm_int16.astype(np.float32) ** 2
              )
          )

          print(
              f"🧠 [STT AUDIO] "
              f"samples={len(pcm_int16)} | "
              f"peak={max_peak:.4f} | "
              f"rms={rms:.2f}"
          )

          # Ignore essentially silent chunks
          if rms < 20:
              print(
                  f"⚠️ [STT SKIP SILENCE] "
                  f"rms={rms:.2f}"
              )
              return

          # Gain normalization
          if max_peak > 0.01:
              audio_float32 = (
                  audio_float32 / max_peak
              )

          print("🧠 [WHISPER START]")

          segments, info = await asyncio.to_thread(
              whisper_model.transcribe,
              audio_float32,
              beam_size=1,
              language="en",
              condition_on_previous_text=False,
              vad_filter=True,
              vad_parameters={
                  "min_silence_duration_ms": 300,
              },
          )

          segments = list(segments)

          print(
              f"🧠 [WHISPER SEGMENTS] "
              f"count={len(segments)}"
          )

          user_text = "".join(
              segment.text
              for segment in segments
          ).strip()

          print(
              f"🗣️ [WHISPER RESULT]: "
              f"'{user_text}'"
          )

          clean_check = re.sub(
              r"[^\w\s]",
              "",
              user_text.lower(),
          ).strip()

          hallucinations = [
              "you you",
              "thank you",
              "subtitles",
              "bye",
              "amaraorg",
              "mb",
              "thank you for watching",
              "thanks for watching",
          ]

          words = clean_check.split()

          # Telephone conversations contain valid one-word turns.
          allowed_single_words = {
              "hello", "hi", "yes", "no", "okay", "ok",
              "sure", "correct", "right", "wait", "repeat",
          }
          useful_transcript = (
              bool(user_text)
              and len(clean_check) >= 2
              and clean_check not in hallucinations
              and (
                  len(words) >= 2
                  or clean_check in allowed_single_words
              )
          )

          if useful_transcript:
              print(
                  f"✅ [CUSTOMER TRANSCRIPT]: "
                  f"{user_text}"
              )

              self.call_transcript_log.append(
                  f"Customer: {user_text}"
              )

              if self.is_connected:
                  self.create_background_task(
                      self.safe_process_text_inference(
                          user_text
                      )
                  )

          else:
              print(
                  f"⚠️ [WHISPER IGNORED]: "
                  f"'{user_text}'"
              )

      except asyncio.CancelledError:
          raise

      except Exception as e:
          print(
              f"❌ [TRANSCRIPTION FAIL]: "
              f"{type(e).__name__}: {e}"
          )

  async def safe_process_text_inference(
    self,
    user_text,
):
    async with self.ai_processing_lock:

        if not self.is_connected:
            return

        try:
            print(
                f"🔒 [AI LOCK ACQUIRED] "
                f"Processing: '{user_text}'"
            )

            await self.process_text_inference(
                user_text
            )

        except asyncio.CancelledError:
            print(
                "⚠️ [AI PIPELINE TASK CANCELLED]"
            )
            raise

        except Exception as e:
            print(
                f"❌ [AI PIPELINE ERROR]: "
                f"{type(e).__name__}: {e}"
            )

        finally:
            print(
                "🔓 [AI LOCK RELEASED]"
            )
  async def trigger_initial_greeting(self):
    try:
      await asyncio.sleep(0.4)

      if not self.is_connected:
        return

      script_data = await self.get_active_script()

      greeting_text = (
          script_data["greeting"]
          if script_data
          else "Hello! How can I help you today?"
      )

      if not self.is_connected:
        return

      print(
          f"🗣️ [INITIAL GREETING]: "
          f"{greeting_text}"
      )

      self.call_transcript_log.append(
          f"AI Agent: {greeting_text}"
      )

      await self.safe_send({
          "type": "ai_response",
          "sender": "AI",
          "text": greeting_text,
      })

      try:
        # The synthesis itself is already offloaded with
        # asyncio.to_thread() inside generate_voice_pcm_bytes().
        # The lock prevents greeting TTS and response TTS from
        # hitting XTTS concurrently.
        self.is_tts_generating = True
        try:
          async with self.tts_processing_lock:
            pcm_bytes = await generate_voice_pcm_bytes(
                greeting_text
            )
        finally:
          self.is_tts_generating = False

        if pcm_bytes and self.is_connected:
          self.is_ai_speaking = True
          self.ai_pcm.extend(pcm_bytes)
          await self.send(bytes_data=pcm_bytes)
          await asyncio.sleep(len(pcm_bytes) / 32000.0)

      except asyncio.CancelledError:
        print("⚠️ [GREETING TASK CANCELLED]")
        raise

      except Exception as e:
        print(
            f"⚠️ [GREETING AUDIO ERROR]: "
            f"{type(e).__name__}: {e}"
        )

      finally:
        self.is_ai_speaking = False
        print(
            "🎧 [CUSTOMER DOWNLINK READY] "
            "Waiting for remote customer audio..."
        )

    except asyncio.CancelledError:
      raise

    except Exception as e:
      self.is_ai_speaking = False
      print(
          f"❌ [GREETING ERROR]: "
          f"{type(e).__name__}: {e}"
      )

  async def process_text_inference(self, user_text):
    print(f"🤖 [LLAMA TRIGGERED] Processing: '{user_text}'")

    await self.safe_send({
        "type": "user_transcript",
        "sender": "Customer",
        "text": user_text,
    })

    script_data = await self.get_active_script()
    system_prompt = (
        f"You are {script_data['bot_name']}, AI Agent for"
        f" {script_data['company']}. Details: {script_data['details']}. Keep"
        " answers under 2 short sentences."
        if script_data
        else (
            "You are a helpful AI sales assistant. Keep responses under 2"
            " sentences."
        )
    )

    prompt_context = (
        f"{system_prompt}\n\nLead Context:"
        f" {self.lead_details}\nCustomer: {user_text}\nAI:"
    )

    try:
      client = ollama.AsyncClient()
      response_stream = await client.generate(
          model="llama3.2", prompt=prompt_context, stream=True
      )

      accumulated_text = ""
      sentence_buffer = ""

      async for chunk in response_stream:
        if not self.is_connected:
          break

        token = chunk.get("response", "")
        accumulated_text += token
        sentence_buffer += token

        sentences, sentence_buffer = split_into_sentences(sentence_buffer)

        for sentence in sentences:
          if sentence.strip():
            self.is_tts_generating = True
            try:
              async with self.tts_processing_lock:
                pcm_bytes = await generate_voice_pcm_bytes(sentence)
            finally:
              self.is_tts_generating = False

            if pcm_bytes and self.is_connected:
              self.is_ai_speaking = True
              try:
                self.ai_pcm.extend(pcm_bytes)
                await self.send(bytes_data=pcm_bytes)
                await asyncio.sleep(len(pcm_bytes) / 32000.0)
              finally:
                self.is_ai_speaking = False

      if sentence_buffer.strip() and self.is_connected:
        self.is_tts_generating = True
        try:
          async with self.tts_processing_lock:
            pcm_bytes = await generate_voice_pcm_bytes(sentence_buffer)
        finally:
          self.is_tts_generating = False

        if pcm_bytes and self.is_connected:
          self.is_ai_speaking = True
          try:
            self.ai_pcm.extend(pcm_bytes)
            await self.send(bytes_data=pcm_bytes)
            await asyncio.sleep(len(pcm_bytes) / 32000.0)
          finally:
            self.is_ai_speaking = False

      if accumulated_text and self.is_connected:
        print(f"🗣️ [AI RESPONSE COMPLETE]: {accumulated_text.strip()}")
        self.call_transcript_log.append(
            f"AI Agent: {accumulated_text.strip()}"
        )
        await self.safe_send({
            "type": "ai_response",
            "sender": "AI",
            "text": accumulated_text.strip(),
        })

    except Exception as e:
      print(f"⚠️ [INFERENCE EXCEPTION]: {e}")
    finally:
      self.is_ai_speaking = False

  async def safe_send(self, payload: dict):
    if hasattr(self, "is_connected") and self.is_connected:
      try:
        await self.send(text_data=json.dumps(payload))
      except Exception:
        self.is_connected = False

  @database_sync_to_async
  def create_call_session(self, phone_number):
    try:
      if not phone_number:
        return None
      contact, _ = Contact.objects.get_or_create(phone_number=phone_number)
      session = CallSession.objects.create(contact=contact, status="active")
      return session.id
    except Exception as e:
      print(f"⚠️ [DB CREATE SESSION ERROR]: {e}")
      return None

  @database_sync_to_async
  def finalize_call_session(self, session_id, duration, transcript_log):
    try:
      if not session_id:
        return
      session = CallSession.objects.get(id=session_id)
      session.duration_seconds = int(duration)
      session.status = "completed"

      timestamp = int(time.time())

      # Save Customer Audio stream safely
      if len(self.customer_pcm) > 0:
        raw_cust = bytes(self.customer_pcm)
        if len(raw_cust) % 2 != 0:
          raw_cust = raw_cust[:-1]

        cust_io = io.BytesIO()
        with wave.open(cust_io, "wb") as wav_file:
          wav_file.setnchannels(1)
          wav_file.setsampwidth(2)
          wav_file.setframerate(16000)
          wav_file.writeframes(raw_cust)

        cust_filename = f"customer_{session_id}_{timestamp}.wav"
        session.recording_file.save(
            cust_filename, ContentFile(cust_io.getvalue()), save=False
        )

      # Save AI Audio stream safely
      if len(self.ai_pcm) > 0:
        raw_ai = bytes(self.ai_pcm)
        if len(raw_ai) % 2 != 0:
          raw_ai = raw_ai[:-1]

        ai_io = io.BytesIO()
        with wave.open(ai_io, "wb") as wav_file:
          wav_file.setnchannels(1)
          wav_file.setsampwidth(2)
          wav_file.setframerate(16000)
          wav_file.writeframes(raw_ai)

        ai_filename = f"ai_{session_id}_{timestamp}.wav"
        session.ai_recording_file.save(
            ai_filename, ContentFile(ai_io.getvalue()), save=False
        )

      session.save()
      print(f"✅ [DB SESSION SAVED] Dual recordings saved for ID: {session_id}")
    except Exception as e:
      print(f"⚠️ [DB FINALIZE ERROR]: {e}")

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
      print(f"⚠️ [DB SCRIPT FETCH ERROR]: {e}")
    return None