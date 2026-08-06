#!/usr/bin/env python3
"""AmpCoreX Agent 6 assembly core (transport-agnostic).
Per-chapter audio-master + proportional tail-freeze. Pure ffmpeg.
Validated locally before wrapping in the Cloud Run endpoint."""
import json, os, subprocess, tempfile, shutil

def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\n{r.stderr[-800:]}")
    return r.stdout

def probe_dur(path):
    out = _run(["ffprobe","-v","error","-show_entries","format=duration",
                "-of","default=nw=1:nk=1", path])
    return float(out.strip())

def norm_beat(src, target, work, idx):
    """Re-encode a silent beat clip to exactly `target` seconds, 1080x1920@30, yuv420p.
    Pads by freezing the last frame (tpad) when target>src; trims when target<src."""
    d = probe_dur(src); dst = os.path.join(work, f"v_{idx:03d}.mp4")
    pad = round(target - d, 3)
    vf = "scale=1080:1920:force_original_aspect_ratio=decrease,"\
         "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"
    if pad > 0.02:
        vf += f",tpad=stop_mode=clone:stop_duration={pad}"
        _run(["ffmpeg","-y","-i",src,"-vf",vf,"-an","-c:v","libx264",
              "-preset","veryfast","-r","30",dst])
    else:  # target <= src (or equal): hard-cut to target
        _run(["ffmpeg","-y","-i",src,"-t",f"{target:.3f}","-vf",vf,"-an",
              "-c:v","libx264","-preset","veryfast","-r","30",dst])
    return dst

def norm_audio(src, work, idx, silence_dur=None):
    """Decode any MP3 (or make silence) to a normalized AAC 44100 stereo m4a."""
    dst = os.path.join(work, f"a_{idx:03d}.m4a")
    if silence_dur is not None:
        _run(["ffmpeg","-y","-f","lavfi","-i","anullsrc=r=44100:cl=stereo",
              "-t",f"{silence_dur:.3f}","-c:a","aac","-b:a","160k",dst])
    else:
        _run(["ffmpeg","-y","-i",src,"-ar","44100","-ac","2",
              "-c:a","aac","-b:a","160k",dst])
    return dst

def concat(parts, work, name, copy=True):
    lst = os.path.join(work, name+".txt")
    with open(lst,"w") as f:
        for p in parts: f.write(f"file '{os.path.abspath(p)}'\n")
    out = os.path.join(work, name)
    args = ["ffmpeg","-y","-f","concat","-safe","0","-i",lst]
    args += (["-c","copy"] if copy else ["-c:v","libx264","-r","30"])
    _run(args + [out])
    return out

def assemble(manifest, work, out_path):
    """manifest = {
        'beats':   [{'beat':1,'clip':<path>,'chapter':1}, ... 'chapter':None for sting],
        'chapters':[{'chapter':1,'mp3':<path>}, ...]   # in order
    }"""
    beats = manifest["beats"]
    ch_mp3 = {c["chapter"]: c["mp3"] for c in manifest["chapters"]}
    # real audio durations
    ch_audio = {c["chapter"]: probe_dur(c["mp3"]) for c in manifest["chapters"]}
    # group beats by chapter, preserve order
    from collections import defaultdict, OrderedDict
    groups = OrderedDict()
    for b in beats:
        groups.setdefault(b["chapter"], []).append(b)

    v_parts, a_parts, ai = [], [], 0
    timeline = []  # (segment_kind, chapter_or_None)
    # Build in BEAT order, but audio is attached per chapter as we first hit it.
    seen_ch = set()
    vi = 0
    # Precompute per-beat targets per chapter
    beat_target = {}
    for ch, bs in groups.items():
        if ch is None:            # sting beats: native duration, silence audio
            for b in bs: beat_target[b["beat"]] = probe_dur(b["clip"])
            continue
        vis = sum(probe_dur(b["clip"]) for b in bs)
        scale = ch_audio[ch]/vis if vis>0 else 1.0
        for b in bs: beat_target[b["beat"]] = probe_dur(b["clip"])*scale

    for b in beats:
        v_parts.append(norm_beat(b["clip"], beat_target[b["beat"]], work, vi)); vi+=1
        ch = b["chapter"]
        if ch is None:            # sting -> matching silence
            a_parts.append(norm_audio(None, work, ai, silence_dur=beat_target[b["beat"]])); ai+=1
        elif ch not in seen_ch:   # first beat of a chapter -> whole chapter mp3 plays here across its beats
            a_parts.append(norm_audio(ch_mp3[ch], work, ai)); ai+=1
            seen_ch.add(ch)
    v = concat(v_parts, work, "video.mp4")
    a = concat(a_parts, work, "audio.m4a")
    _run(["ffmpeg","-y","-i",v,"-i",a,"-c:v","copy","-c:a","aac",
          "-map","0:v:0","-map","1:a:0",out_path])
    return {"video_dur":probe_dur(v),"audio_dur":probe_dur(a),"final_dur":probe_dur(out_path)}

if __name__ == "__main__":
    import sys
    m = json.load(open(sys.argv[1]))
    work = tempfile.mkdtemp()
    try:
        print(json.dumps(assemble(m, work, sys.argv[2]), indent=2))
    finally:
        shutil.rmtree(work, ignore_errors=True)
