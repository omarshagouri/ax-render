"""AX — /finish-video endpoint (FastAPI, part of ax-render).

The NEW ax-video renderer outputs ONE {ID}_render.mp4 (cards + narration, audio
baked in). This endpoint does NOT re-stitch beats. It only WRAPS that finished
body with a 1.0s silent intro still (the thumbnail) and the outro clip, then
returns the result. Captions are burned in a second step via /caption-video.

Register in app.py, right after the other include_router lines:

    from finish_endpoint import router as finish_router
    app.include_router(finish_router)

POST /finish-video   (header x-api-key)
  {
    "video_id":            "AX-001-SF",
    "out_folder_id":       "<Drive folder>",          # informational
    "body_video_file_id":  "<Drive ID of {ID}_render.mp4>",   # REQUIRED
    "intro_photo_file_id": "<Thumbnail_ID>",          # optional -> 1s silent still
    "outro_video_file_id": "<End_clip_ID>"            # optional -> appended outro
  }
  -> { "status":"ok", "filename":"{ID}_FINAL.mp4", "file_base64":"...", ... }
"""
import os, base64, asyncio, tempfile, shutil
from fastapi import APIRouter, Request, HTTPException
from google.auth import default as gauth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Reuse the proven ffmpeg helpers from the existing assembler.
from assemble import norm_photo, norm_audio, norm_outro_av, probe_dur, INTRO_DUR, _run

router = APIRouter()
RENDER_API_KEY = os.environ["RENDER_API_KEY"]
NAVY = "0x0A1628"  # brand navy letterbox for any non-9:16 segment


def _drive():
    creds, _ = gauth_default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _download(svc, file_id, dst):
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dst, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dst


def finish(body_path, intro_photo, outro_video, work, out_path):
    """Concatenate [intro?][body][outro?] as self-contained A/V segments.
    Every input is normalized to 1080x1920@30 / yuv420p / 44100 stereo INSIDE
    the filter graph, so a codec/SAR/fps mismatch can't crash the concat."""
    parts = []

    # --- intro: 1.0s silent still from the thumbnail (own silent audio) ---
    if intro_photo:
        iv = norm_photo(intro_photo, INTRO_DUR, work, 900)                    # silent 1s video
        ia = norm_audio(None, work, 900, silence_dur=INTRO_DUR)              # 1s AAC silence
        intro = os.path.join(work, "intro_av.mp4")
        _run(["ffmpeg", "-y", "-i", iv, "-i", ia,
              "-map", "0:v:0", "-map", "1:a:0",
              "-c:v", "libx264", "-preset", "veryfast", "-r", "30",
              "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k", "-shortest", intro])
        parts.append(intro)

    # --- body: the rendered cards + narration (audio already baked in) ---
    parts.append(body_path)

    # --- outro: end clip, keeps its own audio ---
    if outro_video:
        parts.append(norm_outro_av(outro_video, work))

    # --- concat via FILTER with per-input normalization (crash-proof) ---
    inputs, fc, labels = [], [], ""
    for i, p in enumerate(parts):
        inputs += ["-i", p]
        fc.append(
            f"[{i}:v:0]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color={NAVY},"
            f"setsar=1,fps=30,format=yuv420p[v{i}]"
        )
        fc.append(f"[{i}:a:0]aformat=sample_rates=44100:channel_layouts=stereo[a{i}]")
        labels += f"[v{i}][a{i}]"
    n = len(parts)
    fc.append(f"{labels}concat=n={n}:v=1:a=1[v][a]")

    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fc),
          "-map", "[v]", "-map", "[a]",
          "-c:v", "libx264", "-preset", "veryfast", "-r", "30",
          "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
          "-movflags", "+faststart", out_path])

    return {"final_dur": round(probe_dur(out_path), 3),
            "intro": bool(intro_photo), "outro": bool(outro_video)}


def _build(video_id, body_id, intro_id, outro_id):
    work = tempfile.mkdtemp()
    try:
        svc = _drive()
        body = _download(svc, body_id, os.path.join(work, f"{video_id}_render.mp4"))
        intro = (_download(svc, intro_id, os.path.join(work, f"{video_id}_thumb"))
                 if (intro_id or "").strip() else None)
        outro = (_download(svc, outro_id, os.path.join(work, f"{video_id}_outro.mp4"))
                 if (outro_id or "").strip() else None)
        out = os.path.join(work, f"{video_id}_FINAL.mp4")
        info = finish(body, intro, outro, work, out)
        with open(out, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"{video_id}_FINAL.mp4", b64, info
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.post("/finish-video")
async def finish_video(req: Request):
    if req.headers.get("x-api-key") != RENDER_API_KEY:
        raise HTTPException(401, "bad api key")
    b = await req.json()
    if not (b.get("body_video_file_id") or "").strip():
        raise HTTPException(400, "need body_video_file_id")
    loop = asyncio.get_event_loop()
    filename, b64, info = await loop.run_in_executor(
        None, _build,
        b["video_id"], b["body_video_file_id"],
        b.get("intro_photo_file_id"), b.get("outro_video_file_id"))
    return {"status": "ok", "filename": filename, "file_base64": b64, **info}
