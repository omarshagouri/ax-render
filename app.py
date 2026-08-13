# ============================================================
# AmpCoreX render service  —  Cloud Run
# Endpoints:
#   POST /render-beat      → renders one card to MP4 (base64)
#   POST /thumbnail        → composites one card to a still PNG (base64)
#   POST /assemble-video   → (assemble_endpoint router)
# Make orchestrates. Drive WRITES go through Make (base64 return).
# Drive READS (thumbnail background/logo by file id) are done here via
# the service account — reads need no quota, only writes were blocked.
# ============================================================
import os, io, json, base64, asyncio, subprocess, tempfile, shutil
import requests
from fastapi import FastAPI, Request, HTTPException
from playwright.async_api import async_playwright

# ---- config (env vars set at deploy) ----
GH_USER   = os.environ.get("GH_USER", "omarshagouri")
GH_REPO   = os.environ.get("GH_REPO", "ax-cards")
GH_BRANCH = os.environ.get("GH_BRANCH", "main")
CARDS_DIR = os.environ.get("CARDS_DIR", "Cards")
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]       # required
RENDER_API_KEY = os.environ["RENDER_API_KEY"]     # required; must match Make's x-api-key

app = FastAPI()

from assemble_endpoint import router
app.include_router(router)

_pw = None
_browser = None
_render_lock = asyncio.Lock()      # one render at a time per instance (safe on one browser)
_card_cache = {}                   # card_id -> CARD dict
_drive = None                      # lazy Drive client (read-only), built on first /thumbnail call
BG_URI = None
LOGO_URI = None                    # optional baked-in logo.png (fallback for /thumbnail)

# ---- the shared frame (identical to the Colab engine) ----
FRAME = """
<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>
  html,body{ margin:0; padding:0; }
  #stage{ width:1080px; height:1920px; position:relative; overflow:hidden;
    background:#0A1628 url('__BG__') center/cover no-repeat;
    font-family:'Space Grotesk', sans-serif; }
  __CARD_CSS__
</style></head>
<body>
  <div id="stage">__CARD_BODY__</div>
<script>
  function clamp(x){ return Math.max(0, Math.min(1, x)); }
  function easeOutCubic(p){ return 1 - Math.pow(1 - p, 3); }
  window.seek = function(t){ __CARD_SEEK__ };
  window.seek(0);
</script></body></html>
"""

# ---- thumbnail frame: parameterized size + per-video AI background + optional logo,
#      seeked to a settled state (all clamp-based entrances saturated). ----
THUMB_FRAME = """
<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>
  html,body{ margin:0; padding:0; }
  #stage{ width:__W__px; height:__H__px; position:relative; overflow:hidden;
    background:#0A1628 url('__BG__') center/cover no-repeat;
    font-family:'Space Grotesk', sans-serif; }
  __CARD_CSS__
</style></head>
<body>
  <div id="stage">__CARD_BODY__</div>
<script>
  function clamp(x){ return Math.max(0, Math.min(1, x)); }
  function easeOutCubic(p){ return 1 - Math.pow(1 - p, 3); }
  window.seek = function(t){ __CARD_SEEK__ };
  window.seek(999);   // still image: settle every entrance
</script></body></html>
"""
html = (THUMB_FRAME.replace("__CARD_CSS__", card["css"])
                       .replace("__CARD_BODY__", card["body"])
                       .replace("__CARD_SEEK__", card.get("seek", ""))
                       .replace("__BG_SRC__", bg_uri or "")
                       .replace("__LOGO_SRC__", logo_uri or "")
                       .replace("__BG__", bg_uri or "")
                       .replace("__LOGO__", logo_uri or "")
                       .replace("__W__", str(width))
                       .replace("__H__", str(height)))

