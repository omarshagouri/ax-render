#!/usr/bin/env python3
"""AmpCoreX Agent 8 assembly core (transport-agnostic).
Per-chapter audio-master + proportional tail-freeze. Pure ffmpeg.

V1.2 (outro audio fix):
  - INTRO: optional 1.0s SILENT still from a photo, prepended to both tracks (unchanged).
  - OUTRO: optional end clip that KEEPS ITS OWN AUDIO. Now normalized into a
    standalone A/V clip and joined to the main video with the concat FILTER,
    so the outro's audio is guaranteed into the final (no -shortest tail clip,
    no copy-concat AAC drop). Body/intro pipeline is unchanged.
Both intro and outro are optional: absent -> assembles exactly as before.
"""
import json, os, subprocess, tempfile, shutil

INTRO_DUR = 1.0                 # seconds; the still intro photo is silent
INTRO_PAD_COLOR = "0x0A1628"    # brand navy letterbox if a source isn't 9:16

def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\n{r.stderr[-800:]}")
    return r.stdout

def probe_dur(path):
    out = _run(["ffprobe","-v","error","-show_entries","format=duration",
                "-of","default=nw=1:nk=1", path])
    return float(out.strip())

def has_audio_stream(path):
    """True if the file contains at least one audio stream."""
    out = _run(["ffprobe","-v","error","-select_streams","a",
                "-show_entries","stream=index","-of","csv=p=0", path])
    return bool(out.strip())

def norm_beat(src, target, work, idx):
    """Re-encode a silent beat clip to exactly `target` seconds, 1080x1920@30, yuv420p."""
    d = probe_dur(src); dst = os.path.join(work, f"v_{idx:03d}.mp4")
    pad = round(target - d, 3)
    vf = "scale=1080:1920:force_original_aspect_ratio=decrease,"\
         "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"
    if pad > 0.02:
        vf += f",tpad=stop_mode=clone:stop_duration={pad}"
        _run(["ffmpeg","-y","-i",src,"-vf",vf,"-an","-c:v","libx264",
              "-preset","veryfast","-r","30",dst])
    else:
        _run(["ffmpeg","-y","-i",src,"-t",f"{target:.3f}","-vf",vf,"-an",
              "-c:v","libx264","-preset","veryfast","-r","30",dst])
    return dst

def norm_photo(src, target, work, idx, pad_color=INTRO_PAD_COLOR):
    """Turn a still image into a SILENT 1080x1920@30 clip of exactly `target` seconds.
    Identical codec params to norm_beat so `concat -c copy` works. Letterbox = brand navy."""
    dst = os.path.join(work, f"v_{idx:03d}.mp4")
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color={pad_color},fps=30,format=yuv420p")
    _run(["ffmpeg","-y","-framerate","30","-loop","1","-i",src,"-t",f"{target:.3f}",
          "-vf",vf,"-an","-c:v","libx264","-preset","veryfast","-r","30",dst])
    return dst

def norm_audio(src, work, idx, silence_dur=None):
    """Decode any MP3/media audio (or make silence) to a normalized AAC 44100 stereo m4a."""
    dst = os.path.join(work, f"a_{idx:03d}.m4a")
    if silence_dur is not None:
        _run(["ffmpeg","-y","-f","lavfi","-i","anullsrc=r=44100:cl=stereo",
              "-t",f"{silence_dur:.3f}","-c:a","aac","-b:a","160k",dst])
    else:
        _run(["ffmpeg","-y","-i",src,"-ar","44100","-ac","2",
              "-c:a","aac","-b:a","160k",dst])
    return dst

