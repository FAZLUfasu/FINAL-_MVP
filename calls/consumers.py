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

# # Patching torchaudio load for systems without missing FFmpeg DLLs
# def patched_torchaudio_load(filepath, *args, **kwargs):
#     data, samplerate = sf.read(filepath, dtype='float32')
#     tensor = torch.from_numpy(data).t()
#     if tensor.ndim == 1:
#         tensor = tensor.unsqueeze(0)
#     return tensor, samplerate

# torchaudio.load = patched_torchaudio_load
# os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"

# from channels.generic.websocket import AsyncWebsocketConsumer
# from channels.db import database_sync_to_async
# from calls.models import CallSession, SalesInsight, Contact

# from faster_whisper import WhisperModel
# import ollama
# from TTS.api import TTS

# print("🧠 Loading Whisper Speech Engine inside Django...")
# whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
# print("✅ Whisper Bound to Django App!")

# print("🎙️ Initializing Neural Voice Cloning Layers...")
# device = "cpu"
# cloning_engine = TTS(model_name="tts_models/multilingual/multi-dataset/xtts_v2").to(device)
# print(f"✅ Voice Engine fully bound to framework hardware: [{device.upper()}]")


# def text_to_cloned_voice_bytes(text: str) -> bytes:
#     output_wav = "temp_cloned_voice.wav"
#     reference_clip = "my_voice.wav"
    
#     if not os.path.exists(reference_clip):
#         print(f"⚠️ Missing reference sample '{reference_clip}'")
#         return b""
    
#     cleaned_text = text.strip()
#     if cleaned_text and cleaned_text[-1] not in [".", "!", "?", ","]:
#         cleaned_text += "."

#     if not cleaned_text or len(cleaned_text) < 4:
#         return b""
        
#     try:
#         cloning_engine.tts_to_file(
#             text=cleaned_text,
#             speaker_wav=reference_clip,
#             language="en",
#             file_path=output_wav
#         )
        
#         if os.path.exists(output_wav):
#             data, samplerate = sf.read(output_wav)
#             byte_io = io.BytesIO()
#             sf.write(byte_io, data, samplerate, format='WAV', subtype='PCM_16')
#             byte_io.seek(0)
#             return byte_io.read()
                
#     except Exception as e:
#         print(f"⚠️ Voice Cloning exception: {e}")
#         return b""
#     finally:
#         if os.path.exists(output_wav):
#             try: os.remove(output_wav)
#             except: pass
#         gc.collect()
#     return b""

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
#             self.client_phone = None
#             self.session_id = None
#             self.lead_details = ""
#             self.call_transcript_log = []
#             self.start_time = time.time()
            
#             self.audio_buffer = bytearray()
#             self.silence_counter = 0
#             self.is_user_talking = False
            
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
#         if hasattr(self, 'timeout_checker_task'):
#             self.timeout_checker_task.cancel()
            
#         duration = time.time() - self.start_time
#         print(f"🔌 [WS DISCONNECT] Connection severed safely. Code: {close_code}")
        
#         if self.session_id:
#             asyncio.create_task(self.finalize_call_session(self.session_id, duration, self.call_transcript_log))
        
#         self.audio_buffer.clear()

#     async def receive(self, text_data=None, bytes_data=None):
#         self.last_activity_time = time.time()

#         if text_data:
#             try:
#                 parsed_json = json.loads(text_data)
                
#                 # 🛡️ Handle Call Metadata
#                 if "client_phone_number" in parsed_json or parsed_json.get("event") == "client_phone_number":
#                     self.client_phone = parsed_json.get("client_phone_number")
#                     self.lead_details = parsed_json.get("lead_details", parsed_json.get("details", ""))
#                     self.session_id = await self.create_call_session(self.client_phone)
#                     print(f"📱 [METADATA BOUND] Session active. Phone ID: {self.client_phone} | Details: {self.lead_details}")
#                     return

#                 # 🛡️ Handle call state telemetry
#                 if parsed_json.get("event") == "call_state_changed":
#                     state = parsed_json.get("state")
#                     print(f"📞 [TELEMETRY REGISTRY] Hardware Line Changed: {state}")
#                     return

#                 # 🛡️ Handle prompt inference requests
#                 user_text = parsed_json.get("text", parsed_json.get("message", parsed_json.get("prompt", ""))).strip()
#                 if user_text and user_text not in ["HELLO_SERVER", "__SYSTEM_CONNECTION_INITIALIZED__"]:
#                     asyncio.create_task(self.process_text_inference(user_text))

