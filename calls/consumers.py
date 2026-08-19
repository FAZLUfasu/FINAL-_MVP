# import subprocess
# import os
# import sys
# import torch
# import torchaudio
# import soundfile as sf
# import time
# import io
# import gc
# import json
# import re
# import asyncio
# import numpy as np
# import edge_tts
# import requests
# from pydub import AudioSegment
# from channels.generic.websocket import AsyncWebsocketConsumer
# from channels.db import database_sync_to_async
# import pydub

# # Point pydub directly to your WinGet ffmpeg / ffprobe binaries
# pydub.AudioSegment.converter = r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
# pydub.AudioSegment.ffprobe = r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"

# # ⚡ Enable PyTorch CPU Multi-Threading for Fast Inference
# num_cores = os.cpu_count() or 4
# torch.set_num_threads(num_cores)
# torch.set_num_interop_threads(num_cores)

# # Patching torchaudio load for systems with missing FFmpeg DLLs
# def patched_torchaudio_load(filepath, *args, **kwargs):
#     data, samplerate = sf.read(filepath, dtype='float32')
#     tensor = torch.from_numpy(data).t()
#     if tensor.ndim == 1:
#         tensor = tensor.unsqueeze(0)
#     return tensor, samplerate

# torchaudio.load = patched_torchaudio_load
# os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"

# from calls.models import CallSession, CompanyScript, SalesInsight, Contact
# from faster_whisper import WhisperModel
# import ollama

# print("🧠 Loading Whisper Speech Engine inside Django...")
# # Quantized tiny model with VAD enabled
# whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
# print("✅ Whisper Bound to Django App!")

# print("🎙️ Initializing Ultra-Fast Edge Neural Voice Engine...")
# def initialize_llama_engine():
#     print("🦙 Checking Llama Engine Status...")
#     try:
#         # Check if Ollama service is active
#         res = requests.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
#         if res.status_code == 200:
#             print("✅ Llama Engine Online & Bound to Pipeline!")
#             return
#     except Exception:
#         print("🚀 Llama Engine not detected. Auto-launching Ollama background process...")

#     try:
#         # Launch Ollama in the background automatically
#         subprocess.Popen(
#             ["ollama", "run", "llama3.2"],
#             stdout=subprocess.DEVNULL,
#             stderr=subprocess.DEVNULL,
#             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
#         )
#         time.sleep(2)
#         print("✅ Llama Engine automatically started & bound!")
#     except FileNotFoundError:
#         print("❌ Could not auto-start Llama! Ensure Ollama is installed and added to PATH.")
#     except Exception as e:
#         print(f"❌ Error auto-starting Llama: {e}")

# # Run Llama initialization on module load
# initialize_llama_engine()

# async def generate_voice_pcm_bytes(text: str) -> bytes:
#     """Generates natural human neural voice and converts MP3 to raw 16kHz PCM 16-bit mono for phone baseband."""
#     cleaned_text = re.sub(r"[^\w\s.,?!']", "", text).strip()
    
#     if not cleaned_text or len(cleaned_text) < 3:
#         return b""
        
#     if len(cleaned_text) > 250:
#         cleaned_text = cleaned_text[:250]
        
#     if cleaned_text[-1] not in [".", "!", "?", ","]:
#         cleaned_text += "."

#     try:
#         # 1. Fetch MP3 audio stream from Edge-TTS
#         communicate = edge_tts.Communicate(cleaned_text, "en-US-GuyNeural")
#         mp3_io = io.BytesIO()
#         async for chunk in communicate.stream():
#             if chunk["type"] == "audio":
#                 mp3_io.write(chunk["data"])
        
#         mp3_io.seek(0)
        
#         # 2. Convert MP3 to 16kHz PCM 16-bit Mono (Required for SIM Call Uplink)
#         audio = AudioSegment.from_file(mp3_io, format="mp3")
#         audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        
#         return audio.raw_data

#     except Exception as e:
#         print(f"⚠️ Voice generation exception: {e}")
#         return b""


# def split_into_sentences(text_buffer):
#     sentences = re.split(r'(?<=[.!?])\s+', text_buffer)
#     if len(sentences) > 1:
#         return sentences[:-1], sentences[-1]
#     return [], text_buffer


# class MediaStreamConsumer(AsyncWebsocketConsumer):

