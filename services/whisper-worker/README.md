# Whisper Worker

This worker processes speech-to-text transcription jobs using OpenAI Whisper.

## Features

- **Dual Queue Support**: Listens to both `queue:stt.transcribe` (replacing the placeholder STT worker) and `queue:whisper.transcribe` (dedicated Whisper queue)
- **Multiple Output Formats**: Supports JSON, plain text, SRT, and WebVTT formats
- **Language Support**: Auto-detection or specific language codes
- **Whisper Parameters**: Supports temperature, initial prompts, and other Whisper-specific options
- **MinIO Integration**: Downloads audio from MinIO, uploads results back
- **Job Management**: Full integration with the existing job database and status tracking

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_URL` | `http://whisper:9000` | URL of the Whisper service |
| `WHISPER_MODEL` | `base` | Whisper model size (tiny, base, small, medium, large, large-v2, large-v3) |
| `WHISPER_LANGUAGE` | `auto` | Default language or 'auto' for detection |
| `REQUEST_TIMEOUT` | `300` | Timeout for Whisper API requests (seconds) |
| `QUEUE_STT` | `queue:stt.transcribe` | Redis queue for STT jobs |
| `QUEUE_WHISPER` | `queue:whisper.transcribe` | Redis queue for Whisper-specific jobs |

## Supported Output Formats

- **json**: Full Whisper response with segments, timestamps, and metadata
- **text**: Plain text transcription
- **srt**: SubRip subtitle format with timestamps
- **vtt**: WebVTT subtitle format with timestamps

## API Endpoints

The gateway now provides two endpoints:

1. **`/v1/stt/transcribe`** - Generic STT endpoint (processed by Whisper worker)
2. **`/v1/whisper/transcribe`** - Whisper-specific endpoint with additional parameters

## Usage Examples

### Whisper-Specific Endpoint

```bash
# Basic transcription (JSON output)
curl -X POST "http://localhost:9001/v1/whisper/transcribe" \
  -F "audio=@audio.wav;type=audio/wav" \
  -F "audio_mime_type=audio/wav" \
  -F "language=en" \
  -F "output_format=json" \
  -F "temperature=0.2"

# Generate SRT subtitles
curl -X POST "http://localhost:9001/v1/whisper/transcribe" \
  -F "audio=@audio.wav;type=audio/wav" \
  -F "audio_mime_type=audio/wav" \
  -F "language=en" \
  -F "output_format=srt" \
  -F "temperature=0.0"

# Plain text transcription with auto language detection
curl -X POST "http://localhost:9001/v1/whisper/transcribe" \
  -F "audio=@audio.wav;type=audio/wav" \
  -F "audio_mime_type=audio/wav" \
  -F "output_format=text"

# With initial prompt to guide transcription
curl -X POST "http://localhost:9001/v1/whisper/transcribe" \
  -F "audio=@audio.wav;type=audio/wav" \
  -F "audio_mime_type=audio/wav" \
  -F "language=en" \
  -F "prompt=This is a conversation about technology" \
  -F "output_format=json"
```

### Generic STT Endpoint (Powered by Whisper)

```bash
# Using the standard STT endpoint
curl -X POST "http://localhost:9001/v1/stt/transcribe" \
  -F "audio=@audio.wav;type=audio/wav" \
  -F "audio_mime_type=audio/wav" \
  -F "output_format=text"

# Generate WebVTT subtitles
curl -X POST "http://localhost:9001/v1/stt/transcribe" \
  -F "audio=@audio.wav;type=audio/wav" \
  -F "audio_mime_type=audio/wav" \
  -F "output_format=vtt"
```

### Using the Test Script

```bash
# Test with different files and formats
./test-whisper.sh test-audio/sample.wav json
./test-whisper.sh test-audio/sample.wav srt
./test-whisper.sh test-audio/sample.wav text
./test-whisper.sh voices/inara/any_voice_file.wav vtt
```

### Important Notes

- **MIME Type**: Always include `;type=audio/wav` in the file upload and set `audio_mime_type=audio/wav`
- **Temperature**: Pass as a number (0.2) not a string ('0.2')
- **Language**: Use language codes like 'en', 'es', 'fr', or omit for auto-detection
- **Output Formats**: json, text, srt, vtt

## Integration

The Whisper worker seamlessly integrates with your existing TTS stack:

- Uses the same Redis queue system
- Stores results in the same MinIO bucket
- Updates the same job database
- Follows the same job lifecycle (queued → running → succeeded/failed)