@app.on_event("startup")
async def startup():
    """Launch Chromium once (warm), load the baked-in background and optional logo."""
    global _pw, _browser, BG_URI, LOGO_URI
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

    with open("background.png", "rb") as f:
        BG_URI = "data:image/png;base64," + base64.b64encode(f.read()).decode()

    # optional fixed logo baked into the image; used by /thumbnail if no logo_file_id is sent
    if os.path.exists("logo.png"):
        with open("logo.png", "rb") as f:
            LOGO_URI = "data:image/png;base64," + base64.b64encode(f.read()).decode()

    print("startup complete: browser warm, background loaded, logo=%s" % bool(LOGO_URI))

@app.on_event("shutdown")
async def shutdown():
    if _browser: await _browser.close()
    if _pw: await _pw.stop()

# ---- Drive (read-only) ----------------------------------------------------
def _drive_client():
    """Build a read-only Drive client from the Cloud Run service account (ADC).
    Reads need no storage quota, so this is safe for the SA that cannot write."""
    global _drive
    if _drive is None:
        from google.auth import default as google_auth_default
        from googleapiclient.discovery import build
        creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
        _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive

def drive_fetch_data_uri(file_id, fallback_mime="image/png"):
    """Download a Drive file by id and return it as a data: URI for inline embedding."""
    from googleapiclient.http import MediaIoBaseDownload
    drive = _drive_client()
    meta = drive.files().get(fileId=file_id, fields="mimeType",
                             supportsAllDrives=True).execute()
    mime = meta.get("mimeType") or fallback_mime
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return "data:%s;base64,%s" % (mime, base64.b64encode(buf.getvalue()).decode())

# ---- cards ----------------------------------------------------------------
def load_card(card_id):
    if card_id in _card_cache:
        return _card_cache[card_id]
    url = f"https://raw.githubusercontent.com/{GH_USER}/{GH_REPO}/{GH_BRANCH}/{CARDS_DIR}/{card_id}.py"
    r = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=30)
    if r.status_code == 404:
        raise HTTPException(404, f"{card_id}.py not found in repo")
    if r.status_code in (401, 403):
        raise HTTPException(500, "GitHub token rejected")
    r.raise_for_status()
    ns = {}
    exec(r.text, ns)
    _card_cache[card_id] = ns["CARD"]
    return ns["CARD"]

def parse_duration(text, default):
    text = (str(text or "")).strip().lower().replace("s", "")
    return float(text) if text else float(default)

async def render_card(card, values, duration, out_path, fps=30):
    html = (FRAME.replace("__CARD_CSS__", card["css"])
                 .replace("__CARD_BODY__", card["body"])
                 .replace("__CARD_SEEK__", card["seek"])
                 .replace("__BG__", BG_URI))
    missing = [s for s in card["slots"] if s not in values]
    if missing:
        raise HTTPException(400, f"{card['id']} missing slot values: {missing}")
    for name, text in values.items():
        html = html.replace(f"__{name}__", str(text))

    frames = tempfile.mkdtemp()
    try:
        total = round(fps * duration)
        context = await _browser.new_context(viewport={"width": 1080, "height": 1920})
        page = await context.new_page()
        await page.set_content(html)
        await page.evaluate("() => document.fonts.ready")
        for i in range(total):
            await page.evaluate(f"window.seek({i/fps})")
            await page.screenshot(path=f"{frames}/frame_{i:04d}.png")
        await context.close()
        subprocess.run(
            f"ffmpeg -y -framerate {fps} -i {frames}/frame_%04d.png "
            f"-c:v libx264 -pix_fmt yuv420p -r {fps} {out_path}",
            shell=True, check=True, capture_output=True)
    finally:
        shutil.rmtree(frames, ignore_errors=True)

async def render_thumbnail(card, values, bg_uri, logo_uri, width, height, out_path):
    """Composite one card to a single settled PNG. Same slot/exec engine as render_card,
    but no ffmpeg: seek to a large t so entrances saturate, then screenshot #stage."""
    html = (THUMB_FRAME.replace("__CARD_CSS__", card["css"])
                       .replace("__CARD_BODY__", card["body"])
                       .replace("__CARD_SEEK__", card.get("seek", ""))
                       .replace("__BG__", bg_uri or "")
                       .replace("__LOGO__", logo_uri or "")
                       .replace("__W__", str(width))
                       .replace("__H__", str(height)))
    missing = [s for s in card["slots"] if s not in values]
    if missing:
        raise HTTPException(400, f"{card['id']} missing slot values: {missing}")
    for name, text in values.items():
        html = html.replace(f"__{name}__", str(text))

    context = await _browser.new_context(viewport={"width": width, "height": height})
    page = await context.new_page()
    try:
        await page.set_content(html)
        await page.evaluate("() => document.fonts.ready")
        await page.evaluate("window.seek(999)")
        await page.locator("#stage").screenshot(path=out_path)   # exact stage bounds, PNG
    finally:
        await context.close()

