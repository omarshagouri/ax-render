# ============================================================
# AmpCoreX render service  —  Cloud Run
# One endpoint: POST /render-beat  → renders one card to MP4,
# uploads it to Drive, returns the file id. Make orchestrates;
# this service only renders. No Sheets access needed.
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
BG_URI = None

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

@app.on_event("startup")
async def startup():
    """Launch Chromium once (warm), load the baked-in background, build the Drive client."""
    global _pw, _browser, BG_URI
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

    with open("background.png", "rb") as f:
        BG_URI = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    print("startup complete: browser warm, background loaded")

@app.on_event("shutdown")
async def shutdown():
    if _browser: await _browser.close()
    if _pw: await _pw.stop()

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
