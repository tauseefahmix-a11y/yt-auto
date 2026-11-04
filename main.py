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
ZOOM = E("KENBURNS", "1") == "1"            # slow zoom on images (0 = off)
TREND = E("TREND_SEARCH", "1") == "1"        # niche mein chalne wale titles dekhna (0 = off)
REL_LANG = E("RELEVANCE_LANG", "ur")
W, H = (1080, 1920) if KIND == "short" else (1920, 1080)
WORK = pathlib.Path("work") / KIND
WORK.mkdir(parents=True, exist_ok=True)
HIST = pathlib.Path("history.json")
history = json.loads(HIST.read_text()) if HIST.exists() else []
UA = {"User-Agent": "Mozilla/5.0"}
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"]

# 21 hook formulas (apni list, retention ke usool par)
HOOKS = [
    "curiosity gap (baat adhuri chhor do)", "shocking number ya fact", "sab ghalat maante hain (contrarian)",
    "viewer se seedha sawal", "chhupa hua raaz", "kahani ke beech se shuru", "na-mumkin dawa",
    "khatra / agar yeh na jana to", "pehle aur baad ka twist", "kya aap yaqeen karenge",
    "myth todna", "list tease (teen cheezein)", "mamnoo / chhupa hua / banned", "sirf ek raat mein",
    "do cheezon ka hairat angez muqabla", "ankahi paheli (unsolved mystery)", "aap ne kabhi socha",
    "ek zinda tasveer se shuru", "teen ka usool tease", "kamzor hero ki jeet", "anjam pehle, kahani baad mein",
]
CUR = ["راز", "کبھی", "کیوں", "کیسے", "حیرت", "سچ", "خوفناک", "چھپا", "پراسرار", "نامعلوم",
       "آج تک", "حقیقت", "عجیب", "خطرناک", "ناقابل", "کوئی نہیں"]
FILL = ["آج", "السلام", "خوش آمدید", "اس ویڈیو", "ہم آج", "دوستو", "ناظرین"]


def log(*a):
    print(*a, flush=True)


# ---------- 0. Hook score + title/thumbnail lint (dependency-free) ----------
def hook_score(h):
    h = h.strip()
    n = len(h.split())
    s = 30 if 5 <= n <= 14 else (15 if n <= 18 else 0)
    if any(h.startswith(f) for f in FILL):
        s -= 25
    s += min(40, 20 * sum(c in h for c in CUR))
    if "؟" in h or "?" in h:
        s += 15
    if any(x in h for x in ("لیکن", "مگر", "نہیں", "بجائے")):
        s += 10
    if any(x in h for x in ("آپ", "تم", "تمہیں")):
        s += 10
    return max(0, min(100, s))


def title_issues(title, thumb):
    iss = []
    if len(title) > 60:
        iss.append("title lamba")
    if not 1 <= len(thumb.split()) <= 4:
        iss.append("thumb alfaz")
    if set(re.findall(r"\w+", thumb)) & set(re.findall(r"\w+", title)):
        iss.append("thumb title dohra raha hai")
    if not any(c in title for c in CUR) and "؟" not in title and "?" not in title:
        iss.append("curiosity nahi")
    return iss


# ---------- 1. LLM ----------
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
                         {"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json"}})
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


def get_yt():
    creds = Credentials.from_authorized_user_info(json.loads(E("YT_TOKEN_JSON")))
    return build("youtube", "v3", credentials=creds)


def trending(yt):
    """Niche mein pichle 30 din ke sabse zyada dekhe gaye titles (sirf pattern samajhne ke liye)."""
    if not TREND:
        return []
    try:
        since = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = yt.search().list(part="snippet", q=NICHE[:60], type="video", order="viewCount",
                             publishedAfter=since, maxResults=10, relevanceLanguage=REL_LANG).execute()
        t = [i["snippet"]["title"] for i in r.get("items", [])]
        log("trend titles:", len(t))
        return t
    except Exception as e:
        log("trend search skip:", repr(e)[:200])
        return []


def make_idea(trend):
    trend_line = ("Niche mein haal hi mein chalne wale titles (sirf pattern samjho, copy mat karo): "
                  + json.dumps(trend, ensure_ascii=False)) if trend else ""
    formulas = "; ".join(HOOKS)
    prompt = f"""Channel niche: {NICHE}. Language: {LANG}. Format: {KIND}.
In purane topics repeat mat karo: {history[-40:]}
{trend_line}
3 alag topic ideas do. Har idea ke liye:
- title: max 55 characters, curiosity wala, {LANG} script mein
- thumb_text: 2 se 4 alfaz, title ke alfaz dohraye bina, jo title ko mukammal kare (title aur thumb_text ek jodi hain)
- hook: video ka pehla jumla, 6 se 14 alfaz, in formulas mein se koi ek: {formulas}
{RULES}
Sirf JSON: {{"ideas": [{{"title": "...", "thumb_text": "...", "hook": "...", "formula": "..."}}]}}"""
    ideas = ask_json(prompt).get("ideas", [])
    best, bs = None, -1
    for it in ideas:
        try:
            iss = title_issues(it["title"], it["thumb_text"])
            sc = hook_score(it["hook"]) + 100 - 25 * len(iss)
        except (KeyError, AttributeError):
            continue
        log(f"idea score {sc}: {it['title']} | hook: {it['hook']} | {iss}")
        if sc > bs:
            best, bs = it, sc
    if not best:
        raise RuntimeError("no idea")
    return best


