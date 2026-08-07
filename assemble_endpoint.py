"""AmpCoreX Agent 6 — /assemble-video endpoint (FastAPI, matches ax-render's app.py).

Register it in app.py with TWO lines, right after `app = FastAPI()`:

    from assemble_endpoint import router
    app.include_router(router)

Add to requirements.txt:  google-api-python-client   google-auth
(ffmpeg is already in the image; `requests`, base64, tempfile already imported by the service.)

Auth: uses the Cloud Run runtime service account for Drive READ. That SA
(ax-render@ampcorex.iam.gserviceaccount.com) needs at least Viewer on the
"Agents Video" folder tree. Reads use no storage quota, so no upload trick needed here —
the final MP4 comes back as base64 and Make saves it, exactly like /render-beat.

Contract (Make -> service), same x-api-key header as /render-beat:
  POST /assemble-video
  {
    "video_id": "AX-020-SF",
    "out_folder_id": "<Scripted col F>",               # folder holding the beat clips
    "beats":    [ {"beat":1,"card_id":"VC-SF-004","sentence":"UK electric car..."}, ... ],
    "chapters": [ {"chapter":1,"mp3_file_id":"17rJ...","text":"UK electric car..."}, ... ]
  }
Response: { "status":"ok", "filename":"AX-020-SF_FINAL.mp4", "file_base64":"<...>", ... }
"""
import os, io, base64, tempfile, shutil, re, asyncio
from fastapi import APIRouter, Request, HTTPException
from google.auth import default as gauth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from assemble import assemble          # the validated ffmpeg core (assemble.py)

router = APIRouter()
RENDER_API_KEY = os.environ["RENDER_API_KEY"]     # same key as /render-beat

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

def _list_clips(svc, folder_id):
    out, tok = {}, None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType contains 'video/'",
            fields="nextPageToken, files(id,name)", pageSize=1000,
            includeItemsFromAllDrives=True, supportsAllDrives=True, pageToken=tok).execute()
        for f in resp.get("files", []):
            out[f["name"]] = f["id"]
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return out

def _norm(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower()

def _chapter_for(sentence, chapters):
    """A beat's sentence is a substring of its parent chapter's text. Empty sentence = sting."""
    s = _norm(sentence)
    if not s:
        return None
    for c in chapters:
        if s in _norm(c["text"]):
            return c["chapter"]
    return None      # caller falls back to previous beat's chapter

def _build(video_id, folder_id, beats_in, chapters_in):
    """Blocking: Drive downloads + ffmpeg assembly. Runs in a threadpool."""
    work = tempfile.mkdtemp()
    try:
        svc = _drive()
        clip_index = _list_clips(svc, folder_id)

        m_beats, last_ch = [], None
        for b in beats_in:
            ch = _chapter_for(b.get("sentence"), chapters_in)
            if ch is None and _norm(b.get("sentence")):    # non-empty but unmatched -> inherit
                ch = last_ch
            if ch is not None:
                last_ch = ch
            nn = f"{int(b['beat']):02d}"
            fname = f"{video_id}_beat_{nn}_{b['card_id'].strip()}.mp4"
            fid = clip_index.get(fname)
            if not fid:
                raise HTTPException(422, f"clip not found in folder: {fname} "
                                         f"(have: {sorted(clip_index)[:20]})")
            clip = _download(svc, fid, os.path.join(work, fname))
            m_beats.append({"beat": int(b["beat"]), "clip": clip, "chapter": ch})

        m_chaps = []
        for c in chapters_in:
            mp3 = _download(svc, c["mp3_file_id"], os.path.join(work, f"ch_{c['chapter']}.mp3"))
            m_chaps.append({"chapter": int(c["chapter"]), "mp3": mp3})

        out_path = os.path.join(work, f"{video_id}_FINAL.mp4")
        info = assemble({"beats": m_beats, "chapters": m_chaps}, work, out_path)
        with open(out_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"{video_id}_FINAL.mp4", b64, info
    finally:
        shutil.rmtree(work, ignore_errors=True)

@router.post("/assemble-video")
async def assemble_video(req: Request):
    if req.headers.get("x-api-key") != RENDER_API_KEY:
        raise HTTPException(401, "bad api key")
    body = await req.json()
    loop = asyncio.get_event_loop()
    filename, b64, info = await loop.run_in_executor(
        None, _build, body["video_id"], body["out_folder_id"], body["beats"], body["chapters"])
    return {"status": "ok", "filename": filename, "file_base64": b64, **info}