#             except json.JSONDecodeError:
#                 clean_text = text_data.strip()
#                 if clean_text and not clean_text.startswith("{"):
#                     asyncio.create_task(self.process_text_inference(clean_text))
#             except Exception as e:
#                 print(f"⚠️ [RECEIVE ERROR]: {e}")

#         elif bytes_data:
#             if not self.client_phone:
#                 return
                
#             self.audio_buffer.extend(bytes_data)
#             audio_frame = np.frombuffer(bytes_data, dtype=np.int16)
#             rms_energy = np.sqrt(np.mean(audio_frame.astype(np.float32)**2)) if len(audio_frame) > 0 else 0

#             if rms_energy > 300:
#                 self.is_user_talking = True
#                 self.silence_counter = 0
#             else:
#                 if self.is_user_talking:
#                     self.silence_counter += 1

#             if self.is_user_talking and self.silence_counter > 25:
#                 print(f"🎙️ [SPEECH PAUSE] Processing audio buffer ({len(self.audio_buffer)} bytes)...")
#                 raw_buffer = bytes(self.audio_buffer)
#                 self.audio_buffer.clear()
#                 self.silence_counter = 0
#                 self.is_user_talking = False
                
#                 asyncio.create_task(self.process_audio_transcription(raw_buffer))

#     async def process_audio_transcription(self, raw_audio_bytes):
#         try:
#             final_audio = np.frombuffer(raw_audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
#             segments, _ = await asyncio.to_thread(whisper_model.transcribe, final_audio, beam_size=1, language="en")
#             user_text = "".join([segment.text for segment in segments]).strip()
            
#             if len(user_text) >= 4:
#                 print(f"🗣️ [WHISPER TRANSCRIPT]: {user_text}")
#                 await self.process_text_inference(user_text)
#         except Exception as e:
#             print(f"❌ [TRANSCRIPTION FAIL]: {e}")

#     async def process_text_inference(self, user_text):
#         self.call_transcript_log.append(f"Customer: {user_text}")
        
#         await self.safe_send({
#             "type": "user_transcript",
#             "sender": "Customer",
#             "text": user_text
#         })

#         text_buffer = ""
#         full_ai_response = ""
        
#         try:
#             prompt_context = f"Lead Context: {self.lead_details}\nCustomer said: {user_text}" if self.lead_details else user_text
            
#             response_stream = await asyncio.to_thread(
#                 ollama.generate, model="llama3.2", prompt=str(prompt_context), stream=True
#             )
            
#             for chunk in response_stream:
#                 if not self.is_connected:
#                     break
                    
#                 token = chunk.get("response", "")
#                 text_buffer += token
#                 full_ai_response += token
                
#                 await self.safe_send({
#                     "event": "ai_token",
#                     "type": "ai_token",
#                     "text": token
#                 })
                
#                 complete_sentences, text_buffer = split_into_sentences(text_buffer)
#                 for sentence in complete_sentences:
#                     sentence = sentence.strip()
#                     if len(sentence) >= 4:
#                         audio_bytes = await asyncio.to_thread(text_to_cloned_voice_bytes, sentence)
#                         if audio_bytes and self.is_connected:
#                             await self.send(bytes_data=audio_bytes)
                            
#             if text_buffer.strip() and len(text_buffer.strip()) >= 4 and self.is_connected:
#                 sentence = text_buffer.strip()
#                 audio_bytes = await asyncio.to_thread(text_to_cloned_voice_bytes, sentence)
#                 if audio_bytes:
#                     await self.send(bytes_data=audio_bytes)

#             self.call_transcript_log.append(f"AI Agent: {full_ai_response.strip()}")
            
#             await self.safe_send({
#                 "type": "ai_response",
#                 "sender": "AI",
#                 "text": full_ai_response.strip()
#             })

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
#             session = CallSession.objects.create(contact=contact, status="IN_PROGRESS")
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
#             session.duration = int(duration)
#             session.status = "COMPLETED"
#             session.transcript = "\n".join(transcript_log)
#             session.save()
#             print(f"✅ [DB SESSION SAVED] ID: {session_id} | Duration: {int(duration)}s")
#         except Exception as e:
#             print(f"⚠️ [DB FINALIZE ERROR]: {e}")
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

