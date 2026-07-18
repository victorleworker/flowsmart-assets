#!/usr/bin/env python3
"""Studio Tin Tức — audio-first assembly (P6).

Reads a manifest JSON (written by NewsProductionView) and renders the bài:
  1. Per segment: normalize the picked media into export/segments/<segId>.mp4
     - clip  → scale/crop to WxH, trim to the timing slot, freeze-pad if shorter;
               in-clip audio volume = seg "clipVolume" (0..1, app resolves per-seg
               override → bài-wide choice → audioMode default); manifests without
               the field fall back to audioMode (voiceover bed / others full).
     - image → Ken Burns (slow zoompan) for the slot duration, silent audio.
  2. Concat all segment clips (uniform h264/aac intermediates → concat demuxer).
  3. Mix the narration voiceover (silence for sound-breaks is already baked in by
     the app) + optional music bed on top; optionally burn subtitles.
  4. Write <outDir>/FINAL.mp4 and print @@SUMMARY@@ {json} for the app.

Manifest:
{
  "width": 1920, "height": 1080, "fps": 30,
  "segments": [{"id","type":"clip"|"image","media","startSec","endSec","audioMode","clipVolume"?}],
  "voiceover": "/abs/voice/voiceover.mp3" | null,
  "srt": "/abs/voice/subtitles.srt" | null,   # burn when burnSub true
  "burnSub": false,
  "subPos": {"x": 0.5, "y": 0.88} | null,     # normalized cue-block center (drag in app)
  "subStyle": {"size": 0.045, "color": "#ffffff"} | null,  # size = fraction of frame height
  "music": {"path": "...", "paths": ["...",...]?, "volume": 0.15, "loop": true} | null,
      # loop=fill whole bài; paths (18/7) = PLAYLIST theo thứ tự — nối thành 1 bed rồi loop/cắt
      # theo bài (path đơn = back-compat, bị paths thắng khi có)
  "watermark": {"path": "/abs/wm.png", "x": 1, "y": 0,   # channel logo overlay (before subs)
                "marginPct": 2.5, "scale": 0.12, "opacity": 0.7} | null,
                # x/y = 0..1 inside the free space after the margin (corners = exactly 0/1);
                # scale = logo width / frame width; marginPct = % of the SHORTER frame side.
  "removeAiWm": {"region": {"x","y","w","h"}} | true | null,  # delogo the ✦ Flow/Veo badge on
                # every VIDEO segment (fractions of the SOURCE frame; default = Flow badge,
                # same as studio-kich remove_watermark.py — works for 16:9 and 9:16 alike)
  "overlays": [{"path": "/abs/export/overlays/headline.png",  # full-frame PNG "chữ bản tin"
                "startSec": 2.8, "endSec": 8.8}] | null,      # (intro/headline/credit/lower-third)
                # applied AFTER the watermark, BEFORE the subtitle burn, overlay 0:0 with
                # enable='between(t,start,end)' — PNGs are rendered frame-sized by the app.
  "outName": "FINAL_916.mp4" | null,     # default FINAL.mp4 (variant exports keep canonical intact)
  "segSubdir": "segments916" | null,     # default "segments" — variant intermediates live apart
  "outDir": "/abs/export"
}

Usage: news_export.py <manifest.json>
"""
import json
import os
import shutil
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")

# In-clip audio level per audioMode (narration rides on top from the voiceover track).
CLIP_VOLUME = {"voiceover": 0.06, "sound-break": 1.0, "dialogue": 1.0}

# ✦ Flow/Veo badge — legacy FRACTION region (only used when the manifest passes an explicit
# removeAiWm.region; measured on a 9:16 OMNI clip with glow margin).
AI_WM_REGION = {"x": 0.76, "y": 0.845, "w": 0.205, "h": 0.125}


def probe_dims(path):
    """(width, height, durationSec) of the clip's video stream."""
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,duration", "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        st = json.loads(proc.stdout)["streams"][0]
        return int(st["width"]), int(st["height"]), float(st.get("duration") or 8.0)
    except Exception:
        return 0, 0, 0.0


