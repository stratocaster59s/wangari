# Wangari and the Little Green Tree — Reader's Theater Player

A responsive web page (phone + desktop) for practising the English reader's theater
*Wangari and the Little Green Tree*. Every line is pre-rendered with Google Cloud
Gemini-TTS, so playback needs no live TTS on the device.

- Pick a scene, play the whole scene, or tap any line to play just that line.
- Speed control (0.6×–1.25×), relative to the baked-in `tempo` (see below).
- **我的角色 (My role)**: lines for the chosen role (and "All") are left silent with a
  pause, so the student reads them aloud.

## Files

| Path | Purpose |
|---|---|
| `script.json` | Single source of truth: script text, five character voices, tone prompts |
| `takes/` | Raw Gemini-TTS takes (kept because TTS output is not reproducible) |
| `generate.py` | Renders `script.json` to `audio/<scene>/<nn>.mp3` and writes `audio/manifest.js` |
| `index.html` | The player page (works from `file://` or any static host) |

## Adjusting voices and tone

Edit `script.json`:

- `characters.<id>.voice` — Gemini-TTS voice name (e.g. `Leda`, `Puck`, `Charon`).
- `characters.<id>.style` — natural-language description of how the character speaks.
- `lines[].tone` — optional per-line direction, e.g. `"Shocked and worried."`
- `chorus` — voices mixed together for "All" lines.
- `tempo` — speed baked into the published audio with ffmpeg (1× on the page); changing it re-uses the saved takes in `takes/`, no TTS calls.

Then re-run the generator; only changed lines are re-rendered.

```sh
cp .env.example .env   # set GOOGLE_CLOUD_PROJECT
export $(cat .env)
gcloud auth login      # needs Cloud Text-to-Speech + Vertex AI enabled on the project
python3 generate.py                 # render new/changed lines
python3 generate.py --only s2:8     # re-roll one line (scene s2, line 8)
python3 generate.py --force         # re-render everything
```

Requires Python 3.9+, `gcloud`, and `ffmpeg`/`ffprobe` (for mixing chorus lines).

## Local preview

Open `index.html` directly, or `python3 -m http.server` and visit `http://localhost:8000`.
