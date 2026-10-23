#!/usr/bin/env python3
"""Pre-render every line of script.json to MP3 with Google Cloud Gemini-TTS.

Usage:
  GOOGLE_CLOUD_PROJECT=<project-id> python3 generate.py [--force] [--only s2:5 ...]

Auth uses `gcloud auth print-access-token`. A line is re-rendered only when its
text / voice / style changes (tracked by hash in audio/manifest.js), unless
--force or --only is given. Multi-speaker lines ("All", "Wangari and Friend")
are rendered per voice and mixed with ffmpeg.
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUDIO = ROOT / "audio"
MANIFEST = AUDIO / "manifest.js"
MANIFEST_PREFIX = "window.MANIFEST = "
ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

_token = {"value": None, "at": 0.0}
_token_lock = threading.Lock()


def access_token():
    with _token_lock:
        if not _token["value"] or time.time() - _token["at"] > 1800:
            _token["value"] = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"], text=True).strip()
            _token["at"] = time.time()
        return _token["value"]


def synthesize(project, model, lang, voice, prompt, text, retries=5):
    body = json.dumps({
        "input": {"prompt": prompt, "text": text},
        "voice": {"languageCode": lang, "name": voice, "model_name": model},
        "audioConfig": {"audioEncoding": "MP3", "sampleRateHertz": 24000},
    }).encode()
    for attempt in range(retries):
        req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
            "Authorization": f"Bearer {access_token()}",
            "x-goog-user-project": project,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return base64.b64decode(json.load(resp)["audioContent"])
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"TTS {e.code}: {msg}") from None


def build_prompt(script, style, tone):
    parts = [script["globalStyle"], style]
    if tone:
        parts.append(f"Tone for this line: {tone}")
    return " ".join(parts)


def line_jobs(script):
    """Yield (scene_id, index, line, [(voice, prompt), ...]) for every line."""
    chars, chorus = script["characters"], script["chorus"]
    for scene in script["scenes"]:
        for i, line in enumerate(scene["lines"], 1):
            if line["who"] == "all":
                keys = chorus["voices"]
                styles = [chorus["style"]] * len(keys)
            else:
                keys = line["who"]
                styles = [chars[k]["style"] for k in keys]
                if len(keys) > 1:
                    styles = [s + " You are saying this line together with another character." for s in styles]
            voices = [(chars[k]["voice"], build_prompt(script, s, line.get("tone")))
                      for k, s in zip(keys, styles)]
            yield scene["id"], i, line, voices


def mix(parts, out):
    """Overlay several takes into one chorus clip, slightly offset to sound natural."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in parts:
        cmd += ["-i", str(p)]
    delays = "".join(f"[{i}:a]adelay={i * 40}|{i * 40}[d{i}];" for i in range(len(parts)))
    inputs = "".join(f"[d{i}]" for i in range(len(parts)))
    cmd += ["-filter_complex",
            f"{delays}{inputs}amix=inputs={len(parts)}:normalize=0,volume={1.6 / len(parts):.3f},alimiter=limit=0.95",
            "-ac", "1", "-b:a", "96k", str(out)]
    subprocess.run(cmd, check=True)


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)], text=True)
    return round(float(out.strip()), 2)


def load_manifest():
    if not MANIFEST.exists():
        return {}
    data = json.loads(MANIFEST.read_text()[len(MANIFEST_PREFIX):].rstrip().rstrip(";"))
    return {f"{s['id']}:{i}": l for s in data["scenes"] for i, l in enumerate(s["lines"], 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-render every line")
    ap.add_argument("--only", nargs="*", default=[], help="re-render specific lines, e.g. s2:5 end:1")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit("Set GOOGLE_CLOUD_PROJECT (see .env.example)")

    script = json.loads((ROOT / "script.json").read_text())
    previous = load_manifest()
    results, errors = {}, []

    def render(job):
        sid, i, line, voices = job
        key = f"{sid}:{i}"
        rel = f"audio/{sid}/{i:02d}.mp3"
        out = ROOT / rel
        digest = hashlib.sha256(json.dumps([script["model"], line["text"], voices]).encode()).hexdigest()[:16]
        prev = previous.get(key)
        if (not args.force and key not in args.only and out.exists()
                and prev and prev.get("hash") == digest):
            results[key] = prev
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            takes = [synthesize(project, script["model"], script["languageCode"], v, p, line["text"])
                     for v, p in voices]
            if len(takes) == 1:
                out.write_bytes(takes[0])
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    paths = []
                    for n, data in enumerate(takes):
                        p = Path(tmp) / f"{n}.mp3"
                        p.write_bytes(data)
                        paths.append(p)
                    mix(paths, out)
        except Exception as e:  # keep going; report at the end
            errors.append(f"{key}: {e}")
            return
        results[key] = {"hash": digest, "src": rel, "dur": duration(out)}
        print(f"  rendered {key}  {line['text'][:50]}")

    jobs = list(line_jobs(script))
    print(f"{len(jobs)} lines, model {script['model']}, project {project}")
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(render, jobs))

    chars = script["characters"]
    manifest = {"title": script["title"], "scenes": []}
    for scene in script["scenes"]:
        lines = []
        for i, line in enumerate(scene["lines"], 1):
            r = results.get(f"{scene['id']}:{i}", {})
            who = ["all"] if line["who"] == "all" else line["who"]
            lines.append({
                "who": who,
                "label": script["chorus"]["label"] if line["who"] == "all"
                else " and ".join(chars[k]["label"] for k in who),
                "text": line["text"],
                "src": r.get("src"), "dur": r.get("dur"), "hash": r.get("hash"),
            })
        manifest["scenes"].append({k: scene[k] for k in ("id", "title", "titleZh")} | {"lines": lines})
    manifest["roles"] = [{"id": k, "label": c["label"]} for k, c in chars.items()]
    AUDIO.mkdir(exist_ok=True)
    MANIFEST.write_text(MANIFEST_PREFIX + json.dumps(manifest, ensure_ascii=False, indent=1) + ";\n")

    if errors:
        print(f"\n{len(errors)} line(s) failed:\n  " + "\n  ".join(errors))
        sys.exit(1)
    print(f"done -> {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