#     async def connect(self):
#         try:
#             await self.accept()
#             self.is_connected = True
#             self.greeting_sent = False  # Guard flag to prevent duplicate greetings
#             self.client_phone = None
#             self.session_id = None
#             self.lead_details = ""
#             self.call_transcript_log = []
#             self.start_time = time.time()
            
#             # 🔑 REAL-TIME VAD & BUFFER PARAMETERS (16kHz, 16-bit Mono = 32,000 bytes/sec)
#             self.audio_buffer = bytearray()
#             self.silence_start_time = None
#             self.is_user_talking = False
#             self.ENERGY_THRESHOLD = 300       # Sensitivity cutoff for voice presence
#             self.SILENCE_DURATION_SEC = 0.8  # Dispatch STT after 800ms of post-speech silence
#             self.MAX_BUFFER_BYTES = 128000    # Hard cap (~4 seconds) to prevent huge Whisper latencies
            
#             self.INACTIVITY_TIMEOUT_SECONDS = 120.0
#             self.last_activity_time = time.time()
            
#             self.timeout_checker_task = asyncio.create_task(self.monitor_inactivity_timeout())
#             print("🌐 [WS CONNECT] Single synchronized pipeline channel established.")
#         except Exception as e:
#             print(f"❌ [WS CONNECT ERROR]: {e}")
#             await self.close()

#     async def monitor_inactivity_timeout(self):
#         try:
#             while self.is_connected:
#                 await asyncio.sleep(5)
#                 current_time = time.time()
#                 if (current_time - self.last_activity_time) > self.INACTIVITY_TIMEOUT_SECONDS:
#                     print("⏰ [TIMEOUT] Inactivity threshold hit. Terminating call session.")
#                     await self.safe_send({"event": "ai_token", "text": "[SESSION TIMEOUT]"})
#                     await self.close()
#                     break
#         except asyncio.CancelledError:
#             pass

#     async def disconnect(self, close_code):
#         self.is_connected = False
#         self.greeting_sent = False
#         if hasattr(self, 'timeout_checker_task'):
#             self.timeout_checker_task.cancel()
            
#         duration = time.time() - self.start_time
#         print(f"🔌 [WS DISCONNECT] Connection severed safely. Code: {close_code}")
        
#         if self.session_id:
#             asyncio.create_task(self.finalize_call_session(self.session_id, duration, self.call_transcript_log))
        
#         self.audio_buffer.clear()

#     async def receive(self, text_data=None, bytes_data=None):
#         self.last_activity_time = time.time()

#         # ------------------------------------------------------------------
#         # 1. HANDLE JSON / TEXT TELEMETRY & CONTROL MESSAGES
#         # ------------------------------------------------------------------
#         if text_data:
#             try:
#                 parsed_json = json.loads(text_data)
                
#                 # Metadata initialization
#                 if "client_phone_number" in parsed_json or parsed_json.get("event") == "client_phone_number":
#                     self.client_phone = parsed_json.get("client_phone_number")
#                     self.lead_details = parsed_json.get("lead_details", parsed_json.get("details", ""))
#                     self.session_id = await self.create_call_session(self.client_phone)
#                     print(f"📱 [METADATA BOUND] Session active. Phone ID: {self.client_phone} | Details: {self.lead_details}")
                    
#                     # Immediate greeting trigger upon metadata binding if line is active
#                     if not self.greeting_sent:
#                         self.greeting_sent = True
#                         asyncio.create_task(self.trigger_initial_greeting())
#                     return

#                 # Explicit call answered signal
#                 if parsed_json.get("event") == "call_answered":
#                     if not self.greeting_sent:
#                         self.greeting_sent = True
#                         print("✅ [CALL ANSWERED] Line active! Triggering initial greeting...")
#                         asyncio.create_task(self.trigger_initial_greeting())
#                     return

#                 # Telemetry
#                 if parsed_json.get("event") == "call_state_changed":
#                     state = parsed_json.get("state")
#                     print(f"📞 [TELEMETRY REGISTRY] Hardware Line Changed: {state}")
#                     return

#                 # Text Injection
#                 user_text = parsed_json.get("text", parsed_json.get("message", parsed_json.get("prompt", ""))).strip()
#                 if user_text and user_text not in ["HELLO_SERVER", "__SYSTEM_CONNECTION_INITIALIZED__"]:
#                     asyncio.create_task(self.process_text_inference(user_text))