def make_script(idea):
    if KIND == "short":
        parts, n, words = 1, 6, 25
        end_rule = "Aakhri jumla aisa likho ke pehle jumle se jurta lage (loop), aur viewer ko follow karne ko kaho."
    else:
        parts, n, words = 2, 12, 70
        end_rule = "Aakhir mein chhota sa nateeja do aur comments ke liye ek sawal poochho."
    if idea:
        topic = f"Title: {idea['title']}. Pehle scene ka pehla jumla bilkul yeh ho: {idea['hook']}"
    else:
        topic = f"Naya topic khud chuno. In purane topics repeat mat karo: {history[-40:]}"
    data = None
    for p in range(parts):
        if data is None:
            prompt = f"""Channel niche: {NICHE}. Language: {LANG}.
Naya {KIND} YouTube video likho. {topic}
Pacing: har do se teen scenes mein naya sawal, twist ya adhoori baat (open loop) rakho taake viewer ruka rahe. Faltu tamheed nahi. {end_rule}
Pehle {n} scenes do, har scene ki narration ~{words} alfaz.
Har scene ka "chapter": 2 se 4 alfaz ka chhota unwan.
"style": ek English line jo poore video ke visuals ka ek jaisa look tay kare (art style, colour palette, lighting).
"thumb_prompt": thumbnail ke liye English mein dramatic close-up prompt, no text in image.
{RULES}
Sirf JSON do, aur kuch nahi:
{{"title": "...", "thumb_text": "...", "description": "(Urdu script mein, 2-3 jumle, pehle jumle mein main keyword)", "tags": ["..."],
 "style": "...", "thumb_prompt": "...",
 "scenes": [{{"narration": "...", "image_prompt": "English mein detailed visual prompt, no text in image", "chapter": "..."}}]}}"""
            data = ask_json(prompt, True)
        else:
            prompt = f"""Video ka title: {data['title']}. Language: {LANG}.
Ab tak ka aakhri scene: {data['scenes'][-1]['narration']}
Isi kahani ko agle {n} scenes mein jaari rakho (~{words} alfaz har scene, aakhri part mein conclusion). Open loops band karte jao.
{end_rule}
{RULES}
Sirf JSON: {{"scenes": [{{"narration": "...", "image_prompt": "English visual prompt", "chapter": "..."}}]}}"""
            data["scenes"] += ask_json(prompt, True)["scenes"]
    if idea:
        data["title"], data["thumb_text"] = idea["title"], idea["thumb_text"]
        n0 = data["scenes"][0]["narration"]
        if idea["hook"][:12] not in n0:
            data["scenes"][0]["narration"] = idea["hook"] + " " + n0
    data.setdefault("description", "")
    data.setdefault("tags", [])
    data.setdefault("thumb_text", "")
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
def get_image(prompt, path, w=None, h=None):
    w, h = w or W, h or H
    q = urllib.parse.quote(prompt + ", cinematic, high quality")
    if PKEY:
        url = f"https://gen.pollinations.ai/image/{q}?width={w}&height={h}&nologo=true&key={PKEY}"
    else:
        url = f"https://image.pollinations.ai/prompt/{q}?width={w}&height={h}&nologo=true"
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


