# AmpCoreX render service — deploy to Cloud Run

## Files in this folder
- `app.py`            — the FastAPI service (POST /render-beat)
- `requirements.txt`  — Python deps (playwright pinned to 1.50.0)
- `Dockerfile`        — official Playwright image + ffmpeg
- `background.png`    — ADD THIS YOURSELF: the 1080x1920 navy+X plate (bake it in)

## One-time Google setup
1. Create/choose a Google Cloud project; enable **Cloud Run**, **Cloud Build**,
   **Artifact Registry**, and **Google Drive API**.
2. Create a dedicated **service account** (e.g. `ampcorex-render@PROJECT.iam.gserviceaccount.com`).
3. In Google Drive, **share your video output parent folder** (the one the pipeline writes into)
   with that service-account email, as **Editor**. Sharing inherits to subfolders, so the
   per-video folders are covered. (This is how the service can write clips without a key file.)

## Deploy (from this folder, with gcloud installed & logged in)
```
gcloud run deploy ampcorex-render \
  --source . \
  --region europe-west1 \
  --service-account ampcorex-render@PROJECT.iam.gserviceaccount.com \
  --memory 2Gi --cpu 2 --concurrency 1 --timeout 300 \
  --no-allow-unauthenticated \
  --set-env-vars GITHUB_TOKEN=github_pat_xxx,RENDER_API_KEY=your_long_secret
```
- `--memory 2Gi` — Chromium needs ≥2GB. `--concurrency 1` — one render per instance (safe on one browser).
- `--no-allow-unauthenticated` + the `x-api-key` header = two layers of protection. (If Make can't
  send a Google identity token, use `--allow-unauthenticated` and rely on the `RENDER_API_KEY` header.)
- Deploy prints a **Service URL** like `https://ampcorex-render-xxxx.a.run.app`.

## Wire into Make
- Blueprint module 3 `url` = `<Service URL>/render-beat`
- Blueprint module 3 header `x-api-key` = the same `RENDER_API_KEY` you set above

## Test it directly (before Make)
```
curl -X POST https://ampcorex-render-xxxx.a.run.app/render-beat \
  -H "Content-Type: application/json" -H "x-api-key: your_long_secret" \
  -d '{"video_id":"AX-TEST-SF","beat":"1","card_id":"VC-SF-001",
       "values":{"KICKER":"State of Health","NUM":"92","PCT":"%"},
       "duration":"3s","out_folder_id":"<a drive folder shared with the service account>"}'
```
Expect: `{"status":"ok","file_id":"...","filename":"AX-TEST-SF_beat_01_VC-SF-001.mp4"}`

## Cost
Cloud Run scales to zero — you pay only while a render runs. At a few videos/week this very
likely stays in the free tier. Set a **budget alert** so it can never surprise-charge you.

## Notes / hardening later
- Fonts load from Google Fonts CDN at render time (Cloud Run has internet). To remove that
  dependency, bake the .ttf files into the image and @font-face them locally.
- First request after idle is a cold start (container + browser spin-up, ~20-40s). The Make
  blueprint's 120s HTTP timeout covers this.
