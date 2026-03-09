#!/bin/bash

# Test script for Whisper transcription
# Usage: ./test-whisper.sh [audio_file] [output_format]

GATEWAY_URL="http://localhost:9001"
AUDIO_FILE="${1:-test-audio/thank_you_mono.wav}"
OUTPUT_FORMAT="${2:-json}"

echo "🎤 Testing Whisper transcription..."
echo "📁 Audio file: $AUDIO_FILE"
echo "📄 Output format: $OUTPUT_FORMAT"
echo "🌐 Gateway URL: $GATEWAY_URL"
echo ""

if [ ! -f "$AUDIO_FILE" ]; then
    echo "❌ Error: Audio file '$AUDIO_FILE' not found!"
    echo "Available test files:"
    ls -la test-audio/ 2>/dev/null || echo "No test-audio directory found. Run: mkdir test-audio && cp voices/samples/inara/*.wav test-audio/"
    exit 1
fi

echo "🚀 Submitting transcription job..."

# Submit the job
RESPONSE=$(curl -s -X POST "$GATEWAY_URL/v1/whisper/transcribe" \
    -F "audio=@$AUDIO_FILE;type=audio/wav" \
    -F "audio_mime_type=audio/wav" \
    -F "language=en" \
    -F "output_format=$OUTPUT_FORMAT" \
    -F "temperature=0.2")

echo "📤 Response: $RESPONSE"
echo ""

# Extract job ID
JOB_ID=$(echo "$RESPONSE" | grep -o '"job_id":"[^"]*"' | cut -d'"' -f4)

if [ -z "$JOB_ID" ]; then
    echo "❌ Failed to get job ID. Response was:"
    echo "$RESPONSE"
    exit 1
fi

echo "🆔 Job ID: $JOB_ID"
echo "⏳ Waiting for transcription to complete..."

# Poll for results
for i in {1..30}; do
    sleep 2
    STATUS_RESPONSE=$(curl -s "$GATEWAY_URL/v1/jobs/$JOB_ID")
    STATUS=$(echo "$STATUS_RESPONSE" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
    
    echo "📊 Status check $i: $STATUS"
    
    if [ "$STATUS" = "succeeded" ]; then
        echo ""
        echo "✅ Transcription completed successfully!"
        echo "📋 Full job details:"
        echo "$STATUS_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$STATUS_RESPONSE"
        
        # Get the result URL
        RESULT_URL=$(echo "$STATUS_RESPONSE" | grep -o '"result_url":"[^"]*"' | cut -d'"' -f4)
        if [ -n "$RESULT_URL" ]; then
            echo ""
            echo "📥 Downloading transcription result..."
            curl -s "$RESULT_URL" > "result_${JOB_ID}.${OUTPUT_FORMAT}"
            echo "💾 Result saved to: result_${JOB_ID}.${OUTPUT_FORMAT}"
            echo ""
            echo "📝 Transcription content:"
            echo "----------------------------------------"
            cat "result_${JOB_ID}.${OUTPUT_FORMAT}"
            echo ""
            echo "----------------------------------------"
        fi
        exit 0
    elif [ "$STATUS" = "failed" ]; then
        echo ""
        echo "❌ Transcription failed!"
        echo "📋 Error details:"
        echo "$STATUS_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$STATUS_RESPONSE"
        exit 1
    elif [ "$STATUS" = "cancelled" ]; then
        echo ""
        echo "⚠️ Job was cancelled"
        exit 1
    fi
done

echo ""
echo "⏰ Timeout waiting for transcription to complete"
echo "📋 Last status:"
echo "$STATUS_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$STATUS_RESPONSE"