#             except json.JSONDecodeError:
#                 clean_text = text_data.strip()
#                 if clean_text and not clean_text.startswith("{"):
#                     asyncio.create_task(self.process_text_inference(clean_text))
#             except Exception as e:
#                 print(f"⚠️ [RECEIVE ERROR]: {e}")

#         # ------------------------------------------------------------------
#         # 2. HANDLE BINARY AUDIO CHUNKS (STREAMING PCM)
#         # ------------------------------------------------------------------
#         elif bytes_data:
#             if not self.is_connected:
#                 return
                
#             self.audio_buffer.extend(bytes_data)
#             audio_frame = np.frombuffer(bytes_data, dtype=np.int16)
            
#             if len(audio_frame) == 0:
#                 return

#             rms_energy = np.sqrt(np.mean(audio_frame.astype(np.float32)**2))
#             current_time = time.time()

#             # Voice Activity Detection (VAD) Logic
#             if rms_energy > self.ENERGY_THRESHOLD:
#                 self.is_user_talking = True
#                 self.silence_start_time = None
#             else:
#                 if self.is_user_talking and self.silence_start_time is None:
#                     self.silence_start_time = current_time

#             # Calculate silence duration
#             silence_duration = (current_time - self.silence_start_time) if self.silence_start_time else 0.0

#             # Dispatch buffer if silence interval met OR hard size cap reached
#             should_dispatch = (
#                 (self.is_user_talking and silence_duration >= self.SILENCE_DURATION_SEC) or
#                 (len(self.audio_buffer) >= self.MAX_BUFFER_BYTES)
#             )

#             if should_dispatch and len(self.audio_buffer) > 8000:
#                 print(f"🎙️ [SPEECH CHUNK READY] Processing {len(self.audio_buffer)} bytes...")
#                 raw_buffer = bytes(self.audio_buffer)
#                 self.audio_buffer.clear()
#                 self.is_user_talking = False
#                 self.silence_start_time = None
                
#                 asyncio.create_task(self.process_audio_transcription(raw_buffer))

#     async def trigger_initial_greeting(self):
#         """Fetches active opening greeting and speaks it when line opens."""
#         await asyncio.sleep(0.4)
#         script_data = await self.get_active_script()
        
#         greeting_text = script_data['greeting'] if script_data else "Hello! How can I help you today?"
        
#         if self.is_connected:
#             print(f"🗣️ [INITIAL GREETING]: {greeting_text}")
#             self.call_transcript_log.append(f"AI Agent: {greeting_text}")
            
#             await self.safe_send({
#                 "type": "ai_response",
#                 "sender": "AI",
#                 "text": greeting_text
#             })
            
#             pcm_bytes = await generate_voice_pcm_bytes(greeting_text)
#             if pcm_bytes and self.is_connected:
#                 await self.send(bytes_data=pcm_bytes)

        
#     async def process_audio_transcription(self, raw_audio_bytes):
#         """Transcribes incoming 16-bit PCM 16kHz mono audio."""

#         try:

#             # ==========================================================
#             # PCM → INT16
#             # ==========================================================

#             pcm_int16 = np.frombuffer(
#                 raw_audio_bytes,
#                 dtype=np.int16
#             )

#             if len(pcm_int16) == 0:
#                 print("⚠️ [WHISPER] Empty PCM buffer")
#                 return

#             # ==========================================================
#             # PCM DEBUG
#             # ==========================================================

#             max_amp = int(
#                 np.max(
#                     np.abs(pcm_int16)
#                 )
#             )

#             rms = float(
#                 np.sqrt(
#                     np.mean(
#                         pcm_int16.astype(np.float32) ** 2
#                     )
#                 )
#             )

#             duration = len(pcm_int16) / 16000.0

#             print(
#                 f"🔊 [WHISPER PCM] "
#                 f"bytes={len(raw_audio_bytes)} "
#                 f"samples={len(pcm_int16)} "
#                 f"duration={duration:.2f}s "
#                 f"max_amp={max_amp} "
#                 f"rms={rms:.2f}"
#             )

#             # ==========================================================
#             # INT16 → FLOAT32
#             # ==========================================================

#             final_audio = (
#                 pcm_int16.astype(np.float32) / 32768.0
#             )

#             # ==========================================================
#             # WHISPER
#             # ==========================================================