# --- AI watermark geometry + auto-detect (2026-07-17) -------------------------------------
# MIRROR of studio-kich/scripts/remove_watermark.py — keep both in sync. Flow/Veo stamp one
# of TWO watermarks, both anchored a FIXED PIXEL distance from the bottom-right corner
# (s = min(W,H)/720 — Flow renders 720p then upscales, so 1080p is 1.5×):
#   • ✦ sparkle (Flow web): center ~123s px from both edges, glyph ~46s px → candidate box
#     72s×72s at (W-159s, H-159s). Measured on real 720x1280 clips + a 16:9 screenshot.
#   • "Veo" text (API/relay): tight corner box (W-52s, H-38s, 48s×34s), measured 1280x720.
# One fractional region can't serve both aspects — hence pixel anchors + per-clip detection:
# sample ~10 frames from the middle 10–90% (dodges fade-to-black), robust temporal min
# (2nd smallest) — a static semi-transparent white mark keeps min high while moving content
# dips dark. Nothing detected → delogo BOTH candidate boxes (removal was requested).

def clamp_box(x, y, w, h, W, H):
    """delogo requires the rect strictly inside the frame (x/y >= 1, x+w/y+h <= dim-1)."""
    x = max(1, min(int(x), W - 3))
    y = max(1, min(int(y), H - 3))
    w = max(2, min(int(w), W - 1 - x))
    h = max(2, min(int(h), H - 1 - y))
    return x, y, w, h


def wm_candidate_boxes(W, H):
    s = min(W, H) / 720.0
    px = lambda v: int(round(v * s))
    return {
        "sparkle": (W - px(159), H - px(159), px(72), px(72)),
        "veotext": (W - px(52), H - px(38), px(48), px(34)),
    }


def detect_wm_boxes(path, W, H, dur):
    """Auto-detect which watermark(s) this clip carries → list of tight (x,y,w,h) boxes."""
    s = min(W, H) / 720.0
    cw, ch = int(220 * s), int(220 * s)
    cx, cy = W - cw, H - ch
    t0, span = dur * 0.1, max(0.5, dur * 0.8)
    fps = max(0.5, min(6.0, 10 / span))
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{t0:.2f}", "-t", f"{span:.2f}", "-i", path,
         "-vf", f"fps={fps},crop={cw}:{ch}:{cx}:{cy},format=gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    data = proc.stdout
    fsz = cw * ch
    n = len(data) // fsz if fsz else 0
    if n < 3:
        return []
    frames = [data[i * fsz:(i + 1) * fsz] for i in range(n)]
    k = 1 if n >= 5 else 0  # robust min: 2nd smallest survives one dark/fade frame
    mn = bytearray(fsz)
    for i in range(fsz):
        vals = sorted(fr[i] for fr in frames)
        mn[i] = vals[k]

    found = []
    for name, (bx, by, bw, bh) in wm_candidate_boxes(W, H).items():
        rx, ry = bx - cx, by - cy
        pad = int(14 * s)
        ring = [mn[yy * cw + xx]
                for yy in range(max(0, ry - pad), min(ch, ry + bh + pad))
                for xx in range(max(0, rx - pad), min(cw, rx + bw + pad))
                if not (rx <= xx < rx + bw and ry <= yy < ry + bh)]
        ring.sort()
        ring_med = ring[len(ring) // 2] if ring else 0
        thr = max(110, ring_med + 35)
        hot = [(xx, yy)
               for yy in range(max(0, ry), min(ch, ry + bh))
               for xx in range(max(0, rx), min(cw, rx + bw))
               if mn[yy * cw + xx] >= thr]
        need = (18 if name == "sparkle" else 8) * s * s
        if len(hot) >= need:
            xs = [p[0] for p in hot]; ys = [p[1] for p in hot]
            g = int((12 if name == "sparkle" else 5) * s)  # glow margin
            found.append(clamp_box(min(xs) + cx - g, min(ys) + cy - g,
                                   max(xs) - min(xs) + 1 + 2 * g, max(ys) - min(ys) + 1 + 2 * g, W, H))
    return found


def delogo_filter(media, region):
    """delogo filter(s) for THIS clip's source frame. Explicit fraction region from the
    manifest = legacy behavior; otherwise auto-detect (fallback: both candidate boxes)."""
    W, H, dur = probe_dims(media)
    if not W or not H:
        return None
    if isinstance(region, dict) and region:
        reg = {**AI_WM_REGION, **region}
        boxes = [clamp_box(W * reg["x"], H * reg["y"], W * reg["w"], H * reg["h"], W, H)]
    else:
        boxes = detect_wm_boxes(media, W, H, dur)
        if not boxes:
            boxes = [clamp_box(*b, W, H) for b in wm_candidate_boxes(W, H).values()]
    return ",".join(f"delogo=x={x}:y={y}:w={w}:h={h}" for x, y, w, h in boxes)


def run(args, tag):
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise RuntimeError(f"{tag}: ffmpeg exit {proc.returncode}\n{tail}")


def probe_duration(path):
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def has_audio(path):
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return "audio" in (proc.stdout or "")


def build_segment(seg, W, H, FPS, out_path, delogo=None):
    dur = max(0.2, float(seg["endSec"]) - float(seg["startSec"]))
    media = seg["media"]
    mode = seg.get("audioMode", "voiceover")
    # delogo runs FIRST — its pixel coords are in the SOURCE frame, before scale/crop.
    fit = ((delogo + ",") if delogo else "") + \
        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},format=yuv420p"

    if seg["type"] == "image":
        # Ken Burns: slow push-in (1.0 → ~1.12) centered. zoompan runs at fps over d frames.
        frames = max(2, int(round(dur * FPS)))
        vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='1+0.12*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={FPS},setsar=1,format=yuv420p")
        args = [FFMPEG, "-y", "-loop", "1", "-t", str(dur), "-i", media,
                "-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=44100:cl=stereo",
                "-filter_complex", f"[0:v]{vf}[v]",
                "-map", "[v]", "-map", "1:a",
                "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", out_path]
        run(args, seg["id"])
        return

    # clip — freeze-pad the tail if the source is shorter than the slot.
    src_dur = probe_duration(media)
    pad = max(0.0, dur - src_dur)
    vf = fit + (f",tpad=stop_mode=clone:stop_duration={pad:.3f}" if pad > 0.05 else "")
    # Tiếng gốc: app resolve sẵn per-segment (seg.clipVolume riêng → chọn chung của bài →
    # mặc định theo audioMode) thành 'clipVolume'; manifest cũ không có field → theo mode.
    vol = seg.get("clipVolume")
    vol = CLIP_VOLUME.get(mode, 0.06) if vol is None else max(0.0, min(1.0, float(vol)))
    if has_audio(media):
        af = f"[0:a]volume={vol},aresample=44100,aformat=channel_layouts=stereo,apad[a]"
        args = [FFMPEG, "-y", "-i", media,
                "-filter_complex", f"[0:v]{vf}[v];{af}",
                "-map", "[v]", "-map", "[a]",
                "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", out_path]
    else:
        args = [FFMPEG, "-y", "-i", media,
                "-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=44100:cl=stereo",
                "-filter_complex", f"[0:v]{vf}[v]",
                "-map", "[v]", "-map", "1:a",
                "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", out_path]
    run(args, seg["id"])


