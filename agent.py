#!/usr/bin/env python3
"""Clip Agent: يسجّل بثاً مباشراً (Twitch/YouTube)، يلتقط أقوى اللحظات، يقصّها عمودية 9:16،
ويرفعها مسودات إلى Rofilm Hub لتضغط أنت "نشر".

مثال:  python agent.py --url https://twitch.tv/NAME --minutes 45 --max-clips 5
اختبار محلي بملف جاهز:  python agent.py --file test.mp4 --no-upload
"""
import argparse, os, re, socket, subprocess, sys, threading, time, json, shutil, urllib.request, urllib.parse
import numpy as np

SR = 16000
WIN = 0.5          # ثانية: دقة تحليل الصوت

def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=True, **kw)

# ---------------------------------------------------------------- تسجيل
def record(url, minutes, out):
    """streamlink -> ffmpeg، يسجّل حتى minutes دقيقة. إن انتهى البث قبلها يكتفي بما سُجّل."""
    secs = int(minutes * 60)
    sl = ["streamlink", "--stdout", "--twitch-disable-ads", url, "720p60,720p,480p,best"]
    ff = ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0", "-t", str(secs), "-c", "copy", out]
    p1 = subprocess.Popen(sl, stdout=subprocess.PIPE)
    p2 = subprocess.Popen(ff, stdin=p1.stdout)
    p1.stdout.close()
    try:
        p2.wait(timeout=secs + 120)
    except subprocess.TimeoutExpired:
        p2.kill()
    p1.terminate()
    if not os.path.exists(out) or os.path.getsize(out) < 100_000:
        raise SystemExit("لم يُسجَّل شيء. تأكد أن الرابط لبث مباشر شغّال الآن.")

# ---------------------------------------------------------------- محادثة تويتش (اختياري)
class ChatCounter(threading.Thread):
    """يعدّ رسائل الشات في تويتش بدون حساب (justinfan). نشاط الشات يرتفع عند اللحظات القوية."""
    def __init__(self, channel):
        super().__init__(daemon=True)
        self.channel, self.t0, self.times, self.stop = channel.lower(), time.time(), [], False
    def run(self):
        try:
            s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=20)
            s.sendall(b"PASS SCHMOOPIIE\r\nNICK justinfan12345\r\n" + f"JOIN #{self.channel}\r\n".encode())
            s.settimeout(30); buf = b""
            while not self.stop:
                try: d = s.recv(4096)
                except socket.timeout: continue
                if not d: break
                buf += d
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    if line.startswith(b"PING"): s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif b" PRIVMSG " in line: self.times.append(time.time() - self.t0)
        except Exception as e:
            print("chat: stopped:", e)

def chat_score(times, n, offset=-6.0):
    """سلسلة بطول n نافذة (كل WIN ثانية): عدد الرسائل في آخر 10 ثوانٍ، مزاحة offset (الشات يتأخر عن الحدث)."""
    c = np.zeros(n)
    for t in times:
        i = int((t + offset) / WIN)
        if 0 <= i < n: c[i] += 1
    k = int(10 / WIN)
    return np.convolve(c, np.ones(k), mode="same")

# ---------------------------------------------------------------- كشف اللحظات
def audio_rms(path):
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    w = int(SR * WIN); n = len(x) // w
    return np.sqrt((x[: n * w].reshape(n, w) ** 2).mean(axis=1) + 1e-12)

def zscore(a):
    med = np.median(a); mad = np.median(np.abs(a - med)) + 1e-9
    return (a - med) / (1.4826 * mad)

def smooth(a, secs):
    k = max(1, int(secs / WIN)); return np.convolve(a, np.ones(k) / k, mode="same")