#             segments, info = await asyncio.to_thread(
#                 whisper_model.transcribe,
#                 final_audio,
#                 beam_size=1,
#                 language="en",
#                 no_speech_threshold=0.4,
#                 vad_filter=True,
#                 vad_parameters={
#                     "min_silence_duration_ms": 500
#                 }
#             )

#             user_text = "".join(
#                 segment.text
#                 for segment in segments
#             ).strip()

#             # ==========================================================
#             # RESULT
#             # ==========================================================

#             clean_check = re.sub(
#                 r"[^\w\s]",
#                 "",
#                 user_text.lower()
#             ).strip()

#             hallucinations = [
#                 "you",
#                 "you you",
#                 "thank you",
#                 "subtitles",
#                 "bye",
#                 "amaraorg",
#                 "mb",
#                 "thank you for watching"
#             ]

#             if (
#                 user_text
#                 and len(clean_check) >= 2
#                 and clean_check not in hallucinations
#             ):

#                 print(
#                     f"🗣️ [WHISPER TRANSCRIPT]: {user_text}"
#                 )

#                 if (
#                     hasattr(self, "is_connected")
#                     and self.is_connected
#                 ):

#                     asyncio.create_task(
#                         self.process_text_inference(
#                             user_text
#                         )
#                     )

#             else:

#                 print(
#                     f"⚠️ [WHISPER IGNORED]: "
#                     f"Ignored empty or hallucinated audio: "
#                     f"'{user_text}'"
#                 )

#         except Exception as e:

#             print(
#                 f"❌ [TRANSCRIPTION FAIL]: {e}"
#             )
#     async def process_text_inference(self, user_text):
#         print(f"🤖 [LLAMA TRIGGERED] Generating response for: '{user_text}'")
#         self.call_transcript_log.append(f"Customer: {user_text}")
        
#         await self.safe_send({
#             "type": "user_transcript",
#             "sender": "Customer",
#             "text": user_text
#         })

#         script_data = await self.get_active_script()
        
#         if script_data:
#             system_prompt = (
#                 f"You are {script_data['bot_name']}, an AI Sales Representative for {script_data['company']}.\n"
#                 f"KNOWLEDGE BASE:\n{script_data['details']}\n\n"
#                 f"RULES:\n"
#                 f"1. GREETING: '{script_data['greeting']}'\n"
#                 f"2. CLOSING: '{script_data['closing']}'\n"
#                 f"3. Keep answers under 2 concise, natural conversational sentences."
#             )
#         else:
#             system_prompt = "You are a helpful AI sales assistant. Keep responses under 2 sentences."

#         prompt_context = f"{system_prompt}\n\nLead Context: {self.lead_details}\nCustomer: {user_text}\nAI:"

#         def _run_ollama():
#             try:
#                 # Use stream=False inside thread for immediate response generation
#                 res = ollama.generate(model="llama3.2", prompt=prompt_context, stream=False)
#                 return res.get("response", "").strip()
#             except Exception as e:
#                 print(f"❌ [OLLAMA MODEL ERROR]: {e}")
#                 # Fallback to llama3 if llama3.2 is not pulled
#                 try:
#                     res = ollama.generate(model="llama3", prompt=prompt_context, stream=False)
#                     return res.get("response", "").strip()
#                 except Exception as fallback_err:
#                     print(f"❌ [OLLAMA FALLBACK ERROR]: {fallback_err}")
#                     return "I understand. How else can I assist you?"

#         try:
#             full_ai_response = await asyncio.to_thread(_run_ollama)
            
#             if full_ai_response and self.is_connected:
#                 print(f"🗣️ [AI RESPONSE]: {full_ai_response}")
#                 self.call_transcript_log.append(f"AI Agent: {full_ai_response}")
                
#                 # Send text response to Flutter Call UI
#                 await self.safe_send({
#                     "type": "ai_response",
#                     "sender": "AI",
#                     "text": full_ai_response
#                 })

#                 # Convert AI response to PCM bytes and stream over cellular line
#                 pcm_bytes = await generate_voice_pcm_bytes(full_ai_response)
#                 if pcm_bytes and self.is_connected:
#                     await self.send(bytes_data=pcm_bytes)

#         except Exception as e:
#             print(f"⚠️ [INFERENCE EXCEPTION]: {e}")

#     async def safe_send(self, payload: dict):
#         if hasattr(self, 'is_connected') and self.is_connected:
#             try:
#                 await self.send(text_data=json.dumps(payload))
#             except Exception as e:
#                 print(f"⚠️ [SEND SKIPPED]: Socket closed ({e})")
#                 self.is_connected = False

