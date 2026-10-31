import os, sys, json, asyncio, subprocess, datetime, urllib.parse, urllib.request, time, pathlib, re

import edge_tts
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

KIND = sys.argv[1] if len(sys.argv) > 1 else "short"   # short | long
E = os.environ.get
NICHE = E("NICHE", "Amazing facts")
LANG = E("LANGUAGE", "Urdu")
VOICE = E("VOICE", "ur-PK-AsadNeural")
OFFSET = int(E("UTC_OFFSET_HOURS", "3"))
PUB = E("SHORT_PUBLISH_TIME", "18:00") if KIND == "short" else E("LONG_PUBLISH_TIME", "20:00")
W, H = (1080, 1920) if KIND == "short" else (1920, 1080)
WORK = pathlib.Path("work") / KIND
WORK.mkdir(parents=True, exist_ok=True)
HIST = pathlib.Path("history.json")
history = json.loads(HIST.read_text()) if HIST.exists() else []
UA = {"User-Agent": "Mozilla/5.0"}


# ---------- 1. Script (free: Pollinations text, no key) ----------
def llm(prompt):
    body = json.dumps({"model": "openai", "messages": [{"role": "user", "content": prompt}]}).encode()
    for _ in range(5):
        try:
            req = urllib.request.Request("https://text.pollinations.ai/openai", body,
                                         {**UA, "Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=180).read())
            return r["choices"][0]["message"]["content"]
        except Exception:
            time.sleep(8)
    raise RuntimeError("LLM fail")


def parse_json(t):
    m = re.search(r"\{.*\}", t, re.S)
    return json.loads(m.group(0))


def make_script():
    if KIND == "short":
        parts, n, words = 1, 6, 25
    else:
        parts, n, words = 2, 12, 70
    data = None
    for p in range(parts):
        if data is None:
            prompt = f"""Channel niche: {NICHE}. Language: {LANG}.
Naya {KIND} YouTube video likho. In purane topics repeat mat karo: {history[-40:]}
Pehle {n} scenes do, har scene ki narration ~{words} alfaz. Pehle scene mein strong hook.
Sirf JSON do, aur kuch nahi:
{{"title": "...", "description": "...", "tags": ["..."],
 "scenes": [{{"narration": "...", "image_prompt": "English mein detailed visual prompt, no text in image"}}]}}"""
            data = parse_json(llm(prompt))
        else:
            prompt = f"""Video ka title: {data['title']}. Language: {LANG}.
Ab tak ka aakhri scene: {data['scenes'][-1]['narration']}
Isi kahani ko agle {n} scenes mein jaari rakho (~{words} alfaz har scene, aakhri part mein conclusion).
Sirf JSON: {{"scenes": [{{"narration": "...", "image_prompt": "English visual prompt"}}]}}"""
            data["scenes"] += parse_json(llm(prompt))["scenes"]
    return data


# ---------- 2. Voice + subtitles (free: edge-tts) ----------
def srt_time(s):
    ms = int(s * 1000)
    return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}"


async def tts(text, mp3, srt):
    com = edge_tts.Communicate(text, VOICE)
    cues = []
    with open(mp3, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] in ("SentenceBoundary", "WordBoundary"):
                cues.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
    if cues:
        out, buf = [], []
        for c in cues:
            buf.append(c)
            if len(buf) == 4:
                out.append((buf[0][0], buf[-1][1], " ".join(x[2] for x in buf)))
                buf = []
        if buf:
            out.append((buf[0][0], buf[-1][1], " ".join(x[2] for x in buf)))
        # sentence boundary ho to har cue already poora jumla hai, group mat karo
        if any(len(c[2].split()) > 6 for c in cues):
            out = cues
        with open(srt, "w", encoding="utf-8") as f:
            for i, (a, b, t) in enumerate(out, 1):
                f.write(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n\n")


# ---------- 3. Images (free: Pollinations image, no key) ----------
def get_image(prompt, path):
    q = urllib.parse.quote(prompt + ", cinematic, high quality")
    url = f"https://image.pollinations.ai/prompt/{q}?width={W}&height={H}&nologo=true"
    for _ in range(5):
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()
            if len(data) > 5000:
                path.write_bytes(data)
                return
        except Exception:
            pass
        time.sleep(6)
    raise RuntimeError("image fail: " + prompt)


# ---------- 4. Video (free: FFmpeg) ----------
def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_video(data):
    clips = []
    fs = 14 if KIND == "short" else 18
    style = f"Alignment=2,FontSize={fs},Outline=2,MarginV={120 if KIND=='short' else 40}"
    for i, s in enumerate(data["scenes"]):
        get_image(s["image_prompt"], WORK / f"{i}.jpg")
        asyncio.run(tts(s["narration"], WORK / f"{i}.mp3", WORK / f"{i}.srt"))
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},format=yuv420p"
        if (WORK / f"{i}.srt").exists():
            vf += f",subtitles={i}.srt:force_style='{style}'"
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", f"{i}.jpg", "-i", f"{i}.mp3", "-vf", vf,
             "-r", "25", "-c:v", "libx264", "-c:a", "aac", "-ar", "44100", "-ac", "2",
             "-shortest", f"{i}.mp4"], cwd=WORK, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        clips.append(f"{i}.mp4")
    (WORK / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips))
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
                    "-c", "copy", "final.mp4"], cwd=WORK, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return WORK / "final.mp4"


# ---------- 5. Upload + schedule (free: YouTube Data API) ----------
def publish_at():
    h, m = map(int, PUB.split(":"))
    now = datetime.datetime.utcnow()
    t = now.replace(hour=(h - OFFSET) % 24, minute=m, second=0, microsecond=0)
    while t < now + datetime.timedelta(hours=1):
        t += datetime.timedelta(days=1)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def upload(video, data):
    creds = Credentials.from_authorized_user_info(json.loads(E("YT_TOKEN_JSON")))
    yt = build("youtube", "v3", credentials=creds)
    title = data["title"][:90] + (" #Shorts" if KIND == "short" else "")
    body = {"snippet": {"title": title, "description": data["description"],
                        "tags": data["tags"], "categoryId": "27"},
            "status": {"privacyStatus": "private", "publishAt": publish_at(),
                       "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Uploaded:", resp["id"], "publishAt", body["status"]["publishAt"])


if __name__ == "__main__":
    data = make_script()
    video = build_video(data)
    upload(video, data)
    history.append(data["title"])
    HIST.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=1))