def find_peaks(rms, chat, max_clips, min_gap=60, lead=25, tail=12):
    # الصوت: متوسط 4 ثوانٍ مقابل خلفية 3 دقائق (يتجاهل الأصوات الثابتة العالية)
    s = smooth(rms, 4)
    bg = smooth(rms, 180)
    score = zscore(np.log(s + 1e-6) - np.log(bg + 1e-6))
    if chat is not None and chat.sum() > 20:
        score = score + 0.8 * np.clip(zscore(chat), -2, 6)
    dur = len(rms) * WIN
    picks = []
    for i in np.argsort(-score):
        t = i * WIN
        if score[i] < 1.5: break
        if t < lead or t > dur - 5: continue
        if any(abs(t - p[0]) < min_gap for p in picks): continue
        picks.append((t, float(score[i])))
        if len(picks) >= max_clips: break
    out = []
    for t, sc in sorted(picks):
        a = max(0, t - lead); b = min(dur, t + tail)
        out.append({"peak": t, "score": sc, "start": a, "dur": b - a})
    return out

# ---------------------------------------------------------------- قص عمودي
VF = ("[0:v]split[a][b];"
      "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:6[bg];"
      "[b]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
      "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")

def cut(src, c, out, fade=True):
    af = f"afade=t=in:d=0.3,afade=t=out:st={max(0, c['dur']-0.6):.2f}:d=0.6" if fade else "anull"
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{c['start']:.2f}", "-t", f"{c['dur']:.2f}", "-i", src,
         "-filter_complex", VF, "-map", "[v]", "-map", "0:a?", "-af", af,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out])

# ---------------------------------------------------------------- رفع إلى Rofilm Hub
def upload_to_hub(base, token, path, title, description):
    base = base.rstrip("/")
    H = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ClipAgent/1.0", "Accept": "application/json"}
    def call(req, t):
        try:
            return urllib.request.urlopen(req, timeout=t).read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} من {req.full_url.split('/t/')[0]}: {e.read()[:200]!r}")
    body = json.dumps({"filename": os.path.basename(path), "title": title, "description": description}).encode()
    req = urllib.request.Request(f"{base}/t/{token}/new", data=body, method="POST", headers={**H, "Content-Type": "application/json"})
    vid = json.loads(call(req, 90))["video_id"]
    with open(path, "rb") as f:
        data = f.read()
    up = urllib.request.Request(f"{base}/t/{token}/upload/{vid}", data=data, method="PUT", headers={**H, "Content-Type": "video/mp4"})
    call(up, 600)
    return vid

# ---------------------------------------------------------------- الرئيسي
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url"); ap.add_argument("--file")
    ap.add_argument("--minutes", type=float, default=45)
    ap.add_argument("--max-clips", type=int, default=5)
    ap.add_argument("--name", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--out", default="clips")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rec = os.path.join(a.out, "recording.ts")
    chat = None
    if a.file:
        rec = a.file
    else:
        if not a.url: sys.exit("--url مطلوب")
        m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", a.url)
        if m:
            chat = ChatCounter(m.group(1)); chat.start()
        record(a.url, a.minutes, rec)
        if chat: chat.stop = True
    rms = audio_rms(rec)
    cs = chat_score(chat.times, len(rms)) if chat else None
    print(f"المدة: {len(rms)*WIN/60:.1f} دقيقة، رسائل الشات: {len(chat.times) if chat else 0}")
    cands = find_peaks(rms, cs, a.max_clips)
    print("اللحظات:", json.dumps(cands, ensure_ascii=False))
    if not cands: print("لم أجد لحظات قوية كفاية."); return
    base, token = os.environ.get("HUB_BASE"), os.environ.get("HUB_TOKEN")
    for i, c in enumerate(cands, 1):
        out = os.path.join(a.out, f"clip_{i:02d}.mp4")
        cut(rec, c, out)
        mm, ss = divmod(int(c["peak"]), 60)
        title = (a.title or (a.name + " أقوى لحظة" if a.name else "أقوى لحظة")).strip()[:90] + f" #{i}"
        desc = f"لقطة من بث {a.name}" + (f"\nالمصدر: {a.url}" if a.url else "") + f"\n(الدقيقة {mm}:{ss:02d}) #Shorts #GTA6"
        if not a.no_upload and base and token:
            vid = upload_to_hub(base, token, out, title, desc)
            print(f"رُفعت المسودة {i}: {vid}")
    print("انتهى.")

if __name__ == "__main__":
    main()