@app.get("/")
def health():
    return {"status": "ok", "service": "ampcorex-render"}

@app.post("/render-beat")
async def render_beat(req: Request):
    if req.headers.get("x-api-key") != RENDER_API_KEY:
        raise HTTPException(401, "bad api key")
    body = await req.json()

    video_id = body["video_id"]
    beat     = str(body["beat"])
    card_id  = body["card_id"]
    values   = body["values"]
    if isinstance(values, str):           # tolerate values arriving as a JSON string
        values = json.loads(values)
    _ = body.get("out_folder_id")  # no longer used by the service; Make handles Drive

    card = load_card(card_id)
    duration = parse_duration(body.get("duration"), card["default_duration"])

    filename = f"{video_id}_beat_{beat.zfill(2)}_{card_id}.mp4"
    out_path = f"/tmp/{filename}"

    async with _render_lock:
        await render_card(card, values, duration, out_path)

    with open(out_path, "rb") as f:
        mp4_b64 = base64.b64encode(f.read()).decode()
    os.remove(out_path)

    # Make decodes 'file_base64' and saves it to Drive with its own connection.
    return {"status": "ok", "filename": filename, "duration": duration, "file_base64": mp4_b64}

@app.post("/thumbnail")
async def thumbnail(req: Request):
    """Composite a thumbnail/end-plate PNG from a card + per-video AI background (+ optional logo).
    Body:
      card_id             (required)  thumbnail card id in ax-cards
      values              (required)  slot dict, e.g. {"HEADLINE":"...","SUBHEAD":"..."}
      background_file_id  (required)  Drive id of the AI background Make generated for this video
      logo_file_id        (optional)  Drive id of the logo; falls back to baked-in logo.png, else none
      width / height      (optional)  default 1080 x 1920
      output_name         (optional)  default "{card_id}_thumb.png"
    Returns: {status, filename, file_base64}  → Make saves it to Drive with its own connection.
    """
    if req.headers.get("x-api-key") != RENDER_API_KEY:
        raise HTTPException(401, "bad api key")
    body = await req.json()

    card_id = body["card_id"]
    values  = body.get("values", {})
    if isinstance(values, str):
        values = json.loads(values)

    bg_file_id = body.get("background_file_id")
    if not bg_file_id:
        raise HTTPException(400, "background_file_id required")
    try:
        bg_uri = drive_fetch_data_uri(bg_file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"could not read background {bg_file_id} from Drive: {e}")

    logo_file_id = body.get("logo_file_id")
    if logo_file_id:
        try:
            logo_uri = drive_fetch_data_uri(logo_file_id)
        except Exception as e:
            raise HTTPException(502, f"could not read logo {logo_file_id} from Drive: {e}")
    else:
        logo_uri = LOGO_URI   # baked-in fallback, or None if the card draws its own mark

    width  = int(body.get("width", 1080))
    height = int(body.get("height", 1920))

    output_name = body.get("output_name") or f"{card_id}_thumb.png"
    if not output_name.lower().endswith(".png"):
        output_name += ".png"
    out_path = f"/tmp/{output_name}"

    card = load_card(card_id)

    async with _render_lock:
        await render_thumbnail(card, values, bg_uri, logo_uri, width, height, out_path)

    with open(out_path, "rb") as f:
        png_b64 = base64.b64encode(f.read()).decode()
    os.remove(out_path)

    return {"status": "ok", "filename": output_name, "file_base64": png_b64}