def norm_outro_av(src, work, pad_color=INTRO_PAD_COLOR):
    """Normalize the outro into a STANDALONE clip that keeps its own audio, matched to the
    main video's params (1080x1920@30, yuv420p, SAR 1:1; AAC 44100 stereo). If the source
    has no audio stream, synth matching silence so the concat filter always has audio to map."""
    dst = os.path.join(work, "outro_av.mp4")
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color={pad_color},fps=30,format=yuv420p,setsar=1")
    if has_audio_stream(src):
        _run(["ffmpeg","-y","-i",src,"-vf",vf,"-r","30",
              "-c:v","libx264","-preset","veryfast",
              "-c:a","aac","-ar","44100","-ac","2","-b:a","160k",dst])
    else:
        dur = probe_dur(src)
        _run(["ffmpeg","-y","-i",src,
              "-f","lavfi","-t",f"{dur:.3f}","-i","anullsrc=r=44100:cl=stereo",
              "-filter_complex", f"[0:v]{vf}[v]",
              "-map","[v]","-map","1:a:0","-shortest",
              "-c:v","libx264","-preset","veryfast",
              "-c:a","aac","-ar","44100","-ac","2","-b:a","160k",dst])
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
        'chapters':[{'chapter':1,'mp3':<path>}, ...],  # in order
        'intro_photo': <path or None>,   # optional: 1s silent still at the very start
        'outro_video': <path or None>,   # optional: end clip that keeps its own audio
    }"""
    beats = manifest["beats"]
    ckey = lambda c: None if c is None else str(c)
    ch_mp3   = {ckey(c["chapter"]): c["mp3"] for c in manifest["chapters"]}
    ch_audio = {ckey(c["chapter"]): probe_dur(c["mp3"]) for c in manifest["chapters"]}

    from collections import OrderedDict
    groups = OrderedDict()
    for b in beats:
        groups.setdefault(ckey(b["chapter"]), []).append(b)

    v_parts, a_parts, ai, vi = [], [], 0, 0
    seen_ch = set()

    beat_target = {}
    for ch, bs in groups.items():
        if ch is None:
            for b in bs: beat_target[b["beat"]] = round(probe_dur(b["clip"])*30)/30
            continue
        vis = sum(probe_dur(b["clip"]) for b in bs)
        scale = ch_audio[ch]/vis if vis>0 else 1.0
        for b in bs: beat_target[b["beat"]] = round(probe_dur(b["clip"])*30)/30*scale

    for b in beats:
        v_parts.append(norm_beat(b["clip"], beat_target[b["beat"]], work, vi)); vi+=1
        ch = ckey(b["chapter"])
        if ch is None:
            a_parts.append(norm_audio(None, work, ai, silence_dur=beat_target[b["beat"]])); ai+=1
        elif ch not in seen_ch:
            a_parts.append(norm_audio(ch_mp3[ch], work, ai)); ai+=1
            seen_ch.add(ch)

    # ---- INTRO: 1s silent still, prepended to BOTH tracks so A/V stay locked ----
    intro_photo = manifest.get("intro_photo")
    if intro_photo:
        iv = norm_photo(intro_photo, INTRO_DUR, work, vi); vi += 1
        ia = norm_audio(None, work, ai, silence_dur=INTRO_DUR); ai += 1
        v_parts.insert(0, iv)
        a_parts.insert(0, ia)

    # ---- MAIN (intro + body): silent video track + narration audio track, muxed ----
    v = concat(v_parts, work, "video.mp4")
    a = concat(a_parts, work, "audio.m4a")
    main = os.path.join(work, "main.mp4")
    _run(["ffmpeg","-y","-i",v,"-i",a,"-filter_complex","[1:a]apad[a]",
          "-map","0:v:0","-map","[a]","-c:v","copy","-c:a","aac","-shortest",main])

    # ---- OUTRO: standalone A/V clip, joined via concat FILTER (keeps its own audio) ----
    outro_video = manifest.get("outro_video")
    if outro_video:
        outro = norm_outro_av(outro_video, work)
        _run(["ffmpeg","-y","-i",main,"-i",outro,"-filter_complex",
              "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[v][a]",
              "-map","[v]","-map","[a]",
              "-c:v","libx264","-preset","veryfast","-r","30",
              "-c:a","aac","-ar","44100","-ac","2","-b:a","160k",out_path])
    else:
        shutil.move(main, out_path)

    return {"video_dur":probe_dur(v),"audio_dur":probe_dur(a),
            "outro":bool(outro_video),"final_dur":probe_dur(out_path)}

if __name__ == "__main__":
    import sys
    m = json.load(open(sys.argv[1]))
    work = tempfile.mkdtemp()
    try:
        print(json.dumps(assemble(m, work, sys.argv[2]), indent=2))
    finally:
        shutil.rmtree(work, ignore_errors=True)
