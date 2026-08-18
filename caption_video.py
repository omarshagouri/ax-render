"""AmpCoreX Agent 9 — /caption-video endpoint (FastAPI, matches ax-render's app.py).

Burns karaoke-style captions (active word teal, rest white, heavy outline) onto the
assembled {ID}_FINAL.mp4 and returns the captioned MP4 + a sidecar .srt as base64.
Make saves both to Drive, adds a Review row, and flips status to `Review`.

Register in app.py with TWO lines, right after `app.include_router(router)` for assemble:

    from caption_video import router as caption_router
    app.include_router(caption_router)

Design (from Automation Master C.2, tuned to the reference screenshot):
  - IN:  Make sends the Final_file_ID (Drive id of {ID}_FINAL.mp4). The service reads it
         via the existing read-only runtime service account (drive.readonly) — no new scope.
  - TIMING from the final audio via faster-whisper (word_timestamps), NOT the sheet.
  - WORDS from the locked script when `sentences` is supplied (difflib aligns whisper timing
         onto the correct spelling — protects LFP/NMC/C-rate/kWh). Falls back to raw whisper.
  - OUT: captioned MP4 + .srt returned as base64. Make does the Drive writes (proven pattern).

Contract (Make -> service), same x-api-key header as /render-beat and /assemble-video:
  POST /caption-video
  {
    "video_id":  "AX-020-SF",
    "drive_file_id": "1ab...",          # Final_file_ID (the {ID}_FINAL.mp4)
    "sentences": ["...", "..."]         # OPTIONAL: narration lines, in order (whisper-only if absent)
  }
Response:
  {
    "status": "ok",
    "video_id": "AX-020-SF",
    "captioned_filename": "AX-020-SF_FINAL_SUB.mp4",
    "captioned_base64": "<...>",
    "srt_filename": "AX-020-SF.srt",
    "srt_base64": "<...>",
    "srt": "<plain srt text>",
    "event_count": 137,
    "group_count": 58,
    "duration_s": 71.7,
    "used_script": true,
    "engine": "faster-whisper base.en"
  }
"""
import os, io, re, base64, difflib, subprocess, tempfile, shutil
from fastapi import APIRouter, Request, HTTPException

router = APIRouter()

RENDER_API_KEY = os.environ["RENDER_API_KEY"]           # same key as the other endpoints

# ---- caption style (all env-overridable so you can tune without a code change) ----
WHISPER_MODEL   = os.environ.get("CAPTION_WHISPER_MODEL", "base.en")   # baked into the image
FONT_NAME       = os.environ.get("CAPTION_FONT", "Montserrat ExtraBold")
FONT_SIZE       = int(os.environ.get("CAPTION_FONT_SIZE", "108"))      # at 1080x1920
COLOR_WHITE_HEX = os.environ.get("CAPTION_WHITE", "FFFFFF")
COLOR_TEAL_HEX  = os.environ.get("CAPTION_TEAL",  "5EEAD4")            # brand teal (active word)
OUTLINE_PX      = os.environ.get("CAPTION_OUTLINE", "6")
SHADOW_PX       = os.environ.get("CAPTION_SHADOW", "3")
ALIGN           = os.environ.get("CAPTION_ALIGN", "2")                 # 2 = bottom-centre (lower third)
MARGIN_V        = int(os.environ.get("CAPTION_MARGIN_V", "340"))       # px up from the bottom edge
MARGIN_H        = int(os.environ.get("CAPTION_MARGIN_H", "80"))        # left/right safe margin
MAX_WORDS       = int(os.environ.get("CAPTION_MAX_WORDS", "2"))        # words visible per group
MAX_CHARS       = int(os.environ.get("CAPTION_MAX_CHARS", "14"))       # start a new group past this
CHAR_W          = float(os.environ.get("CAPTION_CHAR_W", "0.66"))      # est. glyph width / font size (autofit)
UPPERCASE       = os.environ.get("CAPTION_UPPERCASE", "1") == "1"
OUTRO_TAIL_S    = float(os.environ.get("CAPTION_OUTRO_TAIL_S", "6"))   # secs at the END left uncaptioned (outro clip)
INTRO_HEAD_S    = float(os.environ.get("CAPTION_INTRO_HEAD_S", "1"))   # secs at the START left uncaptioned (logo intro)

