"""AmpCoreX — /plan-cadence endpoint (FastAPI, matches ax-render's app.py).

Register in app.py with two lines after `app = FastAPI()`:
    from cadence_endpoint import router as cadence_router
    app.include_router(cadence_router)

Purpose: turn REAL measured chapter audio into a cadence beat skeleton (<=CAP
per cell) BEFORE the Visual Plan agent runs. Make calls this, injects the
returned `beats[]` into the Agent 6 prompt; Agent 6 fills card_id + values per
cell and must not touch durations or cell count. This removes LLM/byte-estimate
durations as the planning input and makes the 3-4s retention cadence an
invariant (which also removes the assembly freeze as a byproduct).

Auth: same `x-api-key` header as /render-beat and /assemble-video.
Drive READ via the Cloud Run runtime service account (drive.readonly), same as
the assembly endpoint.

Contract (Make -> service):
  POST /plan-cadence
  {
    "video_id": "AX-020-SF",
    "target": 3.0, "cap": 4.0, "floor": 1.5,        # optional; these are the defaults
    "chapters": [
      {"chapter":1, "mp3_file_id":"17rJ...", "sentences":["...","..."]},
      ...
    ]
  }
Response:
  {
    "status":"ok", "video_id":"AX-020-SF",
    "target":3.0,"cap":4.0,"floor":1.5,
    "beats":[ {"beat":1,"chapter":1,"cell_in_chapter":1,"cells_in_chapter":6,
               "duration":3.0,"sentence":"...","from_sentence":[0]}, ... ],
    "chapters":[ {"chapter":1,"real_audio":18.0,"cells":6,"cell_dur":3.0}, ... ],
    "total_dur": 71.7, "beat_count": 24
  }
"""
import os, io, tempfile, shutil, asyncio
from fastapi import APIRouter, Request, HTTPException
from google.auth import default as gauth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from assemble import probe_dur          # reuse the validated ffprobe helper
import cadence as C

router = APIRouter()
RENDER_API_KEY = os.environ["RENDER_API_KEY"]


def _drive():
    creds, _ = gauth_default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _download(svc, file_id, dst):
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(dst, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dst


def _build(video_id, chapters_in, target, cap, floor):
    """Blocking: download each chapter MP3, ffprobe its real duration, plan cells."""
    work = tempfile.mkdtemp()
    try:
        svc = _drive()
        chapters = []
        for c in chapters_in:
            fid = (c.get("mp3_file_id") or "").strip()
            if not fid:
                raise HTTPException(422, f"chapter {c.get('chapter')} has no mp3_file_id")
            mp3 = _download(svc, fid, os.path.join(work, f"ch_{c['chapter']}.mp3"))
            sentences = c.get("sentences") or []
            if isinstance(sentences, str):        # tolerate a single blob split on newlines
                sentences = [s for s in sentences.splitlines() if s.strip()]
            chapters.append({"chapter": int(c["chapter"]),
                             "real_audio": probe_dur(mp3),
                             "sentences": sentences})

        chapters.sort(key=lambda x: x["chapter"])
        beats, summary = C.plan_video(chapters, target, cap, floor)
        total = round(sum(b["duration"] for b in beats), 3)
        return beats, summary, total
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.post("/plan-cadence")
async def plan_cadence(req: Request):
    if req.headers.get("x-api-key") != RENDER_API_KEY:
        raise HTTPException(401, "bad api key")
    body = await req.json()

    video_id = body["video_id"]
    target   = float(body.get("target", C.TARGET))
    cap      = float(body.get("cap", C.CAP))
    floor    = float(body.get("floor", C.FLOOR))
    chapters_in = body["chapters"]
    if not isinstance(chapters_in, list) or not chapters_in:
        raise HTTPException(422, "chapters must be a non-empty array")

    loop = asyncio.get_event_loop()
    beats, summary, total = await loop.run_in_executor(
        None, _build, video_id, chapters_in, target, cap, floor)

    return {"status": "ok", "video_id": video_id,
            "target": target, "cap": cap, "floor": floor,
            "beats": beats, "chapters": summary,
            "total_dur": total, "beat_count": len(beats)}
