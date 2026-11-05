import os, sys, json, asyncio, subprocess, datetime, time, pathlib, re
import urllib.parse, urllib.request, urllib.error
import ssl, random
import httplib2
from googleapiclient.errors import HttpError
import edge_tts
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
def next_chunk_retry(req):
    retries = 0
    while True:
        try:
            status, resp = req.next_chunk()
            if status:
                log(f"Upload {int(status.progress() * 100)}%")
            return resp
        except Exception as e:
            retryable = isinstance(e, (OSError, ssl.SSLError, httplib2.HttpLib2Error)) or (
                isinstance(e, HttpError) and e.resp.status in (500, 502, 503, 504))
            retries += 1
            if not retryable or retries > 12:
                raise
            wait = min(2 ** retries, 60) + random.random()
            log(f"Upload error ({retries}): {e!r:.150} - {wait:.0f}s baad dobara")
            time.sleep(wait)

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
PIXABAY = (E("PIXABAY_API_KEY") or "").strip()   # free key: asli stock video clips
PEXELS = (E("PEXELS_API_KEY") or "").strip()     # free key (optional)
TTS_ENGINE = E("TTS_ENGINE", "auto").lower()     # auto | gemini | edge
GEM_VOICE = E("GEMINI_VOICE", "Charon")
GEM_TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
ELEVEN = (E("ELEVENLABS_API_KEY") or "").strip()
ELEVEN_VOICE = E("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
ELEVEN_MODEL = E("ELEVENLABS_MODEL", "eleven_v3")
SUB_FONT = None
USED_CLIPS = set()
CREDITS = set()
# Title / description / thumbnail / chapters ki zaban: roman (Roman Urdu) | english | urdu
TSTYLE = E("TITLE_STYLE", "roman").lower()
TDESC = {"roman": "Roman Urdu (English/Latin letters mein likhi Urdu, jaise: Is qile ka khofnak raaz)",
         "english": "simple English",
         }.get(TSTYLE, "Urdu script")
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
CUR_LAT = ["raaz", "raz", "kyun", "kaise", "kya ", "secret", "why", "how", "never", "hidden", "shocking",
           "mystery", "truth", "sach", "haqeeqat", "khofnak", "hairat", "ajeeb", "pehli baar", "koi nahi", "dangerous"]
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
    low = title.lower()
    if not (any(c in title for c in CUR) or any(c in low for c in CUR_LAT) or "؟" in title or "?" in title):
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
- title: max 55 characters, curiosity wala, {TDESC} mein (clickable, search-friendly)
- thumb_text: 2 se 4 alfaz, {TDESC} mein, title ke alfaz dohraye bina, jo title ko mukammal kare (title aur thumb_text ek jodi hain)
- hook: video ka pehla jumla (yeh narration hai, is liye {LANG} ki apni script mein), 6 se 14 alfaz, in formulas mein se koi ek: {formulas}
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
Har scene ka "chapter": 2 se 4 alfaz ka chhota unwan, {TDESC} mein.
"style": ek English line jo poore video ke visuals ka ek jaisa look tay kare (art style, colour palette, lighting).
"thumb_prompt": thumbnail ke liye English mein dramatic close-up prompt, no text in image.
{RULES}
Sirf JSON do, aur kuch nahi:
{{"title": "...", "thumb_text": "...", "description": "({TDESC} mein, 2-3 jumle, pehle jumle mein main keyword)", "tags": ["8-12 tags: English aur Roman Urdu dono ke search alfaz"],
 "style": "...", "thumb_prompt": "...",
 "scenes": [{{"narration": "...", "image_prompt": "English mein detailed visual prompt, no text in image", "video_query": "2-3 English alfaz stock-footage search ke liye (aam aur dekhne wali cheez, jaise: desert sunset, soldiers marching, old map)", "chapter": "..."}}]}}"""
            data = ask_json(prompt, True)
        else:
            prompt = f"""Video ka title: {data['title']}. Language: {LANG}.
Ab tak ka aakhri scene: {data['scenes'][-1]['narration']}
Isi kahani ko agle {n} scenes mein jaari rakho (~{words} alfaz har scene, aakhri part mein conclusion). Open loops band karte jao.
{end_rule}
{RULES}
Har scene ka "chapter": 2 se 4 alfaz, {TDESC} mein.
Sirf JSON: {{"scenes": [{{"narration": "...", "image_prompt": "English visual prompt", "video_query": "2-3 English alfaz stock-footage search ke liye", "chapter": "..."}}]}}"""
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


# ---------- 2. Voice + subtitles ----------
def ass_time(s):
    cs = int(round(s * 100))
    return f"{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}"


def make_chunks(cues):
    """Cues ko chhote tukron (short: 4, long: 7 alfaz) mein todta hai, waqt alfaz ki lambai ke hisab se."""
    n = 4 if KIND == "short" else 7
    words = []
    for a, b, t in cues:
        ws = t.split()
        if not ws:
            continue
        tot = sum(len(w) + 1 for w in ws)
        pos = a
        for w in ws:
            step = (b - a) * (len(w) + 1) / tot
            words.append((pos, pos + step, w))
            pos += step
    out = []
    for i in range(0, len(words), n):
        g = words[i:i + n]
        out.append([g[0][0], g[-1][1], " ".join(x[2] for x in g)])
    for i in range(len(out) - 1):
        out[i][1] = out[i + 1][0]
    return out


def write_ass(cues, path):
    """Subtitles: bari, mote, safed alfaz, kaali outline; Shorts mein neeche ke buttons se oopar."""
    short = KIND == "short"
    fs, mv, ol = (74, 400, 6) if short else (50, 70, 4)
    font = SUB_FONT or "Noto Naskh Arabic"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: D,{font},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{ol},3,2,80,80,{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        for a, b, t in make_chunks(cues):
            t = re.sub(r"[{}\\]", "", t)
            f.write(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},D,,0,0,0,,{t}\n")


async def tts_once(text, mp3, ass):
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
        write_ass(cues, ass)


def tts_edge(text, mp3, ass):
    for i in range(5):
        try:
            asyncio.run(tts_once(text, mp3, ass))
            return
        except Exception as e:
            log("TTS error:", repr(e))
            time.sleep(6 * (i + 1))
    raise RuntimeError("TTS fail")


def gemini_tts(text, mp3, ass):
    """Gemini ki qudrati awaaz (free tier). Koi bhi masla ho to exception, aur upar edge-tts le leta hai."""
    import base64
    last = None
    for model in GEM_TTS_MODELS:
        for attempt in range(3):
            try:
                r = post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                         f":generateContent?key={GEMINI}",
                         {"contents": [{"parts": [{"text": "Say in a calm, gripping documentary "
                                                    f"storyteller voice, natural pace: {text}"}]}],
                          "generationConfig": {"responseModalities": ["AUDIO"],
                                               "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig":
                                                                                {"voiceName": GEM_VOICE}}}}})
                pcm = base64.b64decode(r["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
                if len(pcm) < 24000:
                    raise RuntimeError("audio bohat chhota")
                (WORK / "tmp.pcm").write_bytes(pcm)
                if not ffmpeg(["-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "tmp.pcm",
                               "-c:a", "libmp3lame", "-q:a", "3", pathlib.Path(mp3).name]):
                    raise RuntimeError("pcm convert fail")
                d = dur(mp3)
                if d < 0.5:
                    raise RuntimeError("audio khali")
                write_ass([(0.0, d, text)], ass)
                return
            except urllib.error.HTTPError as e:
                last = f"{model} HTTP {e.code}"
                log("Gemini TTS:", last)
                if e.code == 429:
                    time.sleep(25 * (attempt + 1))
                elif e.code in (400, 404):
                    break
                else:
                    time.sleep(5)
            except Exception as e:
                last = f"{model} {e!r}"
                log("Gemini TTS:", last)
                time.sleep(5)
    raise RuntimeError("gemini tts fail: " + str(last))

def eleven_tts(text, mp3, ass):
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
           "?output_format=mp3_44100_128")
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, json.dumps({"text": text, "model_id": ELEVEN_MODEL}).encode(),
                {**UA, "Content-Type": "application/json", "xi-api-key": ELEVEN})
            data = urllib.request.urlopen(req, timeout=180).read()
            if len(data) < 5000:
                raise RuntimeError("audio chhota")
            pathlib.Path(mp3).write_bytes(data)
            d = dur(mp3)
            if d < 0.5:
                raise RuntimeError("audio khali")
            write_ass([(0.0, d, text)], ass)
            return
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:150]}"
            log("ElevenLabs:", last)
            if e.code in (401, 402, 403, 422):
                break
            time.sleep(5 * (attempt + 1))
        except Exception as e:
            last = repr(e)
            log("ElevenLabs:", last)
            time.sleep(5)
    raise RuntimeError("elevenlabs fail: " + str(last))
    
def voice_all(data):
    """Poori video ki awaaz ek hi engine se: pehle Gemini, koi bhi scene fail ho to saari edge-tts se."""
    scenes = data["scenes"]
    if TTS_ENGINE in ("auto", "eleven") and ELEVEN:
        try:
            for i, s in enumerate(scenes):
                eleven_tts(s["narration"], WORK / f"{i}.mp3", WORK / f"{i}.ass")
            log("voice: elevenlabs")
            return
        except Exception as e:
            log("ElevenLabs nahi chali, aage fallback:", repr(e)[:200])
            for f in WORK.glob("*.ass"):
                f.unlink()
    if TTS_ENGINE in ("auto", "gemini") and GEMINI:
        try:
            for i, s in enumerate(scenes):
                gemini_tts(s["narration"], WORK / f"{i}.mp3", WORK / f"{i}.ass")
                time.sleep(7)
            log("voice: gemini")
            return
        except Exception as e:
            log("Gemini voice nahi chali, edge-tts par:", repr(e)[:200])
            for f in WORK.glob("*.ass"):
                f.unlink()
    for i, s in enumerate(scenes):
        tts_edge(s["narration"], WORK / f"{i}.mp3", WORK / f"{i}.ass")
    log("voice: edge-tts")


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


def fetch_clip(q, path):
    """Asli stock video clip (Pexels ya Pixabay, free key). Na mile to False -> AI image."""
    short = KIND == "short"
    try:
        if PEXELS:
            u = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
                {"query": q, "per_page": 15, "orientation": "portrait" if short else "landscape", "size": "medium"})
            r = json.loads(urllib.request.urlopen(urllib.request.Request(
                u, headers={**UA, "Authorization": PEXELS}), timeout=60).read())
            for v in r.get("videos", []):
                if ("px", v["id"]) in USED_CLIPS:
                    continue
                files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width")]
                if not files:
                    continue
                files.sort(key=lambda f: abs(f["width"] - (1080 if short else 1920)))
                data = urllib.request.urlopen(urllib.request.Request(files[0]["link"], headers=UA), timeout=180).read()
                if len(data) > 50000:
                    path.write_bytes(data)
                    USED_CLIPS.add(("px", v["id"]))
                    CREDITS.add("Pexels")
                    return True
        if PIXABAY:
            u = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
                {"key": PIXABAY, "q": q, "per_page": 20, "safesearch": "true"})
            r = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=60).read())
            c = []
            for h in r.get("hits", []):
                v = h["videos"].get("medium") or h["videos"].get("large") or h["videos"].get("small")
                if v and v.get("url") and ("pb", h["id"]) not in USED_CLIPS:
                    c.append(((v["height"] > v["width"]) == short, h["id"], v["url"]))
            c.sort(key=lambda x: not x[0])
            for _, vid, url in c[:3]:
                data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180).read()
                if len(data) > 50000:
                    path.write_bytes(data)
                    USED_CLIPS.add(("pb", vid))
                    return True
    except Exception as e:
        log("clip skip:", repr(e)[:150])
    return False


def try_encode(i, attempts, has_ass, af, enc):
    for inp, vf in attempts:
        for subs in ([True, False] if has_ass else [False]):
            f = vf + (f",subtitles={i}.ass" if subs else "")
            if ffmpeg(inp + ["-vf", f] + af + enc + [f"{i}.mp4"]):
                return True
    return False


def build_video(data, font):
    clips, durs = [], []
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},format=yuv420p"
    # audio: aakhir ki khamoshi (dead air) kaato, 0.2s saans chhor do
    af = ["-af", "areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse,apad=pad_dur=0.2"]
    enc = ["-r", "25", "-c:v", "libx264", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest"]
    sty = data.get("style", "")
    voice_all(data)
    for i, s in enumerate(data["scenes"]):
        log(f"scene {i+1}/{len(data['scenes'])}")
        d = dur(WORK / f"{i}.mp3") or 10.0
        has_ass = (WORK / f"{i}.ass").exists()
        ok = False
        q = (s.get("video_query") or "").strip()
        if q and (PEXELS or PIXABAY) and fetch_clip(q, WORK / f"{i}.clip.mp4"):
            clip_in = ["-stream_loop", "-1", "-i", f"{i}.clip.mp4", "-i", f"{i}.mp3",
                       "-map", "0:v:0", "-map", "1:a:0", "-t", f"{d + 0.3:.2f}"]
            ok = try_encode(i, [(clip_in, base)], has_ass, af, enc)
            if not ok:
                log("clip encode fail, image par")
        if not ok:
            get_image((s["image_prompt"] + ", " + sty).strip(", "), WORK / f"{i}.jpg")
            frames = int(d * 25) + 10
            zin = f"{0.18 / frames:.7f}"
            z = f"1+{zin}*on" if i % 2 == 0 else f"1.18-{zin}*on"
            zp = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
                  f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps=25,"
                  f"format=yuv420p")
            once = ["-i", f"{i}.jpg", "-i", f"{i}.mp3", "-t", f"{d + 0.3:.2f}"]
            loop = ["-loop", "1", "-i", f"{i}.jpg", "-i", f"{i}.mp3"]
            attempts = ([(once, zp)] if ZOOM else []) + [(loop, base)]
            ok = try_encode(i, attempts, has_ass, af, enc)
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
    if "Pexels" in CREDITS:
        parts.append("Stock footage: Pexels (pexels.com)")
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
                             media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        resp = next_chunk_retry(req)
    log("Uploaded:", resp["id"], "publishAt", body["status"]["publishAt"])
    return resp["id"]


def set_thumb(yt, vid, data, font):
    """Long video ka thumbnail: AI image + bara text (title se alag). Channel verified ho to hi chalta hai."""
    try:
        get_image(data.get("thumb_prompt") or data["scenes"][0]["image_prompt"],
                  WORK / "thumb_bg.jpg", 1280, 720)
        text = re.sub(r"[{}\\\n]", " ", data["thumb_text"]).strip()
        latin = TSTYLE != "urdu"
        if latin:
            text = text.upper()
        tfont = "Noto Sans" if latin else (font or "Noto Naskh Arabic")
        ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T,{tfont},110,&H0000FFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,3,5,40,40,40,1

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
    SUB_FONT = font
    log("subtitle font:", font)
    video, durs = build_video(data, font)
    data["description"] = full_description(data, durs)
    vid = upload(yt, video, data)
    if KIND == "long" and data.get("thumb_text"):
        set_thumb(yt, vid, data, font)
    history.append(data["title"])
    HIST.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=1))