_PLAY_W, _PLAY_H = 1080, 1920
_model = None                                                           # lazy-loaded whisper model


# ---------------------------------------------------------------- Drive (read-only)
def _drive():
    from google.auth import default as gad
    from googleapiclient.discovery import build
    creds, _ = gad(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _drive_download(file_id, dst_path):
    from googleapiclient.http import MediaIoBaseDownload
    req = _drive().files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dst_path, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dst_path


# ---------------------------------------------------------------- whisper
def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        # int8 on CPU: small footprint, fast enough for a 60-70s clip well within the timeout.
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model

def _transcribe_words(wav_path, sentences):
    """Return [(word, start, end), ...] for the whole clip, in order."""
    initial_prompt = " ".join(sentences).strip() if sentences else None
    segments, _ = _get_model().transcribe(
        wav_path,
        language="en",
        word_timestamps=True,
        vad_filter=True,                 # skip the silent intro still / outro tail cleanly
        initial_prompt=initial_prompt,   # nudges whisper toward the real vocabulary
        beam_size=5,
    )
    words = []
    for seg in segments:
        for w in (seg.words or []):
            txt = (w.word or "").strip()
            if txt:
                words.append((txt, float(w.start), float(w.end)))
    return words


# ---------------------------------------------------------------- script alignment
_norm_re = re.compile(r"[^a-z0-9]+")
def _norm(tok):
    return _norm_re.sub("", tok.lower())

def _align_to_script(whisper_words, sentences):
    """Keep the SCRIPT's spelling, borrow whisper's timing. difflib maps the two token
    streams; unmatched script tokens get timing interpolated from their neighbours.
    Returns [(display_word, start, end), ...]."""
    script_tokens = []
    for s in sentences:
        for t in s.split():
            t = t.strip()
            if t:
                script_tokens.append(t)
    if not script_tokens or not whisper_words:
        return whisper_words

    s_norm = [_norm(t) for t in script_tokens]
    w_norm = [_norm(w[0]) for w in whisper_words]

    timing = [None] * len(script_tokens)   # (start, end) per script token
    sm = difflib.SequenceMatcher(a=s_norm, b=w_norm, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                ws, we = whisper_words[j1 + k][1], whisper_words[j1 + k][2]
                timing[i1 + k] = (ws, we)
        elif tag == "replace":
            # spread the replaced whisper span across the replaced script tokens
            if j2 > j1 and i2 > i1:
                span_s = whisper_words[j1][1]
                span_e = whisper_words[j2 - 1][2]
                n = i2 - i1
                step = (span_e - span_s) / n if n else 0
                for k in range(n):
                    timing[i1 + k] = (span_s + k * step, span_s + (k + 1) * step)
        # 'delete' (script token, no whisper match) and 'insert' (extra whisper) -> interpolate below

    # interpolate any remaining Nones from surrounding known timings
    known = [(idx, t) for idx, t in enumerate(timing) if t]
    if not known:
        return whisper_words
    # leading gap
    first_idx, first_t = known[0]
    for k in range(first_idx):
        timing[k] = (first_t[0], first_t[0])
    # trailing gap
    last_idx, last_t = known[-1]
    for k in range(last_idx + 1, len(timing)):
        timing[k] = (last_t[1], last_t[1])
    # interior gaps: linear fill between the two bracketing known tokens
    for a in range(len(known) - 1):
        i_a, t_a = known[a]
        i_b, t_b = known[a + 1]
        if i_b - i_a > 1:
            gap = i_b - i_a
            start, end = t_a[1], t_b[0]
            step = (end - start) / gap if gap else 0
            for k in range(1, gap):
                timing[i_a + k] = (start + (k - 1) * step, start + k * step)

    return [(script_tokens[i], timing[i][0], timing[i][1]) for i in range(len(script_tokens))]


# ---------------------------------------------------------------- chunking
_END_RE         = re.compile(r"[.!?…]$")
_STRIP_ALWAYS   = re.compile(r"[;:…]+")                   # never shown
_STRIP_DOTCOMMA = re.compile(r"(?<!\d)[.,]|[.,](?!\d)")   # drop . and , EXCEPT between digits (keep 3.5 / 8,000)

def _clean_display(tok):
    """Remove sentence punctuation from a caption word without corrupting numbers."""
    return _STRIP_DOTCOMMA.sub("", _STRIP_ALWAYS.sub("", tok))

def _group_words(words):
    """Break the word stream into small visible groups (karaoke chunks)."""
    groups, cur, cur_chars = [], [], 0
    for w in words:
        raw = w[0]
        disp = _clean_display(raw)
        disp = disp.upper() if UPPERCASE else disp
        if disp:                                   # skip tokens that were pure punctuation
            add = len(disp) + (1 if cur else 0)
            would_break = (len(cur) >= MAX_WORDS) or (cur and cur_chars + add > MAX_CHARS)
            if would_break:
                groups.append(cur); cur, cur_chars = [], 0
                add = len(disp)
            cur.append((disp, w[1], w[2])); cur_chars += add
        if _END_RE.search(raw.strip()):            # flush on sentence enders (uses raw punctuation)
            if cur:
                groups.append(cur); cur, cur_chars = [], 0
    if cur:
        groups.append(cur)
    return groups


# ---------------------------------------------------------------- .ass / .srt writers
def _ass_color(hexrgb):
    """#RRGGBB -> ASS &HAABBGGRR (opaque)."""
    r, g, b = hexrgb[0:2], hexrgb[2:4], hexrgb[4:6]
    return f"&H00{b}{g}{r}".upper()

def _ts_ass(t):
    if t < 0: t = 0
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"

def _ts_srt(t):
    if t < 0: t = 0
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60); ms = int(round((t - int(t)) * 1000))
    if ms == 1000: s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _build_ass(groups):
    white = _ass_color(COLOR_WHITE_HEX)
    teal  = _ass_color(COLOR_TEAL_HEX)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {_PLAY_W}
PlayResY: {_PLAY_H}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Kar,{FONT_NAME},{FONT_SIZE},{white},{white},&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,{OUTLINE_PX},{SHADOW_PX},{ALIGN},{MARGIN_H},{MARGIN_H},{MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    usable = _PLAY_W - 2 * MARGIN_H
    lines, event_count = [], 0
    for g in groups:
        # autofit: shrink this group's font so the widest single word can never clip the frame edge
        longest = max((len(w[0]) for w in g), default=1)
        word_px = longest * FONT_SIZE * CHAR_W
        fs = FONT_SIZE if word_px <= usable else max(56, int(FONT_SIZE * usable / word_px))
        fs_tag = "" if fs == FONT_SIZE else f"\\fs{fs}"
        # one Dialogue per word: that word teal, the rest white. Position is style-driven
        # (bottom-centre + margins), so libass wraps long groups instead of overflowing.
        for i in range(len(g)):
            start = g[i][1]
            end = g[i + 1][1] if i + 1 < len(g) else g[i][2]   # active until next word starts
            if end <= start:
                end = start + 0.12
            parts = []
            for j, (disp, _s, _e) in enumerate(g):
                col = teal if j == i else white
                parts.append(f"{{\\c{col}&}}{disp}")
            text = " ".join(parts)
            prefix = f"{{{fs_tag}}}" if fs_tag else ""
            lines.append(f"Dialogue: 0,{_ts_ass(start)},{_ts_ass(end)},Kar,,0,0,0,,{prefix}{text}")
            event_count += 1
    return header + "\n".join(lines) + "\n", event_count

def _build_srt(groups):
    """Sidecar for YouTube CC / SEO: group-level, no colour."""
    out = []
    for n, g in enumerate(groups, 1):
        start, end = g[0][1], g[-1][2]
        if end <= start:
            end = start + 0.3
        text = " ".join(w[0] for w in g)
        out.append(f"{n}\n{_ts_srt(start)} --> {_ts_srt(end)}\n{text}\n")
    return "\n".join(out)


# ---------------------------------------------------------------- ffmpeg helpers
def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\n{r.stderr[-800:]}")
    return r.stdout

def _extract_wav(mp4_path, wav_path):
    _run(["ffmpeg", "-y", "-i", mp4_path, "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", wav_path])

def _probe_dur(path):
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path])
    try:
        return round(float(out.strip()), 2)
    except ValueError:
        return None