#     @database_sync_to_async
#     def create_call_session(self, phone_number):
#         try:
#             if not phone_number:
#                 return None
#             contact, _ = Contact.objects.get_or_create(phone_number=phone_number)
#             session = CallSession.objects.create(contact=contact, status="active")
#             return session.id
#         except Exception as e:
#             print(f"⚠️ [DB CREATE SESSION ERROR]: {e}")
#             return None

#     @database_sync_to_async
#     def finalize_call_session(self, session_id, duration, transcript_log):
#         try:
#             if not session_id:
#                 return
#             session = CallSession.objects.get(id=session_id)
#             session.duration_seconds = int(duration)
#             session.status = "completed"
#             session.save()
#             print(f"✅ [DB SESSION SAVED] ID: {session_id} | Duration: {int(duration)}s")
#         except Exception as e:
#             print(f"⚠️ [DB FINALIZE ERROR]: {e}")

#     @database_sync_to_async
#     def get_active_script(self):
#         """Fetches active script parameters from Django DB."""
#         try:
#             script = CompanyScript.objects.filter(is_active=True).first()
#             if script:
#                 return {
#                     "bot_name": script.bot_name,
#                     "company": script.company_name,
#                     "details": script.company_details,
#                     "greeting": script.opening_greeting,
#                     "closing": script.closing_statement
#                 }
#         except Exception as e:
#             print(f"⚠️ [DB SCRIPT FETCH ERROR]: {e}")
#         return None

import subprocess
import os
import sys
import torch
import torchaudio
import soundfile as sf
import time
import io
import gc
import json
import re
import asyncio
import numpy as np
import edge_tts
import requests
from pydub import AudioSegment
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import pydub

# Configure pydub binaries for Windows environment
pydub.AudioSegment.converter = r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
pydub.AudioSegment.ffprobe = r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"

# Enable PyTorch CPU Multi-Threading for Fast Inference
num_cores = os.cpu_count() or 4
torch.set_num_threads(num_cores)
torch.set_num_interop_threads(num_cores)

# Patching torchaudio load for systems with missing FFmpeg DLLs
def patched_torchaudio_load(filepath, *args, **kwargs):
    data, samplerate = sf.read(filepath, dtype='float32')
    tensor = torch.from_numpy(data).t()
    if tensor.ndim == 1:
        tensor = tensor.unsqueeze(0)
    return tensor, samplerate

torchaudio.load = patched_torchaudio_load
os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"

from calls.models import CallSession, CompanyScript, SalesInsight, Contact
from faster_whisper import WhisperModel
import ollama

print("🧠 Loading Whisper Speech Engine inside Django...")
# Quantized tiny model with VAD enabled
whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
print("✅ Whisper Bound to Django App!")

print("🎙️ Initializing Ultra-Fast Edge Neural Voice Engine...")

