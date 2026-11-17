#!/usr/bin/env python3
"""وضع المراقبة: يبقى متصلاً بالبث المباشر، وعندما ينفجر الشات أو يرتفع الصوت بشكل غير عادي
يحفظ آخر ~25 ثانية + 12 ثانية بعدها كلقطة عمودية ويرفعها مسودة إلى Rofilm Hub."""
import argparse, os, re, shlex, subprocess, sys, time, json, glob, shutil
import numpy as np
import agent
from agent import SR, WIN, ChatCounter, zscore

SEG = 5            # ثوانٍ لكل مقطع
LEAD, TAIL = 25, 12

def seg_rms(path):
    try:
        return agent.audio_rms(path)
    except Exception:
        return np.zeros(int(SEG / WIN))

def make_clip(segdir, first_idx, last_idx, trig_t, out):
    files = [f"{segdir}/seg_{i:06d}.ts" for i in range(first_idx, last_idx + 1) if os.path.exists(f"{segdir}/seg_{i:06d}.ts")]
    if len(files) < 3: return False
    lst = f"{segdir}/list_{int(trig_t)}.txt"
    with open(lst, "w") as f:
        for p in files: f.write(f"file '{os.path.abspath(p)}'\n")
    merged = f"{segdir}/m_{int(trig_t)}.ts"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", merged], check=True)
    dur = len(files) * SEG
    agent.cut(merged, {"start": 0, "dur": dur}, out)
    for p in (lst, merged):
        try: os.remove(p)
        except OSError: pass
    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url"); ap.add_argument("--source-cmd", help="أمر يطلع البث إلى stdout (للاختبار)")
    ap.add_argument("--hours", type=float, default=5.0)
    ap.add_argument("--max-clips", type=int, default=8)
    ap.add_argument("--threshold", type=float, default=3.2, help="كلما زاد صار أكثر تشدداً")
    ap.add_argument("--cooldown", type=int, default=90)
    ap.add_argument("--name", default=""); ap.add_argument("--title", default="")
    ap.add_argument("--out", default="clips"); ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    segdir = os.path.join(a.out, "segs"); shutil.rmtree(segdir, ignore_errors=True); os.makedirs(segdir)
    chat = None
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", a.url or "")
    if m: chat = ChatCounter(m.group(1)); chat.start()
    # مصدر البث
    if a.source_cmd:
        src = subprocess.Popen(a.source_cmd, shell=True, stdout=subprocess.PIPE)
    else:
        src = subprocess.Popen(["streamlink", "--stdout", "--twitch-disable-ads", a.url, "720p60,720p,480p,best"], stdout=subprocess.PIPE)
    rec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy", "-f", "segment", "-segment_time", str(SEG),
                            "-reset_timestamps", "1", f"{segdir}/seg_%06d.ts"], stdin=src.stdout)
    src.stdout.close()
    t_start = time.time(); end_at = t_start + a.hours * 3600
    base, token = os.environ.get("HUB_BASE"), os.environ.get("HUB_TOKEN")
    rms_hist, chat_hist = [], []       # قيمة لكل مقطع
    k = 0; last_trig = -1e9; clips = 0; pending = []   # [(trigger_seg, score)]
    print(f"المراقبة بدأت. الحد {a.threshold}", flush=True)
    while time.time() < end_at and clips < a.max_clips:
        time.sleep(1)
        # مقطع k اكتمل عندما يظهر k+1
        while os.path.exists(f"{segdir}/seg_{k+1:06d}.ts"):
            r = seg_rms(f"{segdir}/seg_{k:06d}.ts")
            rms_hist.append(float(np.sqrt((r ** 2).mean())) if len(r) else 0.0)
            if chat:
                c = sum(1 for t in chat.times if k * SEG <= t < (k + 1) * SEG)
                chat_hist.append(c)
            # تقييم
            i = len(rms_hist) - 1
            bgN = 36   # 3 دقائق
            if i >= 12:
                bg = np.array(rms_hist[max(0, i - bgN):i - 1]) + 1e-6
                a_rms = (np.log(rms_hist[i] + 1e-6) - np.median(np.log(bg))) / (1.4826 * np.median(np.abs(np.log(bg) - np.median(np.log(bg)))) + 0.15)
                a_chat = 0.0
                if chat and len(chat_hist) > 12:
                    cb = np.array(chat_hist[max(0, i - bgN):i - 1], dtype=float)
                    cur = np.mean(chat_hist[i - 1:i + 1])
                    a_chat = (cur - np.median(cb)) / (1.4826 * np.median(np.abs(cb - np.median(cb))) + 1.0)
                    if np.mean(chat_hist[i - 1:i + 1]) < 4: a_chat = 0.0   # شات هادئ: تجاهل
                score = max(0, a_rms) * 0.7 + max(0, a_chat) * 0.9
                if score >= a.threshold and k * SEG - last_trig > a.cooldown:
                    last_trig = k * SEG
                    pending.append((k, score))
                    print(f"⚡ لحظة قوية عند {k*SEG}s (درجة {score:.1f}) صوت={a_rms:.1f} شات={a_chat:.1f}", flush=True)
            k += 1
        # نفّذ اللقطات التي اكتمل ذيلها
        for (tk, sc) in list(pending):
            if k >= tk + int(TAIL / SEG) + 2:
                pending.remove((tk, sc))
                out = os.path.join(a.out, f"clip_{clips+1:02d}.mp4")
                first = max(0, tk - int(LEAD / SEG)); last = tk + int(TAIL / SEG) + 1
                if make_clip(segdir, first, last, tk * SEG, out):
                    clips += 1
                    mm, ss = divmod(int(tk * SEG), 60)
                    title = ((a.title or (a.name + " لحظة أسطورية" if a.name else "لحظة أسطورية")).strip()[:85]) + f" #{clips}"
                    desc = f"لقطة من بث {a.name}\nالمصدر: {a.url}\n#Shorts #GTA6"
                    print(f"✅ لقطة {clips}: {out}", flush=True)
                    if not a.no_upload and base and token:
                        try: print("رُفعت:", agent.upload_to_hub(base, token, out, title, desc), flush=True)
                        except Exception as e: print("فشل الرفع:", e, flush=True)
        # تنظيف المقاطع القديمة (نحتفظ بآخر 10 دقائق)
        for old in glob.glob(f"{segdir}/seg_*.ts"):
            n = int(os.path.basename(old)[4:10])
            if n < k - 120: 
                try: os.remove(old)
                except OSError: pass
        if rec.poll() is not None and not os.path.exists(f"{segdir}/seg_{k+1:06d}.ts"):
            print("انتهى البث أو انقطع الاتصال."); break
    rec.terminate()
    if chat: chat.stop = True
    print(f"انتهت المراقبة. عدد اللقطات: {clips}")

if __name__ == "__main__":
    main()
