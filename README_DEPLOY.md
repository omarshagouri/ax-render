

## Notes / hardening later
- Fonts load from Google Fonts CDN at render time (Cloud Run has internet). To remove that
  dependency, bake the .ttf files into the image and @font-face them locally.
- First request after idle is a cold start (container + browser spin-up, ~20-40s). The Make
  blueprint's 120s HTTP timeout covers this.
