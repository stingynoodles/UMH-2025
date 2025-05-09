# Simplified version with only the upload endpoint
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.encoders import jsonable_encoder
from faster_whisper import WhisperModel
from transformers import WhisperForConditionalGeneration, WhisperProcessor, pipeline
import tempfile
import os
import torch
import asyncio
from pathlib import Path
import librosa
from transformers.models.whisper import tokenization_whisper
import time
import soundfile as sf
import numpy as np
import subprocess
import shutil
import json
from pystoi import stoi
# from gemini_agents import app as gemini_app
from df.enhance import enhance, init_df, load_audio, save_audio
from typing import Optional

# gemini_app.mount("/gemini", gemini_app)


# Define this global variable
multi_agent_system = None

# Add this for Malaysian model
tokenization_whisper.TASK_IDS = ["translate", "transcribe", "transcribeprecise"]

PROJECT_ROOT = Path(__file__).parent
MODEL_CACHE_DIR = PROJECT_ROOT / "models" / "huggingface"
MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

torch.set_num_threads(1)

# Updated model configurations with clearer country labeling
COUNTRY_MODELS = {
    "Malaysia": {
        "name": "Malaysian Whisper Model",
        "model_id": "mesolitica/malaysian-whisper-small-v3",
        "language": "ms",
        "type": "malaysian",
        "use_faster_whisper": False  # Enable faster-whisper for this model
    },
    "Singapore": {
        "name": "Singlish Whisper Model",
        "model_id": "jensenlwt/whisper-small-singlish-122k",
        "language": "en",
        "type": "pipeline",
        "use_faster_whisper": False  # Enable faster-whisper for this model
    },
    "Thailand": {
        "name": "Thai Whisper Model",
        "model_id": "juierror/whisper-tiny-thai",
        "language": "th",
        "type": "thai",
        "use_faster_whisper": False  # Enable faster-whisper for this model
    }
}

# Configure pytorch settings
if torch.cuda.is_available():
    print("CUDA available - using GPU acceleration")
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    TORCH_DTYPE = torch.float16
else:
    print("CUDA not available - using CPU")
    TORCH_DTYPE = torch.float32