def dur(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
        return float(out.strip())
    except Exception:
        return 0.0


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


def build_video(data, font):
    clips, durs = [], []
    fs = 14 if KIND == "short" else 18
    style = f"Alignment=2,FontSize={fs},Outline=2,MarginV={120 if KIND=='short' else 40}"
    if font:
        style += f",FontName={font}"
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},format=yuv420p"
    # audio: aakhir ki khamoshi (dead air) kaato, 0.2s saans chhor do
    af = ["-af", "areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse,apad=pad_dur=0.2"]
    enc = ["-r", "25", "-c:v", "libx264", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest"]
    sty = data.get("style", "")
    for i, s in enumerate(data["scenes"]):
        log(f"scene {i+1}/{len(data['scenes'])}")
        get_image((s["image_prompt"] + ", " + sty).strip(", "), WORK / f"{i}.jpg")
        tts(s["narration"], WORK / f"{i}.mp3", WORK / f"{i}.srt")
        d = dur(WORK / f"{i}.mp3") or 10.0
        frames = int(d * 25) + 10
        zin = f"{0.18 / frames:.7f}"
        z = f"1+{zin}*on" if i % 2 == 0 else f"1.18-{zin}*on"
        zp = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps=25,"
              f"format=yuv420p")
        once = ["-i", f"{i}.jpg", "-i", f"{i}.mp3", "-t", f"{d + 0.3:.2f}"]
        loop = ["-loop", "1", "-i", f"{i}.jpg", "-i", f"{i}.mp3"]
        attempts = []
        if ZOOM:
            attempts += [(once, zp, True), (once, zp, False)]
        attempts += [(loop, base, True), (loop, base, False)]
        has_srt = (WORK / f"{i}.srt").exists()
        ok = False
        for inp, vf, subs in attempts:
            if subs and not has_srt:
                continue
            f = vf + (f",subtitles={i}.srt:force_style='{style}'" if subs else "")
            if ffmpeg(inp + ["-vf", f] + af + enc + [f"{i}.mp4"]):
                ok = True
                break
        if not ok:
            raise RuntimeError("ffmpeg fail scene %d" % i)
        clips.append(f"{i}.mp4")
        durs.append(dur(WORK / f"{i}.mp4") or d)
    (WORK / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips))
    if not ffmpeg(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "final.mp4"]):
        raise RuntimeError("concat fail")
    return WORK / "final.mp4", durs


# ---------- 5. SEO: chapters + description ----------
def ts(sec):
    s = int(sec)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def chapters(data, durs):
    if KIND != "long":
        return ""
    t, lines = 0.0, []
    for i, s in enumerate(data["scenes"]):
        name = s.get("chapter") or ("Intro" if i == 0 else "")
        if i % 3 == 0 and name:
            lines.append(f"{ts(t)} {name}")
        t += durs[i]
    return "\n".join(lines) if len(lines) >= 3 else ""


def full_description(data, durs):
    parts = [data["description"].strip()]
    ch = chapters(data, durs)
    if ch:
        parts.append(ch)
    tags = ["#" + re.sub(r"\s+", "", t) for t in data["tags"][:3] if t.strip()]
    if tags:
        parts.append(" ".join(tags))
    return "\n\n".join(p for p in parts if p)[:4900]


# ---------- 6. Upload + schedule + thumbnail ----------
def publish_at():
    h, m = map(int, PUB.split(":"))
    now = datetime.datetime.utcnow()
    t = now.replace(hour=(h - OFFSET) % 24, minute=m, second=0, microsecond=0)
    while t < now + datetime.timedelta(hours=1):
        t += datetime.timedelta(days=1)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def upload(yt, video, data):
    title = data["title"][:90] + (" #Shorts" if KIND == "short" else "")
    tags, n = [], 0
    for t in data["tags"]:
        if n + len(t) > 450:
            break
        tags.append(t)
        n += len(t) + 1
    body = {"snippet": {"title": title, "description": data["description"],
                        "tags": tags, "categoryId": "27"},
            "status": {"privacyStatus": "private", "publishAt": publish_at(),
                       "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    log("Uploaded:", resp["id"], "publishAt", body["status"]["publishAt"])
    return resp["id"]


def set_thumb(yt, vid, data, font):
    """Long video ka thumbnail: AI image + bara text (title se alag). Channel verified ho to hi chalta hai."""
    try:
        get_image(data.get("thumb_prompt") or data["scenes"][0]["image_prompt"],
                  WORK / "thumb_bg.jpg", 1280, 720)
        text = re.sub(r"[{}\\\n]", " ", data["thumb_text"]).strip()
        ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T,{font or 'Noto Naskh Arabic'},120,&H0000FFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,3,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,{text}
"""
        (WORK / "thumb.ass").write_text(ass, encoding="utf-8")
        vf = ("scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
              "subtitles=thumb.ass")
        if not ffmpeg(["-i", "thumb_bg.jpg", "-vf", vf, "-frames:v", "1", "-q:v", "2", "thumb.jpg"]):
            raise RuntimeError("thumb render fail")
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(
            str(WORK / "thumb.jpg"), mimetype="image/jpeg")).execute()
        log("Thumbnail set")
    except Exception as e:
        log("Thumbnail skip:", repr(e)[:300])


if __name__ == "__main__":
    yt = get_yt()
    try:
        idea = make_idea(trending(yt))
        log("Idea:", idea["title"], "|", idea["hook"])
    except Exception as e:
        log("idea step fail, purane tareeqe par:", repr(e)[:200])
        idea = None
    data = make_script(idea)
    log("Title:", data["title"])
    font = pick_font()
    log("subtitle font:", font)
    video, durs = build_video(data, font)
    data["description"] = full_description(data, durs)
    vid = upload(yt, video, data)
    if KIND == "long" and data.get("thumb_text"):
        set_thumb(yt, vid, data, font)
    history.append(data["title"])
    HIST.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=1))
