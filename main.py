import os, sys, json, asyncio, subprocess, datetime, time, pathlib, re
import urllib.parse, urllib.request, urllib.error

import edge_tts
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


def E(k, d=None):
    v = os.environ.get(k)
    return v if v else d


KIND = sys.argv[1] if len(sys.argv) > 1 else "short"   # short | long
NICHE = E("NICHE", "Hairat angez facts aur history ki kahaniyan")
LANG = E("LANGUAGE", "Urdu")
VOICE = E("VOICE", "ur-PK-UzmaNeural")
OFFSET = int(E("UTC_OFFSET_HOURS", "3"))
PUB = E("SHORT_PUBLISH_TIME", "18:00") if KIND == "short" else E("LONG_PUBLISH_TIME", "20:00")
GEMINI = (E("GEMINI_API_KEY") or "").strip()
PKEY = (E("POLLINATIONS_API_KEY") or "").strip()
W, H = (1080, 1920) if KIND == "short" else (1920, 1080)
WORK = pathlib.Path("work") / KIND
WORK.mkdir(parents=True, exist_ok=True)
HIST = pathlib.Path("history.json")
history = json.loads(HIST.read_text()) if HIST.exists() else []
UA = {"User-Agent": "Mozilla/5.0"}
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"]


def log(*a):
    print(*a, flush=True)


# ---------- 1. Script ----------
def post(url, payload, headers=None):
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {**UA, "Content-Type": "application/json", **(headers or {})})
    return json.loads(urllib.request.urlopen(req, timeout=240).read())