def _burn(mp4_in, ass_path, mp4_out, work):
    # copy the .ass next to the cwd so the libass filter path stays simple/escaped
    ass_local = os.path.join(work, "subs.ass")
    shutil.copy(ass_path, ass_local)
    _run(["ffmpeg", "-y", "-i", mp4_in,
          "-vf", f"ass={ass_local}",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
          "-pix_fmt", "yuv420p", "-c:a", "copy", mp4_out])


# ---------------------------------------------------------------- endpoint
@router.post("/caption-video")
async def caption_video(req: Request):
    if req.headers.get("x-api-key") != RENDER_API_KEY:
        raise HTTPException(401, "bad api key")
    body = await req.json()

    video_id  = body["video_id"]
    file_id   = body.get("drive_file_id") or body.get("final_file_id")
    if not file_id:
        raise HTTPException(400, "provide drive_file_id (the Final_file_ID of {ID}_FINAL.mp4)")
    sentences = body.get("sentences") or []
    if isinstance(sentences, str):
        # tolerate a single joined string or a JSON-ish array coming through Make
        sentences = [sentences]
    sentences = [s for s in (x.strip() for x in sentences) if s]

    work = tempfile.mkdtemp()
    try:
        mp4_in  = os.path.join(work, "in.mp4")
        wav     = os.path.join(work, "audio.wav")
        ass     = os.path.join(work, "subs_src.ass")
        mp4_out = os.path.join(work, f"{video_id}_FINAL_SUB.mp4")

        _drive_download(file_id, mp4_in)
        _extract_wav(mp4_in, wav)

        words = _transcribe_words(wav, sentences)
        if not words:
            raise HTTPException(422, "no speech detected in the final audio — nothing to caption")

        used_script = bool(sentences)
        if used_script:
            words = _align_to_script(words, sentences)

        # Suppress captions over the fixed intro/outro clips (no captions on the end clip).
        head = float(body.get("intro_head_s", INTRO_HEAD_S))
        tail = float(body.get("outro_tail_s", OUTRO_TAIL_S))
        if head > 0 or tail > 0:
            dur = _probe_dur(mp4_in) or 0
            hi = (dur - tail) if (dur and tail > 0) else float("inf")
            words = [w for w in words if w[1] >= head and w[1] < hi]
            if not words:
                raise HTTPException(422, "all speech fell inside the intro/outro suppression window")

        groups = _group_words(words)
        ass_text, event_count = _build_ass(groups)
        srt_text = _build_srt(groups)
        with open(ass, "w") as f:
            f.write(ass_text)

        _burn(mp4_in, ass, mp4_out, work)

        with open(mp4_out, "rb") as f:
            mp4_b64 = base64.b64encode(f.read()).decode()
        srt_b64 = base64.b64encode(srt_text.encode("utf-8")).decode()

        return {
            "status": "ok",
            "video_id": video_id,
            "captioned_filename": f"{video_id}_FINAL_SUB.mp4",
            "captioned_base64": mp4_b64,
            "srt_filename": f"{video_id}.srt",
            "srt_base64": srt_b64,
            "srt": srt_text,
            "event_count": event_count,
            "group_count": len(groups),
            "duration_s": _probe_dur(mp4_out),
            "used_script": used_script,
            "engine": f"faster-whisper {WHISPER_MODEL}",
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
