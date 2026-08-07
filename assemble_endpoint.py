"""AmpCoreX Agent 6 — /assemble-video endpoint (self-contained Flask blueprint).
Register in ax-render's app.py with:
    from assemble_endpoint import bp
    app.register_blueprint(bp)
Requires: flask, google-api-python-client, google-auth  (+ ffmpeg, already in image)
Auth: uses the Cloud Run runtime service account (ax-render@ampcorex...) for Drive READ.
That SA must have at least Viewer on the "Agents Video" folder tree.

Contract (Make -> service):
  POST /assemble-video   header x-api-key: <RENDER_API_KEY>
  {
    "video_id": "AX-020-SF",
    "out_folder_id": "<Scripted col F>",              # folder holding the beat clips + MP3s
    "beats":    [ {"beat":1,"card_id":"VC-SF-004","sentence":"UK electric car..."}, ... ],
    "chapters": [ {"chapter":1,"mp3_file_id":"17rJ...","text":"UK electric car..."}, ... ]
  }
Response: { "filename":"AX-020-SF_FINAL.mp4", "file_base64":"<...>" }   # Make saves it to Drive
"""
import os, io, base64, tempfile, shutil, re
from flask import Blueprint, request, jsonify
from google.auth import default as gauth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from assemble import assemble   # the validated ffmpeg core

bp = Blueprint("assemble", __name__)
API_KEY = os.environ.get("RENDER_API_KEY", "")

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
    """Map filename -> file_id for every mp4 in the output folder (handles paging)."""
    out, tok = {}, None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType contains 'video/'",
            fields="nextPageToken, files(id,name)", pageSize=1000,
            includeItemsFromAllDrives=True, supportsAllDrives=True, pageToken=tok).execute()
        for f in resp.get("files", []): out[f["name"]] = f["id"]
        tok = resp.get("nextPageToken")
        if not tok: break
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
    return None  # caller falls back to previous beat's chapter

@bp.route("/assemble-video", methods=["POST"])
def assemble_video():
    if request.headers.get("x-api-key", "") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    video_id   = data["video_id"]
    folder_id  = data["out_folder_id"]
    beats_in   = data["beats"]
    chapters_in= data["chapters"]

    work = tempfile.mkdtemp()
    try:
        svc = _drive()
        clip_index = _list_clips(svc, folder_id)

        # resolve each beat -> chapter (with previous-beat fallback) + its clip file
        m_beats, last_ch = [], None
        for b in beats_in:
            ch = _chapter_for(b.get("sentence"), chapters_in)
            if ch is None and _norm(b.get("sentence")):   # non-empty but unmatched -> inherit
                ch = last_ch
            if ch is not None:
                last_ch = ch
            nn = f"{int(b['beat']):02d}"
            fname = f"{video_id}_beat_{nn}_{b['card_id'].strip()}.mp4"
            fid = clip_index.get(fname)
            if not fid:
                return jsonify({"error": f"clip not found in folder: {fname}",
                                "available": sorted(clip_index)[:20]}), 422
            clip = _download(svc, fid, os.path.join(work, fname))
            m_beats.append({"beat": int(b["beat"]), "clip": clip, "chapter": ch})

        # download chapter MP3s
        m_chaps = []
        for c in chapters_in:
            mp3 = _download(svc, c["mp3_file_id"], os.path.join(work, f"ch_{c['chapter']}.mp3"))
            m_chaps.append({"chapter": int(c["chapter"]), "mp3": mp3})

        out_path = os.path.join(work, f"{video_id}_FINAL.mp4")
        info = assemble({"beats": m_beats, "chapters": m_chaps}, work, out_path)

        with open(out_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return jsonify({"filename": f"{video_id}_FINAL.mp4", "file_base64": b64, **info})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(work, ignore_errors=True)
