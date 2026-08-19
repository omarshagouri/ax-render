#!/usr/bin/env python3
"""AmpCoreX cadence engine (deterministic, transport-agnostic).

Turns REAL measured chapter audio into a beat skeleton where every beat (visual
cell) lasts <= CAP seconds, so the screen is guaranteed to change on a fixed
retention cadence. This replaces LLM/byte-estimate durations as the planning
input: the arithmetic (how many cells, how long, which words) lives here; the
Visual Plan agent only chooses the visual for each pre-sized cell.

Cadence rule (AmpCoreX Shorts):  TARGET 3.0s  |  HARD CAP 4.0s  |  FLOOR 1.5s
  - No cell may exceed CAP (the retention invariant).
  - Cells aim for TARGET and never fall below FLOOR unless the whole chapter is
    shorter than FLOOR (nothing can be done then).
  - A chapter's cells sum EXACTLY to its real audio, so audio sync is preserved
    at every chapter boundary by construction (assembly's scale factor -> ~1,
    which is what removes the freeze/hold as a byproduct).

v1 uses even-time cells within a chapter (uniform, predictable rhythm; trivially
honors CAP/FLOOR) and maps the spoken words onto those cells by word position.
The precision upgrade (Whisper word-timestamps -> cell boundaries on real word
times) drops in later without changing this contract.
"""
import math, re

TARGET = 3.0    # seconds: aim
CAP    = 4.0    # seconds: hard maximum a single visual may stay on screen
FLOOR  = 1.5    # seconds: minimum a cell should be (avoid stutter cuts)


def plan_cells(dur, target=TARGET, cap=CAP, floor=FLOOR):
    """Split `dur` seconds into N even cells honoring cap/floor. Returns a list of
    per-cell durations that sums to `dur`. Guarantees max(cells) <= cap whenever
    dur >= floor; a sub-floor chapter returns a single (short) cell."""
    if dur <= 0:
        return []
    if dur <= cap:
        return [round(dur, 3)]                    # one cell already satisfies the cap
    n = round(dur / target)                       # aim for TARGET-length cells
    n = max(n, math.ceil(dur / cap))              # enforce the hard cap (mean <= cap)
    n = min(n, math.floor(dur / floor))           # enforce the floor (mean >= floor)
    n = max(n, 1)
    base = dur / n
    cells = [round(base, 3)] * n
    cells[-1] = round(dur - sum(cells[:-1]), 3)   # absorb rounding into the last cell
    return cells


def _words(text):
    return re.findall(r"\S+", text or "")


def _split_words(words, k):
    """Split a word list into k contiguous, roughly equal chunks (text each)."""
    n = len(words)
    out = []
    for i in range(k):
        a = round(i * n / k)
        b = round((i + 1) * n / k) if i < k - 1 else n
        b = max(b, a + 1) if (i < k - 1 and a < n) else b
        out.append(" ".join(words[a:b]))
    return out


def plan_chapter(chapter, real_audio, sentences, target=TARGET, cap=CAP, floor=FLOOR):
    """One chapter -> list of cell dicts (duration + the words spoken in that cell).

    Sentence-aware and deterministic:
      1) each sentence gets time proportional to its word share of the chapter;
      2) sub-FLOOR sentences merge into a neighbour, so one card can animate them
         internally (e.g. three "No —" lines become one Eliminate cell, struck in
         sequence) instead of stuttering as separate hard cuts;
      3) any unit longer than CAP is split into equal sub-cells (a long sentence
         becomes a multi-step timeline reveal), each <= CAP.
    Cells sum EXACTLY to real_audio, so chapter sync is preserved."""
    sents = [s for s in sentences if _words(s)] or [""]
    wc = [max(len(_words(s)), 1) for s in sents]
    tot = sum(wc)

    # 1) proportional units, one per sentence
    units = [{"words": _words(s), "dur": real_audio * w / tot, "sidx": [i]}
             for i, (s, w) in enumerate(zip(sents, wc))]

    # 2) merge any unit below the floor into its neighbour (forward, else back)
    if real_audio >= floor:
        i = 0
        while len(units) > 1 and i < len(units):
            if units[i]["dur"] < floor - 1e-9:
                j = i + 1 if i + 1 < len(units) else i - 1
                a, b = (i, j) if j > i else (j, i)
                units[a] = {"words": units[a]["words"] + units[b]["words"],
                            "dur": units[a]["dur"] + units[b]["dur"],
                            "sidx": units[a]["sidx"] + units[b]["sidx"]}
                del units[b]
                i = 0                       # restart: a merge can create a new sub-floor unit
            else:
                i += 1

    # 3) split over-cap units into equal sub-cells; emit cells
    cells = []
    for u in units:
        if u["dur"] > cap + 1e-9:
            k = max(math.ceil(u["dur"] / target), math.ceil(u["dur"] / cap))
            subd = plan_cells(u["dur"], target, cap, floor) if len(plan_cells(u["dur"], target, cap, floor)) == k \
                else [u["dur"] / k] * k
            subt = _split_words(u["words"], k)
            for d, t in zip(subd, subt):
                cells.append({"dur": d, "text": t, "sidx": u["sidx"]})
        else:
            cells.append({"dur": u["dur"], "text": " ".join(u["words"]), "sidx": u["sidx"]})

    # quantize to 3dp and absorb rounding into the last cell so the chapter sum is exact
    for c in cells:
        c["dur"] = round(c["dur"], 3)
    cells[-1]["dur"] = round(real_audio - sum(c["dur"] for c in cells[:-1]), 3)

    out = []
    for i, c in enumerate(cells):
        out.append({
            "chapter": int(chapter),
            "cell_in_chapter": i + 1,
            "cells_in_chapter": len(cells),
            "duration": c["dur"],
            "sentence": c["text"],
            "from_sentence": sorted(set(c["sidx"])),
        })
    return out, {"chapter": int(chapter), "real_audio": round(real_audio, 3),
                 "cells": len(cells),
                 "cell_dur": round(real_audio / len(cells), 3) if cells else 0}


def plan_video(chapters, target=TARGET, cap=CAP, floor=FLOOR):
    """chapters = [{'chapter':1,'real_audio':11.2,'sentences':[...]}, ...] (in order).
    Returns (beats, summary): a flat, globally-numbered beat skeleton the Visual
    Plan agent decorates with card_id + values, and a per-chapter summary."""
    beats, summary, beat_no = [], [], 0
    for c in chapters:
        cells, ch_sum = plan_chapter(c["chapter"], c["real_audio"], c.get("sentences", []),
                                     target, cap, floor)
        for cell in cells:
            beat_no += 1
            beats.append({"beat": beat_no, **cell})
        summary.append(ch_sum)
    return beats, summary