def llm(prompt):
    last = None
    if GEMINI:
        for attempt in range(10):
            model = GEMINI_MODELS[attempt % len(GEMINI_MODELS)]
            try:
                r = post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                         f":generateContent?key={GEMINI}",
                         {"contents": [{"parts": [{"text": prompt}]}]})
                return r["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                last = f"{model} HTTP {e.code}: {e.read()[:200]}"
            except Exception as e:
                last = f"{model} {e!r}"
            log("LLM error:", last)
            time.sleep(min(15 * (attempt + 1), 60))
    if PKEY:
        try:
            r = post("https://gen.pollinations.ai/v1/chat/completions",
                     {"model": "openai", "messages": [{"role": "user", "content": prompt}]},
                     {"Authorization": "Bearer " + PKEY})
            return r["choices"][0]["message"]["content"]
        except Exception as e:
            last = f"pollinations {e!r}"
            log("LLM error:", last)
    raise RuntimeError("LLM fail: " + str(last))


def parse_json(t):
    m = re.search(r"\{.*\}", t, re.S)
    return json.loads(m.group(0))


def lang_ok(d):
    """Urdu ho to narration sirf Urdu script mein honi chahiye (Roman/Hindi nahi)."""
    if LANG.lower() != "urdu":
        return True
    text = " ".join(x.get("narration", "") for x in d.get("scenes", []))
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    arabic = sum("\u0600" <= c <= "\u06ff" or "\u0750" <= c <= "\u077f" for c in letters)
    return arabic / len(letters) >= 0.92


def ask_json(prompt, check=False):
    last = None
    for i in range(5):
        try:
            d = parse_json(llm(prompt))
        except json.JSONDecodeError as e:
            log("JSON parse error, retry:", e)
            continue
        last = d
        if check and not lang_ok(d):
            log("Narration Urdu script mein nahi, dobara try")
            continue
        return d
    if last:
        return last
    raise RuntimeError("JSON parse fail")


RULES = f"""Narration sirf {LANG} ki asli zaban aur uski apni script mein likho (Urdu ho to Urdu script: اردو، Roman/English letters ya Hindi/Devanagari mein bilkul nahi). Saaf, aasaan, bol-chaal wali zaban, chhote jumle. Har jumle ke aakhir mein poora stop. Numbers ko alfaz mein likho (jaise 1990 ki jagah poora lafz mein). English alfaz, brackets, emoji, hashtags aur special symbols narration mein mat daalo. Narration ke andar double quotes (\") istemal mat karo."""


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
{RULES}
Sirf JSON do, aur kuch nahi:
{{"title": "...", "description": "(Urdu script mein)", "tags": ["..."],
 "scenes": [{{"narration": "...", "image_prompt": "English mein detailed visual prompt, no text in image"}}]}}"""
            data = ask_json(prompt, True)
        else:
            prompt = f"""Video ka title: {data['title']}. Language: {LANG}.
Ab tak ka aakhri scene: {data['scenes'][-1]['narration']}
Isi kahani ko agle {n} scenes mein jaari rakho (~{words} alfaz har scene, aakhri part mein conclusion).
{RULES}
Sirf JSON: {{"scenes": [{{"narration": "...", "image_prompt": "English visual prompt"}}]}}"""
            data["scenes"] += ask_json(prompt, True)["scenes"]
    return data


# ---------- 2. Voice + subtitles (edge-tts) ----------
def srt_time(s):
    ms = int(s * 1000)
    return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}"


async def tts_once(text, mp3, srt):
    com = edge_tts.Communicate(text, VOICE)
    cues = []
    with open(mp3, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] in ("SentenceBoundary", "WordBoundary"):
                cues.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
    if not pathlib.Path(mp3).stat().st_size:
        raise RuntimeError("empty audio")
    if cues:
        if any(len(c[2].split()) > 6 for c in cues):
            out = cues
        else:
            out, buf = [], []
            for c in cues:
                buf.append(c)
                if len(buf) == 4:
                    out.append((buf[0][0], buf[-1][1], " ".join(x[2] for x in buf)))
                    buf = []
            if buf:
                out.append((buf[0][0], buf[-1][1], " ".join(x[2] for x in buf)))
        with open(srt, "w", encoding="utf-8") as f:
            for i, (a, b, t) in enumerate(out, 1):
                f.write(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n\n")


def tts(text, mp3, srt):
    for i in range(5):
        try:
            asyncio.run(tts_once(text, mp3, srt))
            return
        except Exception as e:
            log("TTS error:", repr(e))
            time.sleep(6 * (i + 1))
    raise RuntimeError("TTS fail")


# ---------- 3. Images ----------
def get_image(prompt, path):
    q = urllib.parse.quote(prompt + ", cinematic, high quality")
    if PKEY:
        url = f"https://gen.pollinations.ai/image/{q}?width={W}&height={H}&nologo=true&key={PKEY}"
    else:
        url = f"https://image.pollinations.ai/prompt/{q}?width={W}&height={H}&nologo=true"
    last = None
    for _ in range(6):
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180).read()
            if len(data) > 5000:
                path.write_bytes(data)
                return
            last = "small response"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:
            last = repr(e)
        log("Image error:", last)
        time.sleep(10)
    raise RuntimeError("image fail: " + str(last))


# ---------- 4. Video ----------
def ffmpeg(args):
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + args, cwd=WORK,
                       capture_output=True, text=True)
    if r.returncode:
        log("ffmpeg error:", r.stderr[-600:])
    return r.returncode == 0


def pick_font():
    try:
        out = subprocess.run(["fc-list", ":lang=ur", "family"], capture_output=True, text=True).stdout
        fams = [l.split(",")[0].strip() for l in out.splitlines() if l.strip()]
        for pref in ("Noto Naskh Arabic", "Noto Sans Arabic", "Noto Nastaliq Urdu"):
            if pref in fams:
                return pref
        return fams[0] if fams else None
    except Exception:
        return None


def build_video(data):
    clips = []
    fs = 14 if KIND == "short" else 18
    font = pick_font()
    log("subtitle font:", font)
    style = f"Alignment=2,FontSize={fs},Outline=2,MarginV={120 if KIND=='short' else 40}"
    if font:
        style += f",FontName={font}"
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},format=yuv420p"
    enc = ["-r", "25", "-c:v", "libx264", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest"]
    for i, s in enumerate(data["scenes"]):
        log(f"scene {i+1}/{len(data['scenes'])}")
        get_image(s["image_prompt"], WORK / f"{i}.jpg")
        tts(s["narration"], WORK / f"{i}.mp3", WORK / f"{i}.srt")
        inp = ["-loop", "1", "-i", f"{i}.jpg", "-i", f"{i}.mp3"]
        ok = False
        if (WORK / f"{i}.srt").exists():
            ok = ffmpeg(inp + ["-vf", base + f",subtitles={i}.srt:force_style='{style}'"] + enc + [f"{i}.mp4"])
        if not ok:
            ok = ffmpeg(inp + ["-vf", base] + enc + [f"{i}.mp4"])
        if not ok:
            raise RuntimeError("ffmpeg fail scene %d" % i)
        clips.append(f"{i}.mp4")
    (WORK / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips))
    if not ffmpeg(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "final.mp4"]):
        raise RuntimeError("concat fail")
    return WORK / "final.mp4"


# ---------- 5. Upload + schedule ----------
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
    log("Uploaded:", resp["id"], "publishAt", body["status"]["publishAt"])


if __name__ == "__main__":
    data = make_script()
    log("Title:", data["title"])
    video = build_video(data)
    upload(video, data)
    history.append(data["title"])
    HIST.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=1))