# Patching torchaudio load for systems with missing FFmpeg DLLs
def patched_torchaudio_load(filepath, *args, **kwargs):
    data, samplerate = sf.read(filepath, dtype='float32')
    tensor = torch.from_numpy(data).t()
    if tensor.ndim == 1:
        tensor = tensor.unsqueeze(0)
    return tensor, samplerate

torchaudio.load = patched_torchaudio_load
os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from calls.models import CallSession, SalesInsight, Contact

from faster_whisper import WhisperModel
import ollama
from TTS.api import TTS

print("🧠 Loading Whisper Speech Engine inside Django...")
# Quantized tiny model with VAD enabled
whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
print("✅ Whisper Bound to Django App!")

print("🎙️ Initializing Neural Voice Cloning Layers...")
device = "cpu"
cloning_engine = TTS(model_name="tts_models/multilingual/multi-dataset/xtts_v2").to(device)
print(f"✅ Voice Engine fully bound to framework hardware: [{device.upper()}]")


def text_to_cloned_voice_bytes(text: str) -> bytes:
    reference_clip = "my_voice.wav"
    
    if not os.path.exists(reference_clip):
        print(f"⚠️ Missing reference sample '{reference_clip}'")
        return b""
    
    # 1. Sanitize text (Remove emojis and special characters)
    cleaned_text = re.sub(r"[^\w\s.,?!']", "", text).strip()
    
    # Guard against empty or ultra-short text
    if not cleaned_text or len(cleaned_text) < 3:
        return b""
        
    if len(cleaned_text) > 250:
        cleaned_text = cleaned_text[:250]
        
    if cleaned_text[-1] not in [".", "!", "?", ","]:
        cleaned_text += "."

    try:
        # 2. In-Memory Direct Speech Generation (No disk write/read)
        wav_array = cloning_engine.tts(
            text=cleaned_text,
            speaker_wav=reference_clip,
            language="en"
        )
        
        # 3. Convert NumPy float array directly to 16kHz PCM WAV bytes
        byte_io = io.BytesIO()
        sf.write(byte_io, np.array(wav_array, dtype=np.float32), 24000, format='WAV', subtype='PCM_16')
        byte_io.seek(0)
        return byte_io.read()

    except Exception as e:
        print(f"⚠️ Voice Cloning exception: {e}")
        return b""
    finally:
        # 🧹 Prevent RAM memory allocation error (36.5 MiB issue)
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


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
            self.client_phone = None
            self.session_id = None
            self.lead_details = ""
            self.call_transcript_log = []
            self.start_time = time.time()
            
            self.audio_buffer = bytearray()
            self.silence_counter = 0
            self.is_user_talking = False
            
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
        if hasattr(self, 'timeout_checker_task'):
            self.timeout_checker_task.cancel()
            
        duration = time.time() - self.start_time
        print(f"🔌 [WS DISCONNECT] Connection severed safely. Code: {close_code}")
        
        if self.session_id:
            asyncio.create_task(self.finalize_call_session(self.session_id, duration, self.call_transcript_log))
        
        self.audio_buffer.clear()

    async def receive(self, text_data=None, bytes_data=None):
        self.last_activity_time = time.time()

        if text_data:
            try:
                parsed_json = json.loads(text_data)
                
                # 🛡️ Handle Call Metadata
                if "client_phone_number" in parsed_json or parsed_json.get("event") == "client_phone_number":
                    self.client_phone = parsed_json.get("client_phone_number")
                    self.lead_details = parsed_json.get("lead_details", parsed_json.get("details", ""))
                    self.session_id = await self.create_call_session(self.client_phone)
                    print(f"📱 [METADATA BOUND] Session active. Phone ID: {self.client_phone} | Details: {self.lead_details}")
                    return

                # 🛡️ Handle Call State Telemetry
                if parsed_json.get("event") == "call_state_changed":
                    state = parsed_json.get("state")
                    print(f"📞 [TELEMETRY REGISTRY] Hardware Line Changed: {state}")
                    return

                # 🛡️ Handle Prompt Requests
                user_text = parsed_json.get("text", parsed_json.get("message", parsed_json.get("prompt", ""))).strip()
                if user_text and user_text not in ["HELLO_SERVER", "__SYSTEM_CONNECTION_INITIALIZED__"]:
                    asyncio.create_task(self.process_text_inference(user_text))

            except json.JSONDecodeError:
                clean_text = text_data.strip()
                if clean_text and not clean_text.startswith("{"):
                    asyncio.create_task(self.process_text_inference(clean_text))
            except Exception as e:
                print(f"⚠️ [RECEIVE ERROR]: {e}")

        elif bytes_data:
            if not self.client_phone:
                return
                
            self.audio_buffer.extend(bytes_data)
            audio_frame = np.frombuffer(bytes_data, dtype=np.int16)
            rms_energy = np.sqrt(np.mean(audio_frame.astype(np.float32)**2)) if len(audio_frame) > 0 else 0

            # VAD Energy Threshold Check
            if rms_energy > 350:
                self.is_user_talking = True
                self.silence_counter = 0
            else:
                if self.is_user_talking:
                    self.silence_counter += 1

            if self.is_user_talking and self.silence_counter > 20:
                print(f"🎙️ [SPEECH PAUSE] Processing audio buffer ({len(self.audio_buffer)} bytes)...")
                raw_buffer = bytes(self.audio_buffer)
                self.audio_buffer.clear()
                self.silence_counter = 0
                self.is_user_talking = False
                
                asyncio.create_task(self.process_audio_transcription(raw_buffer))

    async def process_audio_transcription(self, raw_audio_bytes):
        try:
            # Convert raw 16kHz PCM bytes to float32 normalized array
            final_audio = np.frombuffer(raw_audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            
            # 🔑 VAD Filter Enabled to Prevent Static Hallucinations
            segments, _ = await asyncio.to_thread(
                whisper_model.transcribe, 
                final_audio, 
                beam_size=1, 
                language="en",
                no_speech_threshold=0.6,
                vad_filter=True
            )
            
            user_text = "".join([segment.text for segment in segments]).strip()
            
            # Filter out short static noise or hallucinations
            if len(user_text) >= 4 and user_text.lower() not in ["you", "you you", "thank you.", "subtitles"]:
                print(f"🗣️ [WHISPER TRANSCRIPT]: {user_text}")
                await self.process_text_inference(user_text)
        except Exception as e:
            print(f"❌ [TRANSCRIPTION FAIL]: {e}")

    async def process_text_inference(self, user_text):
        self.call_transcript_log.append(f"Customer: {user_text}")
        
        await self.safe_send({
            "type": "user_transcript",
            "sender": "Customer",
            "text": user_text
        })

        text_buffer = ""
        full_ai_response = ""
        
        try:
            # 🎯 System Prompt forces Llama 3.2 to give brief, human-like voice responses
            system_prompt = (
                "You are an AI sales assistant on a live phone call. "
                "Keep responses under 2 sentences, concise, professional, and conversational. "
                "Do not use markdown, emojis, or bullet points."
            )
            
            prompt_context = f"{system_prompt}\nLead Context: {self.lead_details}\nCustomer: {user_text}\nAI:"
            
            response_stream = await asyncio.to_thread(
                ollama.generate, model="llama3.2", prompt=prompt_context, stream=True
            )
            
            for chunk in response_stream:
                if not self.is_connected:
                    break
                    
                token = chunk.get("response", "")
                text_buffer += token
                full_ai_response += token
                
                await self.safe_send({
                    "event": "ai_token",
                    "type": "ai_token",
                    "text": token
                })
                
                complete_sentences, text_buffer = split_into_sentences(text_buffer)
                for sentence in complete_sentences:
                    sentence = sentence.strip()
                    if len(sentence) >= 4:
                        audio_bytes = await asyncio.to_thread(text_to_cloned_voice_bytes, sentence)
                        if audio_bytes and self.is_connected:
                            await self.send(bytes_data=audio_bytes)
                            
            if text_buffer.strip() and len(text_buffer.strip()) >= 4 and self.is_connected:
                sentence = text_buffer.strip()
                audio_bytes = await asyncio.to_thread(text_to_cloned_voice_bytes, sentence)
                if audio_bytes:
                    await self.send(bytes_data=audio_bytes)

            self.call_transcript_log.append(f"AI Agent: {full_ai_response.strip()}")
            
            await self.safe_send({
                "type": "ai_response",
                "sender": "AI",
                "text": full_ai_response.strip()
            })

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
            session = CallSession.objects.create(contact=contact, status="IN_PROGRESS")
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
            session.duration = int(duration)
            session.status = "COMPLETED"
            session.transcript = "\n".join(transcript_log)
            session.save()
            print(f"✅ [DB SESSION SAVED] ID: {session_id} | Duration: {int(duration)}s")
        except Exception as e:
            print(f"⚠️ [DB FINALIZE ERROR]: {e}")