import gc
import torch

def generate_speech_xtts(tts_model, text, speaker_wav_path):
    try:
        # Generate speech
        wav = tts_model.tts(text=text, speaker_wav=speaker_wav_path, language="en")
        return wav
    except Exception as e:
        print(f"❌ XTTS Memory/Index Error: {e}")
        return None
    finally:
        # 🧹 Clear memory buffer explicitly after every generation
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            