def initialize_llama_engine():
    print("🦙 Checking Llama Engine Status...")
    try:
        # Check if Ollama service is active
        res = requests.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
        if res.status_code == 200:
            print("✅ Llama Engine Online & Bound to Pipeline!")
            return
    except Exception:
        print("🚀 Llama Engine not detected. Auto-launching Ollama background process...")

    try:
        # Launch Ollama in the background automatically
        subprocess.Popen(
            ["ollama", "run", "llama3.2"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        time.sleep(2)
        print("✅ Llama Engine automatically started & bound!")
    except FileNotFoundError:
        print("❌ Could not auto-start Llama! Ensure Ollama is installed and added to PATH.")
    except Exception as e:
        print(f"❌ Error auto-starting Llama: {e}")

# Run Llama initialization on module load
initialize_llama_engine()


async def generate_voice_pcm_bytes(text: str) -> bytes:
    """Generates natural human neural voice and converts MP3 to raw 16kHz PCM 16-bit mono for phone baseband."""
    cleaned_text = re.sub(r"[^\w\s.,?!']", "", text).strip()
    
    if not cleaned_text or len(cleaned_text) < 3:
        return b""
        
    if len(cleaned_text) > 250:
        cleaned_text = cleaned_text[:250]
        
    if cleaned_text[-1] not in [".", "!", "?", ","]:
        cleaned_text += "."

    try:
        # 1. Fetch MP3 audio stream from Edge-TTS
        communicate = edge_tts.Communicate(cleaned_text, "en-US-GuyNeural")
        mp3_io = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_io.write(chunk["data"])
        
        mp3_io.seek(0)
        
        # 2. Convert MP3 to 16kHz PCM 16-bit Mono (Required for SIM Call Uplink)
        def _convert_mp3_to_pcm(raw_mp3_bytes):
            audio = AudioSegment.from_file(io.BytesIO(raw_mp3_bytes), format="mp3")
            audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            return audio.raw_data

        return await asyncio.to_thread(_convert_mp3_to_pcm, mp3_io.getvalue())

    except Exception as e:
        print(f"⚠️ Voice generation exception: {e}")
        return b""


def split_into_sentences(text_buffer):
    sentences = re.split(r'(?<=[.!?])\s+', text_buffer)
    if len(sentences) > 1:
        return sentences[:-1], sentences[-1]
    return [], text_buffer


class MediaStreamConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        try:
            await self.accept()
            self.is_connected = True
            self.greeting_sent = False  # Guard flag to prevent duplicate greetings
            self.client_phone = None
            self.session_id = None
            self.lead_details = ""
            self.call_transcript_log = []
            self.start_time = time.time()
            
            # 🔑 REAL-TIME VAD & BUFFER PARAMETERS (16kHz, 16-bit Mono = 32,000 bytes/sec)
            self.audio_buffer = bytearray()
            self.silence_start_time = None
            self.is_user_talking = False
            self.ENERGY_THRESHOLD = 300       # Sensitivity cutoff for voice presence
            self.SILENCE_DURATION_SEC = 0.8  # Dispatch STT after 800ms of post-speech silence
            self.MAX_BUFFER_BYTES = 128000    # Hard cap (~4 seconds) to prevent huge Whisper latencies
            
            self.INACTIVITY_TIMEOUT_SECONDS = 120.0
            self.last_activity_time = time.time()
            
            self.timeout_checker_task = asyncio.create_task(self.monitor_inactivity_timeout())
            print("🌐 [WS CONNECT] Single synchronized pipeline channel established.")
        except Exception as e:
            print(f"❌ [WS CONNECT ERROR]: {e}")
            await self.close()

    async def monitor_inactivity_timeout(self):
        try:
            while self.is_connected:
                await asyncio.sleep(5)
                current_time = time.time()
                if (current_time - self.last_activity_time) > self.INACTIVITY_TIMEOUT_SECONDS:
                    print("⏰ [TIMEOUT] Inactivity threshold hit. Terminating call session.")
                    await self.safe_send({"event": "ai_token", "text": "[SESSION TIMEOUT]"})
                    await self.close()
                    break
        except asyncio.CancelledError:
            pass

    async def disconnect(self, close_code):
        self.is_connected = False
        self.greeting_sent = False
        if hasattr(self, 'timeout_checker_task'):
            self.timeout_checker_task.cancel()
            
        duration = time.time() - self.start_time
        print(f"🔌 [WS DISCONNECT] Connection severed safely. Code: {close_code}")
        
        if self.session_id:
            asyncio.create_task(self.finalize_call_session(self.session_id, duration, self.call_transcript_log))
        
        self.audio_buffer.clear()

    async def receive(self, text_data=None, bytes_data=None):
        self.last_activity_time = time.time()

        # ------------------------------------------------------------------
        # 1. HANDLE JSON / TEXT TELEMETRY & CONTROL MESSAGES
        # ------------------------------------------------------------------
        if text_data:
            try:
                parsed_json = json.loads(text_data)
                
                # Metadata initialization
                if "client_phone_number" in parsed_json or parsed_json.get("event") == "client_phone_number":
                    self.client_phone = parsed_json.get("client_phone_number")
                    self.lead_details = parsed_json.get("lead_details", parsed_json.get("details", ""))
                    self.session_id = await self.create_call_session(self.client_phone)
                    print(f"📱 [METADATA BOUND] Session active. Phone ID: {self.client_phone} | Details: {self.lead_details}")
                    
                    # Immediate greeting trigger upon metadata binding if line is active
                    if not self.greeting_sent:
                        self.greeting_sent = True
                        asyncio.create_task(self.trigger_initial_greeting())
                    return

                # Explicit call answered signal
                if parsed_json.get("event") == "call_answered":
                    if not self.greeting_sent:
                        self.greeting_sent = True
                        print("✅ [CALL ANSWERED] Line active! Triggering initial greeting...")
                        asyncio.create_task(self.trigger_initial_greeting())
                    return

                # Telemetry
                if parsed_json.get("event") == "call_state_changed":
                    state = parsed_json.get("state")
                    print(f"📞 [TELEMETRY REGISTRY] Hardware Line Changed: {state}")
                    return

                # Text Injection
                user_text = parsed_json.get("text", parsed_json.get("message", parsed_json.get("prompt", ""))).strip()
                if user_text and user_text not in ["HELLO_SERVER", "__SYSTEM_CONNECTION_INITIALIZED__"]:
                    asyncio.create_task(self.process_text_inference(user_text))

            except json.JSONDecodeError:
                clean_text = text_data.strip()
                if clean_text and not clean_text.startswith("{"):
                    asyncio.create_task(self.process_text_inference(clean_text))
            except Exception as e:
                print(f"⚠️ [RECEIVE ERROR]: {e}")

        # ------------------------------------------------------------------
        # 2. HANDLE BINARY AUDIO CHUNKS (STREAMING PCM)
        # ------------------------------------------------------------------
        elif bytes_data:
            if not self.is_connected:
                return
                
            self.audio_buffer.extend(bytes_data)
            audio_frame = np.frombuffer(bytes_data, dtype=np.int16)
            
            if len(audio_frame) == 0:
                return

            rms_energy = np.sqrt(np.mean(audio_frame.astype(np.float32)**2))
            current_time = time.time()

            # Voice Activity Detection (VAD) Logic
            if rms_energy > self.ENERGY_THRESHOLD:
                self.is_user_talking = True
                self.silence_start_time = None
            else:
                if self.is_user_talking and self.silence_start_time is None:
                    self.silence_start_time = current_time

            # Calculate silence duration
            silence_duration = (current_time - self.silence_start_time) if self.silence_start_time else 0.0

            # Dispatch buffer if silence interval met OR hard size cap reached
            should_dispatch = (
                (self.is_user_talking and silence_duration >= self.SILENCE_DURATION_SEC) or
                (len(self.audio_buffer) >= self.MAX_BUFFER_BYTES)
            )

            if should_dispatch and len(self.audio_buffer) > 8000:
                print(f"🎙️ [SPEECH CHUNK READY] Processing {len(self.audio_buffer)} bytes...")
                raw_buffer = bytes(self.audio_buffer)
                self.audio_buffer.clear()
                self.is_user_talking = False
                self.silence_start_time = None
                
                asyncio.create_task(self.process_audio_transcription(raw_buffer))

    async def trigger_initial_greeting(self):
        """Fetches active opening greeting and speaks it when line opens."""
        await asyncio.sleep(0.4)
        script_data = await self.get_active_script()
        
        greeting_text = script_data['greeting'] if script_data else "Hello! How can I help you today?"
        
        if self.is_connected:
            print(f"🗣️ [INITIAL GREETING]: {greeting_text}")
            self.call_transcript_log.append(f"AI Agent: {greeting_text}")
            
            await self.safe_send({
                "type": "ai_response",
                "sender": "AI",
                "text": greeting_text
            })
            
            pcm_bytes = await generate_voice_pcm_bytes(greeting_text)
            if pcm_bytes and self.is_connected:
                await self.send(bytes_data=pcm_bytes)

        
    async def process_audio_transcription(self, raw_audio_bytes):
        """Transcribes incoming 16-bit PCM 16kHz mono audio."""

        try:
            pcm_int16 = np.frombuffer(raw_audio_bytes, dtype=np.int16)

            if len(pcm_int16) == 0:
                print("⚠️ [WHISPER] Empty PCM buffer")
                return

            max_amp = int(np.max(np.abs(pcm_int16)))
            rms = float(np.sqrt(np.mean(pcm_int16.astype(np.float32) ** 2)))
            duration = len(pcm_int16) / 16000.0

            print(
                f"🔊 [WHISPER PCM] "
                f"bytes={len(raw_audio_bytes)} "
                f"samples={len(pcm_int16)} "
                f"duration={duration:.2f}s "
                f"max_amp={max_amp} "
                f"rms={rms:.2f}"
            )

            final_audio = pcm_int16.astype(np.float32) / 32768.0

            segments, info = await asyncio.to_thread(
                whisper_model.transcribe,
                final_audio,
                beam_size=1,
                language="en",
                no_speech_threshold=0.4,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500}
            )

            user_text = "".join(segment.text for segment in segments).strip()
            clean_check = re.sub(r"[^\w\s]", "", user_text.lower()).strip()

            hallucinations = [
                "you",
                "you you",
                "thank you",
                "subtitles",
                "bye",
                "amaraorg",
                "mb",
                "thank you for watching"
            ]

            if user_text and len(clean_check) >= 2 and clean_check not in hallucinations:
                print(f"🗣️ [WHISPER TRANSCRIPT]: {user_text}")

                if hasattr(self, "is_connected") and self.is_connected:
                    asyncio.create_task(self.process_text_inference(user_text))
            else:
                print(f"⚠️ [WHISPER IGNORED]: Ignored empty or hallucinated audio: '{user_text}'")

        except Exception as e:
            print(f"❌ [TRANSCRIPTION FAIL]: {e}")

    async def process_text_inference(self, user_text):
        print(f"🤖 [LLAMA TRIGGERED] Generating response for: '{user_text}'")
        self.call_transcript_log.append(f"Customer: {user_text}")
        
        await self.safe_send({
            "type": "user_transcript",
            "sender": "Customer",
            "text": user_text
        })

        script_data = await self.get_active_script()
        
        if script_data:
            system_prompt = (
                f"You are {script_data['bot_name']}, an AI Sales Representative for {script_data['company']}.\n"
                f"KNOWLEDGE BASE:\n{script_data['details']}\n\n"
                f"RULES:\n"
                f"1. GREETING: '{script_data['greeting']}'\n"
                f"2. CLOSING: '{script_data['closing']}'\n"
                f"3. Keep answers under 2 concise, natural conversational sentences."
            )
        else:
            system_prompt = "You are a helpful AI sales assistant. Keep responses under 2 sentences."

        prompt_context = f"{system_prompt}\n\nLead Context: {self.lead_details}\nCustomer: {user_text}\nAI:"

        def _run_ollama():
            try:
                res = ollama.generate(model="llama3.2", prompt=prompt_context, stream=False)
                return res.get("response", "").strip()
            except Exception as e:
                print(f"❌ [OLLAMA MODEL ERROR]: {e}")
                try:
                    res = ollama.generate(model="llama3", prompt=prompt_context, stream=False)
                    return res.get("response", "").strip()
                except Exception as fallback_err:
                    print(f"❌ [OLLAMA FALLBACK ERROR]: {fallback_err}")
                    return "I understand. How else can I assist you?"

        try:
            full_ai_response = await asyncio.to_thread(_run_ollama)
            
            if full_ai_response and self.is_connected:
                print(f"🗣️ [AI RESPONSE]: {full_ai_response}")
                self.call_transcript_log.append(f"AI Agent: {full_ai_response}")
                
                # Send text response to Flutter Call UI
                await self.safe_send({
                    "type": "ai_response",
                    "sender": "AI",
                    "text": full_ai_response
                })

                # Convert AI response to PCM bytes and stream over cellular line
                pcm_bytes = await generate_voice_pcm_bytes(full_ai_response)
                if pcm_bytes and self.is_connected:
                    await self.send(bytes_data=pcm_bytes)

        except Exception as e:
            print(f"⚠️ [INFERENCE EXCEPTION]: {e}")

    async def safe_send(self, payload: dict):
        if hasattr(self, 'is_connected') and self.is_connected:
            try:
                await self.send(text_data=json.dumps(payload))
            except Exception as e:
                print(f"⚠️ [SEND SKIPPED]: Socket closed ({e})")
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
            session.save()
            print(f"✅ [DB SESSION SAVED] ID: {session_id} | Duration: {int(duration)}s")
        except Exception as e:
            print(f"⚠️ [DB FINALIZE ERROR]: {e}")

    @database_sync_to_async
    def get_active_script(self):
        """Fetches active script parameters from Django DB."""
        try:
            script = CompanyScript.objects.filter(is_active=True).first()
            if script:
                return {
                    "bot_name": script.bot_name,
                    "company": script.company_name,
                    "details": script.company_details,
                    "greeting": script.opening_greeting,
                    "closing": script.closing_statement
                }
        except Exception as e:
            print(f"⚠️ [DB SCRIPT FETCH ERROR]: {e}")
        return None