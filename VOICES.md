# TSS Voice Controls – Extended Slider Specification

This document defines **additional, future‑proof voice control sliders** suitable for an XTTS‑style TTS stack today, while remaining compatible with future engines and multi‑user hosting.

The goal is to:

* improve realism and expressiveness
* avoid engine‑specific lock‑in
* allow `/v1/capabilities` to enable/disable controls dynamically

---

## 0. Staged Implementation Plan (recommended)

The UI should **only render what `/v1/capabilities` enables**. We’ll roll features out in stages so each new knob is:
- actually applied (not placebo)
- bounded and safe
- easy to disable per deployment tier

### Stage 0 (already in place)

- **Contracts/UI plumbing**: `/v1/capabilities` driven sliders; request passes `controls` through to workers.
- **Core DSP** (applied post‑TTS): `speed`, `pitch_semitones`, `energy`, `pause_ms`

### Stage 1 (high impact, low risk — implement next)

- **Text post-processing**:
  - `sentence_pause_ms`
  - `pause_variance_ms`
- **Post DSP**:
  - `loudness_db`
  - `post_eq_profile` (neutral/warm/broadcast/crisp)
  - `clarity_boost` (simple EQ/transient-ish shaping)
  - `breathiness` (noise blending)

### Stage 2 (medium complexity)

- `tempo_variance` (micro-variation)
- `articulation` (envelope + tempo shaping proxy)
- `intensity`, `variation` (controlled jitter within safe bounds)
- `punctuation_weight`, `sentence_split_aggressiveness` (text shaping / chunking)

### Stage 3 (advanced / specialist DSP)

- `formant_shift` (true formant processing)
- `nasality` (formant/EQ shaping beyond simple presets)
- `emphasis_strength`, `repeat_emphasis` (token/phoneme-level shaping)
- `latency_mode`, `stream_chunk_ms` (streaming architecture)

---

## 1. Prosody & Timing (High Value, Low Risk)

### `pause_variance_ms`

Adds slight randomness to pauses so speech doesn’t sound metronomic.

* **Range:** 0–120 ms
* **Default:** 20 ms
* **Stage:** text post‑processing

---

### `sentence_pause_ms`

Extra pause after sentence boundaries only.

* **Range:** 0–600 ms
* **Default:** 120 ms
* **Stage:** text post‑processing

---

### `prosody_depth`

Controls how exaggerated pitch and intonation contours are.

* **UX label:** Flat ↔ Expressive
* **Range:** 0.0–1.0
* **Default:** 0.4
* **Stage:** engine‑mapped or post‑DSP envelope shaping

---

### `tempo_variance`

Micro‑variation in speaking rate within sentences.

* **Range:** 0.0–0.05 (±5%)
* **Default:** 0.015
* **Stage:** post‑DSP

---

## 2. Voice Color & “Gender Tilt” (Safe Implementation)

### `formant_shift`

Controls perceived vocal body and resonance.

* **UX label:** Voice body
* **Range:** -1.0 to +1.0
* **Default:** 0.0
* **Stage:** post‑DSP (formant shifting)

---

### `breathiness`

Adds aspiration and air noise for softer voices.

* **Range:** 0.0–1.0
* **Default:** 0.2
* **Stage:** post‑DSP noise blending

---

### `nasality`

Emphasizes nasal resonances.

* **Range:** 0.0–0.6
* **Default:** 0.0
* **Stage:** post‑DSP EQ shaping

---

## 3. Expressiveness & Emotion (Engine‑Agnostic)

### `intensity`

Overall emotional punch and emphasis.

* **UX label:** Calm ↔ Intense
* **Range:** 0.0–1.0
* **Default:** 0.4
* **Stage:** amplitude + pitch variance shaping

---

### `emphasis_strength`

Strength of stressed word emphasis.

* **Range:** 0.0–1.0
* **Default:** 0.5
* **Stage:** token‑level or post‑DSP

---

### `variation`

Randomness in delivery to avoid identical outputs.

* **Range:** 0.0–1.0
* **Default:** 0.3
* **Stage:** engine sampling or DSP jitter

---

## 4. Clarity & Intelligibility (Assistant‑Friendly)

### `clarity_boost`

Enhances consonant sharpness and intelligibility.

* **Range:** 0.0–1.0
* **Default:** 0.5
* **Stage:** EQ + transient shaping

---

### `articulation`

Controls precision of syllables and consonants.

* **UX label:** Relaxed ↔ Precise
* **Range:** 0.0–1.0
* **Default:** 0.6
* **Stage:** tempo + envelope shaping

---

### `loudness_db`

Final output gain adjustment.

* **Range:** -12 dB to +6 dB
* **Default:** 0 dB
* **Stage:** post‑DSP

---

## 5. Text Handling Controls (High Impact, Cheap)

### `sentence_split_aggressiveness`

Controls how aggressively long text is chunked.

* **Range:** 0.0–1.0
* **Default:** 0.5
* **Stage:** text preprocessing

---

### `punctuation_weight`

Strength of punctuation‑driven pauses and intonation.

* **Range:** 0.0–1.0
* **Default:** 0.7
* **Stage:** text preprocessing

---

### `repeat_emphasis`

Reduces emphasis on repeated words to avoid robotic repetition.

* **Range:** 0.0–1.0
* **Default:** 0.4
* **Stage:** token‑level shaping

---

## 6. Advanced / Power‑User Controls (Optional)

### `post_eq_profile`

Preset EQ shaping.

* **Options:** neutral, warm, broadcast, crisp

---

### `latency_mode`

Controls quality vs responsiveness trade‑offs.

* **Options:** quality | balanced | realtime
* **Maps to:** chunk size, overlap, DSP depth

---

### `stream_chunk_ms`

Chunk size for streaming TTS output.

* **Range:** 40–400 ms
* **Default:** 120 ms

---

## 7. Sliders Explicitly Excluded (For Now)

* Raw engine temperature
* Phoneme‑level controls
* Numeric “gender” sliders
* Emotion labels unless natively supported

---

## 8. Recommended UI Grouping

**Voice**

* voice preset
* formant_shift
* pitch_semitones
* breathiness

**Delivery**

* speed
* prosody_depth
* tempo_variance
* pause_ms
* sentence_pause_ms

**Expression**

* energy
* intensity
* variation

**Clarity**

* articulation
* clarity_boost
* loudness_db

**Advanced (collapsed)**

* punctuation_weight
* sentence_split_aggressiveness
* latency_mode

---

## 9. Scaling & `/v1/capabilities`

All controls are:

* optional
* bounded
* deployment‑controlled

The UI must render sliders dynamically based on `/v1/capabilities`, enabling:

* tiered features
* hardware‑dependent limits
* safe multi‑user hosting

---

## Summary

This slider set:

* significantly improves realism
* avoids engine lock‑in
* degrades gracefully when unsupported
* scales cleanly from local dev to hosted multi‑user environments

Use this spec as the **single source of truth** for voice control implementation.