def main():
    if len(sys.argv) < 2:
        print("usage: news_export.py <manifest.json>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        m = json.load(f)

    W, H, FPS = int(m.get("width", 1920)), int(m.get("height", 1080)), int(m.get("fps", 30))
    out_dir = m["outDir"]
    seg_dir = os.path.join(out_dir, m.get("segSubdir") or "segments")
    os.makedirs(seg_dir, exist_ok=True)

    rm = m.get("removeAiWm")
    if rm is None or rm is False:  # NOT `or None` — an empty {} means "enabled, default region"
        rm = None
    delogo_count = 0
    seg_files = []
    total = len(m["segments"])
    for i, seg in enumerate(m["segments"]):
        print(f"SEG {i + 1}/{total} {seg['id']}", flush=True)
        # % lên thanh top app (main parse @@PROGRESS@@ → python:progress; +2 = CONCAT + MIX)
        print("@@PROGRESS@@ " + json.dumps({"index": i, "total": total + 2, "step": seg["id"]}), flush=True)
        out_path = os.path.join(seg_dir, f"{seg['id']}.mp4")
        dl = None
        if rm is not None and seg["type"] == "clip":
            dl = delogo_filter(seg["media"], rm.get("region") if isinstance(rm, dict) else None)
            if dl:
                delogo_count += 1
        build_segment(seg, W, H, FPS, out_path, dl)
        seg_files.append(out_path)

    # ---- concat (uniform intermediates → demuxer is safe) ----
    print("CONCAT", flush=True)
    print("@@PROGRESS@@ " + json.dumps({"index": total, "total": total + 2, "step": "concat"}), flush=True)
    list_path = os.path.join(seg_dir, "_concat.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in seg_files:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    raw_path = os.path.join(out_dir, "_raw.mp4")
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", raw_path], "concat")
    total_dur = probe_duration(raw_path)

    # ---- final mix: raw + voiceover + music (+ subs: burn if libass exists, else soft mov_text) ----
    print("MIX", flush=True)
    print("@@PROGRESS@@ " + json.dumps({"index": total + 1, "total": total + 2, "step": "mix"}), flush=True)
    final_path = os.path.join(out_dir, m.get("outName") or "FINAL.mp4")
    srt_path = m.get("srt") if (m.get("srt") and os.path.exists(m["srt"])) else None
    sub_result = "none"

    # Playlist nhạc nền (18/7): music.paths >1 file → decode + nối thành 1 bed WAV trung gian
    # (48k stereo pcm — mp3 sample-rate lệch nhau mà nối concat demuxer là vỡ tiếng), rồi nhánh
    # single-file sẵn có (loop-to-fill / atrim cắt theo bài) dùng tiếp y nguyên. 1 file = đường cũ.
    music_cfg = m.get("music") or None
    if music_cfg:
        _mp = [p for p in (music_cfg.get("paths") or []) if p and os.path.exists(p)]
        if not _mp and music_cfg.get("path") and os.path.exists(music_cfg["path"]):
            _mp = [music_cfg["path"]]
        if len(_mp) > 1:
            bed_path = os.path.join(out_dir, "_music_bed.wav")
            ff = [FFMPEG, "-y"]
            for p in _mp:
                ff += ["-i", p]
            fg = "".join(f"[{k}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[m{k}];" for k in range(len(_mp)))
            fg += "".join(f"[m{k}]" for k in range(len(_mp))) + f"concat=n={len(_mp)}:v=0:a=1[mbed]"
            run(ff + ["-filter_complex", fg, "-map", "[mbed]", "-c:a", "pcm_s16le", bed_path], "musicbed")
            music_cfg = dict(music_cfg)
            music_cfg["path"] = bed_path
        elif _mp:
            music_cfg = dict(music_cfg)
            music_cfg["path"] = _mp[0]
        else:
            music_cfg = None

    def final_mix(burn):
        inputs = [FFMPEG, "-y", "-i", raw_path]
        audio_labels = ["[base]"]
        filters = ["[0:a]anull[base]"]
        idx = 1
        if m.get("voiceover") and os.path.exists(m["voiceover"]):
            inputs += ["-i", m["voiceover"]]
            filters.append(f"[{idx}:a]aresample=44100,aformat=channel_layouts=stereo,apad,atrim=0:{total_dur:.3f}[vo]")
            audio_labels.append("[vo]")
            idx += 1
        music = music_cfg  # đã resolve playlist→bed ở trên; None nếu không có/thiếu file
        if music and music.get("path") and os.path.exists(music["path"]):
            vol = float(music.get("volume", 0.15))
            loop_args = ["-stream_loop", "-1"] if music.get("loop", True) else []
            inputs += [*loop_args, "-i", music["path"]]
            filters.append(f"[{idx}:a]volume={vol},aresample=44100,aformat=channel_layouts=stereo,atrim=0:{total_dur:.3f}[mu]")
            audio_labels.append("[mu]")
            idx += 1

        if len(audio_labels) > 1:
            filters.append(f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=first:dropout_transition=0:normalize=0[aout]")
            amap = "[aout]"
        else:
            amap = "[base]"

        vmap = "0:v"
        vsrc = "[0:v]"
        smaps = []
        # Channel logo overlay — BEFORE the subtitle burn so cues render on top of it.
        wm = m.get("watermark") or None
        if wm and wm.get("path") and os.path.exists(wm["path"]):
            wm_scale = min(0.4, max(0.03, float(wm.get("scale", 0.12))))
            wm_op = min(1.0, max(0.05, float(wm.get("opacity", 0.7))))
            wm_x = min(1.0, max(0.0, float(wm.get("x", 1.0))))
            wm_y = min(1.0, max(0.0, float(wm.get("y", 0.0))))
            margin = int(round(min(W, H) * float(wm.get("marginPct", 2.5)) / 100.0))
            inputs += ["-i", wm["path"]]
            filters.append(f"[{idx}:v]scale={max(16, int(W * wm_scale))}:-1,format=rgba,"
                           f"colorchannelmixer=aa={wm_op}[wm]")
            # x/y pick a spot in the free space left after the margins (0 = flush left/top,
            # 1 = flush right/bottom) — the exact model the app's drag overlay uses.
            filters.append(f"[0:v][wm]overlay=x={margin}+(W-w-{2 * margin})*{wm_x:.4f}"
                           f":y={margin}+(H-h-{2 * margin})*{wm_y:.4f}[vwm]")
            vsrc = "[vwm]"
            vmap = "[vwm]"
            idx += 1
        # "Chữ bản tin" — frame-sized PNGs over their time window (after watermark, under subs).
        for oi, ov in enumerate(m.get("overlays") or []):
            if not (ov and ov.get("path") and os.path.exists(ov["path"])):
                continue
            start = float(ov.get("startSec", 0))
            end = float(ov.get("endSec", 0))
            if end <= start:
                continue
            inputs += ["-i", ov["path"]]
            filters.append(f"[{idx}:v]format=rgba[ov{oi}]")
            filters.append(f"{vsrc}[ov{oi}]overlay=0:0:enable='between(t,{start:.2f},{end:.2f})'[vov{oi}]")
            vsrc = f"[vov{oi}]"
            vmap = vsrc
            idx += 1
        if srt_path and burn:
            esc = srt_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            # libass lays SRT out on a 384x288 PlayRes canvas; margins/font size use those units.
            sty_parts = []
            pos = m.get("subPos") or None
            if pos:
                # Alignment 2 = bottom-center: MarginV is the gap below the block, and the
                # L/R margin imbalance shifts the centering point horizontally.
                px = min(0.95, max(0.05, float(pos.get("x", 0.5))))
                py = min(0.96, max(0.04, float(pos.get("y", 0.88))))
                mv = max(0, int(round((1.0 - py) * 288 - 12)))
                dx = int(round((px - 0.5) * 2 * 384))
                ml, mr = (dx, 0) if dx > 0 else (0, -dx)
                sty_parts.append(f"Alignment=2,MarginV={mv},MarginL={ml},MarginR={mr}")
            sty = m.get("subStyle") or None
            if sty:
                size = min(0.14, max(0.02, float(sty.get("size", 0.045))))
                sty_parts.append(f"FontSize={max(6, int(round(size * 288)))}")
                col = str(sty.get("color", "")).lstrip("#")
                if len(col) == 6:
                    # ASS PrimaryColour is &HAABBGGRR (alpha first, BGR byte order).
                    bgr = (col[4:6] + col[2:4] + col[0:2]).upper()
                    sty_parts.append(f"PrimaryColour=&H00{bgr}")
            style = f":force_style='{','.join(sty_parts)}'" if sty_parts else ""
            filters.append(f"{vsrc}subtitles=filename='{esc}'{style}[vout]")
            vmap = "[vout]"
        elif srt_path:
            # No libass in many ffmpeg builds — mux a SOFT mov_text track instead (toggleable in players).
            inputs += ["-i", srt_path]
            smaps = ["-map", f"{idx}:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=vie"]
            idx += 1

        # Any filtered video (subs burn and/or watermark overlay) must re-encode.
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"] if vmap != "0:v" else ["-c:v", "copy"]
        args = inputs + ["-filter_complex", ";".join(filters), "-map", vmap, "-map", amap, *smaps,
                         *vcodec, "-c:a", "aac", "-ar", "44100", "-ac", "2",
                         "-movflags", "+faststart", final_path]
        run(args, "final-mix")

    want_burn = bool(m.get("burnSub")) and srt_path is not None
    try:
        final_mix(want_burn)
        sub_result = "burned" if want_burn else ("soft" if srt_path else "none")
    except RuntimeError as e:
        if want_burn and "No such filter" in str(e):
            print("WARN: subtitles filter unavailable (ffmpeg without libass) — muxing soft subs instead", flush=True)
            final_mix(False)
            sub_result = "soft"
        else:
            raise

    try:
        os.remove(raw_path)
        os.remove(list_path)
    except OSError:
        pass

    wm_used = bool((m.get("watermark") or {}).get("path")) and os.path.exists((m.get("watermark") or {}).get("path", ""))
    print("@@SUMMARY@@ " + json.dumps({
        "final": final_path,
        "durationSec": round(probe_duration(final_path), 2),
        "segments": len(seg_files),
        "segDir": seg_dir,
        "subtitles": sub_result,
        "watermark": wm_used,
        "delogo": delogo_count,
    }), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — surface a clean tail to the app
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