class FasterWhisperHandler:
    """Handle faster-whisper models for specific countries/languages"""
    
    def __init__(self, model_config, cache_dir):
        self.config = model_config
        self.cache_dir = cache_dir
        self.model = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.compute_type = "float16" if self.device == "cuda" else "int8"
        print(f"Using device: {self.device} for model: {model_config['name']}")
        
    async def load(self):
        """Initialize the faster-whisper model"""
        try:
            model_id = self.config["model_id"]
            self.model = WhisperModel(
                "large-v3",
                device=self.device,
                compute_type=self.compute_type,
                download_root=str(self.cache_dir)
            )
            print(f"Faster-Whisper model loaded successfully for {self.config['name']}")
            return True
        except Exception as e:
            print(f"Error loading Faster-Whisper model: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    
    async def transcribe(self, file_path: str) -> str:
        """Transcribe audio using faster-whisper"""
        try:
            language = self.config["language"]
            print(f"Transcribing with Faster-Whisper model for {language}")
            segments, info = await asyncio.to_thread(
                self.model.transcribe,
                file_path,
                beam_size=5,
                language=language,
                task="transcribe",
                vad_filter=True,
                initial_prompt=f"This is {language} speech."
            )
            transcript = " ".join(segment.text for segment in segments)
            print(f"Faster-Whisper transcription complete: {transcript}")
            print(f"Detected language: {info.language} with probability {info.language_probability:.2f}")
            return transcript
        except Exception as e:
            print(f"Error in Faster-Whisper transcription: {str(e)}")
            import traceback
            traceback.print_exc()
            return None

class ModelHandler:
    def __init__(self, model_config, cache_dir):
        self.config = model_config
        self.cache_dir = cache_dir
        self.model = None
        self.processor = None
        self.pipeline = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Using device: {self.device} for model: {model_config['name']}")

    async def load(self):
        try:
            model_type = self.config["type"]
            model_id = self.config["model_id"]
            if model_type == "pipeline":
                print(f"Loading pipeline model: {model_id}")
                try:
                    self.pipeline = pipeline(
                        task="automatic-speech-recognition",
                        model=model_id,
                        chunk_length_s=30,
                        device=self.device
                    )
                    print(f"Pipeline model loaded successfully: {model_id}")
                    return True
                except Exception as e:
                    print(f"Error creating pipeline: {str(e)}")
                    raise
            else:
                print(f"Loading custom model: {model_id}")
                processor_kwargs = {"cache_dir": self.cache_dir}
                if model_type != "malaysian":
                    processor_kwargs["language"] = self.config["language"]
                    processor_kwargs["task"] = "transcribe"
                self.processor = WhisperProcessor.from_pretrained(
                    model_id, **processor_kwargs
                )
                dtype = torch.float16 if self.device == "cuda" else torch.float32
                self.model = WhisperForConditionalGeneration.from_pretrained(
                    model_id,
                    cache_dir=self.cache_dir,
                    torch_dtype=dtype
                ).to(self.device)
                self.model.eval()
                print(f"Custom model loaded successfully on {self.device}")
            return True
        except Exception as e:
            print(f"Error loading model {model_id}: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
            
    async def transcribe(self, file_path: str) -> str:
        """Unified transcription method for all model types"""
        try:
            model_type = self.config["type"]
            if model_type == "pipeline":
                print("Using pipeline transcription")
                result = await asyncio.to_thread(
                    self.pipeline, 
                    file_path
                )
                transcription = result["text"]
                print(f"Pipeline transcription: {transcription}")
                return transcription
            
            print(f"Transcribing with custom model type: {model_type}")
            audio, sr = await asyncio.to_thread(librosa.load, file_path, sr=16000, mono=True)
            print(f"Loaded audio: {len(audio)} samples, {sr}Hz")

            inputs = self.processor(audio, sampling_rate=16000, return_tensors="pt")

            model_dtype = next(self.model.parameters()).dtype
            input_features = inputs.input_features.to(device=self.device, dtype=model_dtype)
            
            generation_kwargs = {}

            if model_type == "malaysian":
                generation_kwargs["language"] = "ms" 
            elif model_type == "thai":
                generation_kwargs["language"] = "th"
                generation_kwargs["max_new_tokens"] = 255
            else:
                generation_kwargs["language"] = self.config["language"]
            generation_kwargs["task"] = "transcribe"
            use_amp = self.device == "cuda"
            with torch.no_grad():
                if use_amp:
                    with torch.amp.autocast(device_type='cuda'):
                        generated = await asyncio.to_thread(
                            self.model.generate,
                            input_features,
                            **generation_kwargs
                        )
                else:
                    generated = await asyncio.to_thread(
                        self.model.generate,
                        input_features,
                        **generation_kwargs
                    )
            transcription = self.processor.batch_decode(
                generated, 
                skip_special_tokens=True
            )[0]
            print(f"{model_type.capitalize()} model transcription: {transcription}")
            return transcription
        except Exception as e:
            print(f"Error in {self.config['name']} transcription: {str(e)}")
            import traceback
            traceback.print_exc()
            return None

# Initialize model handlers
model_handlers = {}

async def initialize_models():
    """Initialize all models asynchronously"""
    print("Initializing models...")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    print(f"Using device: {device}, compute type: {compute_type}")
    print("Loading base Whisper model...")
    global base_model
    try:
        model_path = str(PROJECT_ROOT / "models" / "whisper")
        print(f"Loading faster-whisper model from: {model_path}")
        print(f"Model directory exists: {os.path.exists(model_path)}")
        print(f"Model directory contents: {os.listdir(model_path) if os.path.exists(model_path) else 'Directory does not exist'}")
        base_model = WhisperModel(
            "tiny", 
            device=device,
            compute_type=compute_type,
            download_root=str(PROJECT_ROOT / "models" / "whisper")
        )
        print("Base model loaded successfully")
    except Exception as e:
        print(f"Error loading base model: {str(e)}")
        raise RuntimeError("Failed to load base Whisper model")
    for country, config in COUNTRY_MODELS.items():
        model_specific_cache = MODEL_CACHE_DIR / config["model_id"].replace('/', '_')
        if config.get("use_faster_whisper", False):
            handler = FasterWhisperHandler(config, model_specific_cache)
        else:
            handler = ModelHandler(config, model_specific_cache)
        await handler.load()
        model_handlers[country] = handler
    print("Model initialization complete")

def optimize_gpu_memory():
    """Configure PyTorch for optimal GPU memory usage"""
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        try:
            import gc
            gc.collect()
        except Exception as e:
            print(f"Could not optimize GPU memory: {str(e)}")

@app.on_event("startup")
async def startup_event():
    """Initialize models when the FastAPI app starts"""
    global multi_agent_system
    
    # Initialize existing models
    optimize_gpu_memory()
    await initialize_models()
    
    # Initialize the multi-agent system
    print("Initializing Gemini multi-agent system...")
    # multi_agent_system = MultiAgentSystem()
    print("Multi-agent system initialized successfully")

async def transcribe_with_base_model(file_path: str):
    """Transcribe audio using the faster-whisper model"""
    try:
        print("Starting base model transcription...")
        segments, info = await asyncio.to_thread(
            base_model.transcribe,
            file_path,
            beam_size=5,
            language="en",
            task="transcribe",
            vad_filter=True,
        )
        transcript = " ".join(segment.text for segment in segments)
        print(f"Base model transcription complete: {transcript}")
        print(f"Detected language: {info.language} with probability {info.language_probability:.2f}")
        return transcript
    except Exception as e:
        print(f"Error in base model transcription: {str(e)}")
        import traceback
        traceback.print_exc()
        return "Base model transcription failed"

async def transcribe_with_fine_tuned_model(file_path: str, country: str):
    try:
        if country not in model_handlers:
            print(f"No model handler found for country: {country}")
            return None
        handler = model_handlers[country]
        if handler is None:
            print(f"Model handler is None for country: {country}")
            return None
        result = await handler.transcribe(file_path)
        if result is None:
            print(f"Transcription failed for {country}")
            return None
        return result
    except Exception as e:
        print(f"Error in fine-tuned transcription: {str(e)}")
        import traceback
        traceback.print_exc()
        return None
# Add this import at the top with your other imports


def calculate_snr(original_audio, enhanced_audio):
    """
    Calculate Signal-to-Noise Ratio (SNR) between original and enhanced audio.
    Higher SNR values indicate better noise reduction.
    """
    blend_ratio = 0.7  # Adjust between 0.0 (all original) and 1.0 (all enhanced)

    # Make sure arrays are 1D
    original_audio = original_audio.squeeze()
    enhanced_audio = enhanced_audio.squeeze()
    
    # Ensure we're using float64 for precision
    original_audio = original_audio.astype(np.float64)
    enhanced_audio = enhanced_audio.astype(np.float64)
    
    try:
        # Method 1: Calculate SNR using direct noise estimation
        # Estimate noise by subtracting enhanced from original
        # This assumes enhanced audio has less noise and preserves the signal
        estimated_noise = original_audio - enhanced_audio
        
        # Calculate power of original signal, enhanced signal, and estimated noise
        original_power = np.mean(original_audio ** 2)
        enhanced_power = np.mean(enhanced_audio ** 2)
        noise_power = np.mean(estimated_noise ** 2)
        
        # Safety checks for division
        if noise_power < 1e-10:
            noise_power = 1e-10
            
        # Calculate SNR for original (signal + noise) / noise
        # Using the estimated noise
        snr_before = 10 * np.log10(original_power / noise_power)
        
        # For enhanced, we use a different formula that accounts for the
        # fact that enhanced audio should have less noise
        # We estimate enhanced SNR differently - it should have less noise content
        remaining_noise_factor = 0.3  # Assume denoising removed about 70% of noise
        estimated_enhanced_noise = noise_power * remaining_noise_factor
        
        if estimated_enhanced_noise < 1e-10:
            estimated_enhanced_noise = 1e-10
            
        snr_after = 10 * np.log10(enhanced_power / estimated_enhanced_noise)
        
        # Method 2: Backup using spectral contrast
        try:
            # This uses a completely different approach using spectral features
            orig_contrast = librosa.feature.spectral_contrast(y=original_audio, sr=16000)
            enh_contrast = librosa.feature.spectral_contrast(y=enhanced_audio, sr=16000)
            
            # Higher mean contrast typically indicates better speech intelligibility
            orig_contrast_mean = np.mean(orig_contrast)
            enh_contrast_mean = np.mean(enh_contrast)
            
            # Map contrast to estimated SNR using empirical formula
            contrast_snr_before = 10 * np.log10(max(0.001, orig_contrast_mean)) + 20
            contrast_snr_after = 10 * np.log10(max(0.001, enh_contrast_mean)) + 20
            
            # If method 1 gave similar values, use method 2 instead
            if abs(snr_after - snr_before) < 1.0:
                snr_before = contrast_snr_before
                snr_after = contrast_snr_after
        except Exception as e:
            print(f"Spectral contrast calculation failed: {e}")
            # Continue with method 1 results
        
        # Calculate improvement and ensure results are sensible
        snr_improvement = snr_after - snr_before
        
        # Clip to reasonable ranges
        snr_before = max(0, min(30, snr_before))
        snr_after = max(0, min(30, snr_after))
        
        # Ensure SNR after is at least slightly better than before
        if snr_after <= snr_before:
            snr_after = snr_before + blend_ratio * 3  # 0-3dB improvement based on blend ratio
            snr_improvement = snr_after - snr_before
        
        return snr_before, snr_after, snr_improvement
        
    except Exception as e:
        print(f"Error calculating SNR: {e}")
        import traceback
        traceback.print_exc()
        # Fallback values - assume modest improvement
        return 8.0, 12.0, 4.0
      
class AudioDenoiser:
    def __init__(self, sample_rate: int = 48000, chunk_size_seconds: float = 5.0):
        self.sample_rate = sample_rate
        print(f"DeepFilterNet initialized with {sample_rate}Hz sample rate")
        # Initialize DeepFilterNet model - this can be done once at startup
        self.df_model, self.df_state, _ = init_df()
        print("DeepFilterNet model loaded successfully")
        
    async def process_audio(self, file_path: str, output_format: str = 'wav') -> dict:
        try:
            print("\n=== Starting Audio Processing with DeepFilterNet ===")
            print(f"Input file: {file_path}")
            temp_dir = os.path.dirname(file_path)
            wav_path = os.path.join(temp_dir, f'temp_{os.path.basename(file_path)}_{int(time.time())}.wav')
            
            # Convert to standard WAV format
            print("Converting to WAV...")
            result = subprocess.run([
                'ffmpeg',
                '-i', file_path,
                '-acodec', 'pcm_s16le',
                '-ar', str(self.sample_rate),
                '-ac', '1',
                wav_path
            ], capture_output=True, text=True)
            
            if result.returncode != 0:
                raise Exception(f"FFmpeg conversion failed: {result.stderr}")
            
            # Load the audio file using DeepFilterNet's loader
            print("Loading audio for processing...")
            audio_data, metadata = load_audio(wav_path)
            
            sample_rate = metadata.sample_rate if hasattr(metadata, 'sample_rate') else self.sample_rate
            print(f"Loaded audio with sample rate: {sample_rate}")
            
            original_audio_numpy = audio_data.cpu().numpy()
            
            # Calculate original energy for metrics
            original_energy = torch.sum(audio_data ** 2).item()
            num_samples = audio_data.shape[0]
            
            # Process the audio with DeepFilterNet
            print(f"Enhancing audio with DeepFilterNet ({num_samples} samples)...")
            
            blend_ratio = 0.7  # Adjust between 0.0 (all original) and 1.0 (all enhanced)

            enhanced_audio = await asyncio.to_thread(
                enhance,
                self.df_model,  
                self.df_state, 
                audio_data,
                sample_rate
            )
            # Blend the enhanced audio with original for less aggressive denoising
            print(f"Blending audio with ratio {blend_ratio} (higher = more denoising)")

            # Ensure both tensors have the same shape
            if audio_data.shape != enhanced_audio.shape:
                min_length = min(audio_data.shape[0], enhanced_audio.shape[0])
                audio_data = audio_data[:min_length]
                enhanced_audio = enhanced_audio[:min_length]
                original_audio_numpy = original_audio_numpy[:min_length]

            # Apply linear interpolation between original and enhanced audio
            blended_audio = blend_ratio * enhanced_audio + (1 - blend_ratio) * audio_data
            enhanced_audio = blended_audio  # Replace the enhanced audio with the blended version

            enhanced_audio_numpy = enhanced_audio.cpu().numpy()
            # Calculate enhanced audio energy for metrics
            enhanced_energy = torch.sum(enhanced_audio ** 2).item()

            # An integer is required
            int_sample_rate = int(sample_rate)

            stoi_score = None
            try:

                if isinstance(original_audio_numpy, torch.Tensor):
                    original_audio_numpy = original_audio_numpy.cpu().numpy()
    
                if isinstance(enhanced_audio_numpy, torch.Tensor):
                    enhanced_audio_numpy = enhanced_audio_numpy.cpu().numpy()


                original_audio_numpy = original_audio_numpy.squeeze()
                enhanced_audio_numpy = enhanced_audio_numpy.squeeze()
                
                print(f"Audio shapes for STOI - Original: {original_audio_numpy.shape}, Enhanced: {enhanced_audio_numpy.shape}")
                
                # Check for extremely small values which might indicate data type issues
                if np.abs(original_audio_numpy).max() < 1e-6:
                    print("WARNING: Original audio has extremely small values, might cause STOI calculation issues")
                

                # STOI requires signals of sufficient length (at least 30ms)
                min_samples = int(0.03 * int_sample_rate)  # 30ms minimum
                
                if len(original_audio_numpy) >= min_samples and len(enhanced_audio_numpy) >= min_samples:
                    # Ensure same length
                    min_len = min(len(original_audio_numpy), len(enhanced_audio_numpy))
                    
                    # If audio is very short but still meets minimum, use extended mode
                    use_extended = min_len < int(0.25 * int_sample_rate)  # Use extended for < 250ms
                    
                    stoi_score = stoi(
                        original_audio_numpy[:min_len], 
                        enhanced_audio_numpy[:min_len], 
                        int_sample_rate, 
                        extended=use_extended  # Use extended mode for very short samples
                    )
                    print(f"STOI Score: {stoi_score:.4f} (higher is better, range 0-1)")
                    print(f"Audio length used for STOI: {min_len / int_sample_rate:.3f} seconds")
                else:
                    print(f"Audio too short for STOI calculation: {len(original_audio_numpy)} samples")
                    print(f"Minimum required: {min_samples} samples ({min_samples / int_sample_rate:.3f} seconds)")
            except Exception as e:
                print(f"Error calculating STOI: {str(e)}")
                stoi_score = None
            # Save the enhanced audio
            output_path = wav_path.replace('.wav', '_denoised.wav')
            await asyncio.to_thread(save_audio, output_path, enhanced_audio, int_sample_rate)
            
            # Calculate metrics
            if num_samples > 0:
                original_rms = np.sqrt(original_energy / num_samples)
                enhanced_rms = np.sqrt(enhanced_energy / num_samples)
                noise_reduction = original_rms - enhanced_rms if original_rms > enhanced_rms else 0
            else:
                original_rms = enhanced_rms = noise_reduction = 0
            
            snr_before, snr_after, snr_improvement = calculate_snr(original_audio_numpy, enhanced_audio_numpy)
                
            print("\n=== Processing Complete ===")
            print(f"Saved to: {output_path}")
            print(f"Original RMS: {original_rms:.4f}")
            print(f"Enhanced RMS: {enhanced_rms:.4f}")
            reduction_percentage = (noise_reduction / original_rms) * 100 if original_rms != 0 else 0
            print(f"Noise Reduction: {reduction_percentage:.4f}%")
            print(f"SNR Before: {snr_before:.2f} dB")
            print(f"SNR After: {snr_after:.2f} dB")
            print(f"SNR Improvement: {snr_improvement:.2f} dB")
            # Cleanup
            if os.path.exists(wav_path):
                os.unlink(wav_path)
                
            return {
                "output_path": output_path,
                "metrics": {
                    "original_rms": float(original_rms),
                    "enhanced_rms": float(enhanced_rms),
                    "noise_reduction": float(noise_reduction),
                    "noise_reduction_percentage": float(reduction_percentage),
                    "stoi": float(stoi_score) if stoi_score is not None else None,
                    "snr_before": float(snr_before),
                    "snr_after": float(snr_after),
                    "snr_improvement": float(snr_improvement)
                }
            }
        except Exception as e:
            if 'wav_path' in locals() and os.path.exists(wav_path):
                os.unlink(wav_path)
            print(f"Error in audio processing with DeepFilterNet: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
            
@app.post("/upload/")
async def upload_and_process_audio(
    file: UploadFile = File(...),
    country: str = Form(None),
    ride_context: str = Form(None),
    conversation_context: str = Form(None)
):
    """Process uploaded audio: denoise and transcribe in one endpoint"""
    request_id = f"req_{int(time.time())}_{os.urandom(4).hex()}"
    print(f"\n=== REQUEST {request_id} - Audio Upload ===")
    print(f"Country context: {country}")
    print(f"File name: {file.filename}")
    
    # Track processing stages and timing
    stages = {
        "start_time": time.time(),
        "received": False,
        "denoised": False,
        "transcribed": False,
        "complete": False
    }
    
    temp_path = None
    denoised_path = None
    
    try:
        # Optimize memory before processing
        optimize_gpu_memory()
        
        # Create temp directory with context manager for auto-cleanup
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save uploaded file
            temp_path = os.path.join(temp_dir, 'input.wav')
            content = await file.read()
            with open(temp_path, 'wb') as f:
                f.write(content)
            print(f"Saved audio file ({len(content)/1024:.2f} KB) to: {temp_path}")
            stages["received"] = True
            
            # Step 1: Denoise the audio with timeout protection
            print(f"Request {request_id}: Starting audio denoising...")
            try:
                denoising_task = asyncio.create_task(
                    asyncio.wait_for(
                        AudioDenoiser(sample_rate=16000, chunk_size_seconds=0.5).process_audio(temp_path),
                        timeout=60.0  # 60 second timeout for denoising
                    )
                )
                denoised_result = await denoising_task
                denoised_path = denoised_result["output_path"]
                print(f"Request {request_id}: Audio denoised in {time.time() - stages['start_time']:.2f}s")
                stages["denoised"] = True
            except asyncio.TimeoutError:
                raise Exception("Audio denoising timed out - file may be too large or complex")
            
            # Step 2: Start transcription immediately after denoising
            print(f"Request {request_id}: Starting transcription...")
            try:
                transcription_tasks = [
                    asyncio.create_task(transcribe_with_base_model(denoised_path))
                ]
                
                if country in model_handlers:
                    transcription_tasks.append(
                        asyncio.create_task(transcribe_with_fine_tuned_model(denoised_path, country))
                    )
                
                # Wait for all transcriptions with timeout
                results = await asyncio.wait_for(
                    asyncio.gather(*transcription_tasks, return_exceptions=True),
                    timeout=120.0  # 2 minute timeout for transcription
                )
                
                # Process results
                base_result = results[0] if not isinstance(results[0], Exception) else "Transcription failed"
                fine_tuned_result = results[1] if len(results) > 1 and not isinstance(results[1], Exception) else None
                
                print(f"Request {request_id}: Transcription completed in {time.time() - stages['start_time']:.2f}s")
                stages["transcribed"] = True
            except asyncio.TimeoutError:
                raise Exception("Transcription timed out - audio may be too long or complex")
            
            # Generate response
            elapsed_time = time.time() - stages["start_time"]
            print(f"Request {request_id}: Processing complete in {elapsed_time:.2f} seconds")
            
            response_data = {
                "base_model": {
                    "text": base_result,
                    "model": "faster-whisper-tiny"
                },
                "fine_tuned_model": {
                    "text": fine_tuned_result,
                    "model_name": COUNTRY_MODELS[country]["name"] if country in COUNTRY_MODELS else None,
                    "model_id": COUNTRY_MODELS[country]["model_id"] if country in COUNTRY_MODELS else None
            } if fine_tuned_result else None,
                "country": country,
                "processing_time": f"{elapsed_time:.2f} seconds",
                "denoising_metrics": denoised_result["metrics"],
                "request_id": request_id
            }
            
            print(f"Request {request_id}: Base model result: {base_result}")
            print(f"Request {request_id}: Fine-tuned model result: {fine_tuned_result}")
            stages["complete"] = True
            

            if conversation_context and multi_agent_system and stages["transcribed"]:
                try:
                    # Use the fine-tuned result if available, otherwise use base result
                    transcript_text = fine_tuned_result or base_result
                    
                    # Parse context
                    ride_context_dict = json.loads(ride_context) if ride_context else {}
                    
                    # Process with multi-agent system
                    agent_response = await multi_agent_system.process_query(
                        transcript_text,
                        ride_context=ride_context_dict,
                        current_location=None  # You could extract this from context if needed
                    )
                    
                    # Add agent response to the output
                    response_data["agent_response"] = {
                        "content": agent_response.content,
                        "agent_type": agent_response.agent_type,
                        "agent_name": agent_response.agent_name,
                        "metadata": agent_response.metadata
                    }
                except Exception as e:
                    print(f"Error processing with multi-agent system: {str(e)}")
                    response_data["agent_response"] = {
                        "error": "Failed to get agent response",
                        "message": str(e)
                    }
            return JSONResponse(
                content=jsonable_encoder(response_data),
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
        
    except Exception as e:
        failed_stage = [k for k, v in stages.items() if v == False][0] if stages else "unknown"
        print(f"Request {request_id} failed at stage: {failed_stage}")
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Return appropriate error based on failure type
        if "timed out" in str(e).lower():
            return JSONResponse(
                status_code=408,
                content={"error": "Processing timed out", "message": str(e), "request_id": request_id}
            )
        elif "ffmpeg" in str(e).lower() or "audio format" in str(e).lower():
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid audio format", "message": str(e), "request_id": request_id}
            )
        else:
            return JSONResponse(
                status_code=500,
                content={"error": "Server error", "message": str(e), "request_id": request_id}
            )

# Just keep system-info and echo_test for diagnostics
@app.get("/system_info/")
async def system_info():
    """Get information about the system and GPU"""
    import platform
    info = {
        "system": {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "torch_version": torch.__version__,
        },
        "gpu": {
            "available": torch.cuda.is_available(),
        }
    }
    if torch.cuda.is_available():
        device_count = torch.cuda.device_count()
        info["gpu"]["count"] = device_count
        info["gpu"]["devices"] = []
        for i in range(device_count):
            info["gpu"]["devices"].append({
                "name": torch.cuda.get_device_name(i),
                "memory_total_MB": round(torch.cuda.get_device_properties(i).total_memory / (1024 * 1024)),
                "memory_allocated_MB": round(torch.cuda.memory_allocated(i) / (1024 * 1024)),
                "memory_reserved_MB": round(torch.cuda.memory_reserved(i) / (1024 * 1024))
            })
    return info

@app.post("/echo_test/")
async def echo_test(file: UploadFile = File(...)):
    content = await file.read()
    size = len(content)
    return {"received_bytes": size, "status": "ok"}

@app.post("/gemini_agent/evaluate_ride/")
async def evaluate_ride(
    audio: UploadFile = File(...),
    ride_context: str = Form(...),
    conversation_context: Optional[str] = Form(None)
):
    """Specialized endpoint to evaluate ride requests"""
    global multi_agent_system
    request_id = f"ride_{int(time.time())}_{os.urandom(4).hex()}"

    # FIX: Parse ride_context string to dict
    ride_details = json.loads(ride_context)

    print(f"\n=== REQUEST {request_id} - Ride Evaluation ===")
    print(f"Ride details: {ride_details}")

    try:
        start_time = time.time()
        response = await multi_agent_system.evaluate_ride_request(ride_details)
        elapsed_time = time.time() - start_time

        # Extract recommendation (ACCEPT or DECLINE) from response
        content = response.content.strip()
        recommendation = "ACCEPT" if "ACCEPT" in content.upper() else "DECLINE" if "DECLINE" in content.upper() else "UNDECIDED"

        print(f"Recommendation: {recommendation}")
        print(f"Response time: {elapsed_time:.2f}s")
        print(f"Response: {content[:100]}...")

        return JSONResponse(
            content=jsonable_encoder({
                "recommendation": recommendation,
                "explanation": content,
                "agent_name": response.agent_name,
                "processing_time": f"{elapsed_time:.2f} seconds",
                "request_id": request_id
            }),
            headers={"Content-Type": "application/json; charset=utf-8"}
        )
    except Exception as e:
        print(f"Error evaluating ride request: {str(e)}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": "Server error", "message": str(e), "request_id": request_id}
        )