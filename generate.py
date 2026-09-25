#!/usr/bin/env python3
"""Ranný podcast: RSS -> Gemini (scenár + lekcia nemčiny) -> edge-tts (hlas) -> MP3 s kapitolami + feed.xml pre GitHub Pages.

Výstup ide do priečinka ./site, ktorý workflow nasadí na GitHub Pages.
Predchádzajúce epizódy a história naučených fráz sa pri každom behu stiahnu zo živej stránky (PAGES_URL).
Bez PAGES_URL beží lokálne a začína od nuly.
"""

import asyncio
import html
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import edge_tts
import feedparser
import requests
import yaml
from google import genai
from google.genai import types

TZ = ZoneInfo("Europe/Zurich")
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)
RETRY_WAITS = [0, 60, 180, 300]  # sekundy pred každým kolom pokusov
WEEKDAYS = ["pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa"]
RATE = 24000  # vzorkovanie všetkých WAV kúskov (musia byť rovnaké, aby sa dali zlepiť)
GAP = 0.8  # ticho medzi kapitolami, v sekundách
ORDINALS = ["Prvá", "Druhá", "Tretia", "Štvrtá", "Piata", "Šiesta", "Siedma", "Ôsma", "Deviata", "Desiata"]
REVIEW_PLAN = ((1, 3), (3, 2), (7, 2), (14, 1))  # (pred koľkými lekciami, koľko fráz z nej zopakovať)
KEEP_LESSONS = 100  # koľko posledných lekcií si história pamätá


def env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Chýba premenná prostredia {name}")
    return value


# ---------- 1. Zber správ ----------

def clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def google_news_urls(topic):
    searches = topic.get("google_news") or []
    urls = []
    for gn in ([searches] if isinstance(searches, dict) else searches):
        hl, gl = gn.get("hl", "sk"), gn.get("gl", "SK")
        q = quote(f'{gn["query"]} when:1d')
        urls.append(f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{hl}")
    return urls


def fetch_items(urls, since, seen_urls):
    items = []
    for url in urls:
        try:
            response = requests.get(url, headers={"User-Agent": "ranny-podcast/1.0"}, timeout=20)
        except requests.RequestException:
            print(f"  ! feed nefunguje: {url[:100]}")
            continue
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            print(f"  ! feed nefunguje: {url[:100]}")
            continue
        for e in feed.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            published = datetime(*ts[:6], tzinfo=timezone.utc) if ts else None
            link = e.get("link", "")
            if (published and published < since) or link in seen_urls:
                continue
            source = (e.get("source") or {}).get("title") or feed.feed.get("title", url)
            title = clean(e.get("title"), 200)
            if title.endswith(f" - {source}"):  # Google News pripája k titulku názov zdroja
                title = title[: -len(source) - 3]
            items.append({
                "title": title,
                "summary": clean(e.get("summary")),
                "source": source,
                "url": link,
                "published": published.isoformat() if published else "",
            })
    return items


def pick(items, limit, max_per_source, taken):
    """Od najnovších, bez duplicít (`taken` sú už použité titulky) a s limitom správ na vydavateľa."""
    picked, per_source = [], {}
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        title = it["title"].lower()
        if title in taken or per_source.get(it["source"], 0) >= max_per_source:
            continue
        taken.add(title)
        per_source[it["source"]] = per_source.get(it["source"], 0) + 1
        picked.append(it)
        if len(picked) == limit:
            break
    return picked


def collect(topic, since, seen_urls, ep):
    """Správy z vybraných feedov majú prednosť. Google News dopĺňa širší výber zo sveta s vlastným limitom."""
    per_source = ep.get("max_per_source", 6)
    taken = set()
    curated = pick(fetch_items(topic.get("feeds", []), since, seen_urls),
                   ep.get("max_items", 60), per_source, taken)
    searched = pick(fetch_items(google_news_urls(topic), since, seen_urls),
                    ep.get("max_search_items", 30), per_source, taken)
    return curated + searched


# ---------- 2. Scenár (Gemini) ----------

EPISODE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Krátky titulok (max 70 znakov) s 2–3 hlavnými správami dňa."},
        "intro": {"type": "string", "description": "Pozdrav s dňom a dátumom a jedna veta o tom, čo dnes zaznie."},
        "topics": {
            "type": "array",
            "description": "Jedna položka pre každú tému v danom poradí.",
            "items": {
                "type": "object",
                "properties": {"heading": {"type": "string"}, "text": {"type": "string"}},
                "required": ["heading", "text"],
            },
        },
        "outro": {"type": "string", "description": "Jedna-dve vety na rozlúčku."},
        "show_notes": {
            "type": "array",
            "description": "8–20 najdôležitejších použitých zdrojov.",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
            },
        },
    },
    "required": ["title", "intro", "topics", "outro", "show_notes"],
}

GERMAN_SCHEMA = {
    "type": "object",
    "properties": {
        "theme": {"type": "string", "description": "Téma dnešnej lekcie po slovensky, 2–5 slov."},
        "intro": {"type": "string", "description": "Jedna-dve vety po slovensky: čo sa dnes naučíme a načo sa to hodí."},
        "phrases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "de": {"type": "string", "description": "Fráza po nemecky."},
                    "sk": {"type": "string", "description": "Prirodzený slovenský ekvivalent."},
                    "tip": {"type": "string", "description": "1–2 vety po slovensky, bez nemeckých slov."},
                    "example_de": {"type": "string", "description": "Krátka ukážková veta s frázou."},
                    "example_sk": {"type": "string", "description": "Preklad ukážkovej vety."},
                },
                "required": ["de", "sk", "tip", "example_de", "example_sk"],
            },
        },
    },
    "required": ["theme", "intro", "phrases"],
}

SYSTEM = """Si moderátor krátkeho ranného podcastu pre jedného poslucháča: správy zo sveta a lekcia nemčiny.
Text bude čítať syntetický slovenský hlas, takže píšeš na počúvanie, nie na čítanie.

Pravidlá pre správy:
- Spisovná, ale hovorová slovenčina. Krátke vety, prirodzené prechody medzi správami.
- Polia intro, topics a outro majú spolu najviac {news_words} slov. Čas rozdeľ medzi témy podľa uvedených podielov.
- intro: krátky pozdrav s dňom a dátumom a jedna veta o tom, čo dnes zaznie{german_hint}.
- topics: presne jedna položka pre každú tému v danom poradí; heading je názov kapitoly uvedený pri téme.
- outro: jedna-dve vety na rozlúčku.
- Správy prichádzajú z médií a agentúr z celého sveta a v rôznych jazykoch (slovensky, anglicky, nemecky, ukrajinsky, poľsky, japonsky…). Prelož ich do slovenčiny.
- Zdrojov je veľa. Vyber to najdôležitejšie a neopieraj sa o jediný zdroj. Ak tú istú správu potvrdzuje viac nezávislých zdrojov, spomeň to. Ak sa zdroje rozchádzajú, povedz to a uveď obe verzie. Pri dôležitých tvrdeniach pomenuj zdroj (napr. „podľa Reuters“, „píše Kyiv Independent“).
- Uprednostni etablované a dôveryhodné zdroje (agentúry, verejnoprávne a renomované médiá). Správu z neznámeho, bulvárneho alebo agregátorského webu použi iba vtedy, ak ju potvrdzuje spoľahlivejší zdroj, inak ju výslovne označ ako nepotvrdenú.
- Používaj iba informácie z dodaných správ. Nič si nevymýšľaj. Ak k téme nie je nič podstatné, povedz to jednou vetou.
- Rumors a neoverené správy vždy výslovne označ a povedz, kto s nimi prišiel.
- Tvrdenia strán konfliktu pripisuj konkrétnej strane.
- Čísla, dátumy, meny a skratky vypíš slovami tak, ako sa vyslovujú. Anglické názvy produktov nechaj.
- Žiadny markdown, odrážky, emoji ani URL v texte.
- Neopakuj to, čo už bolo v predchádzajúcich epizódach, pokiaľ nie je podstatný posun."""

GERMAN_RULES = """

Pravidlá pre lekciu nemčiny (pole german):
- Poslucháč je Slovák, úroveň: {level}. Učí sa počúvaním a opakovaním nahlas. Slovenský hlas číta iba slovenský text, nemecké frázy číta samostatný nemecký hlas.
- Vyber {phrases} NOVÝCH fráz, ktoré sa v bežnom živote používajú najčastejšie (pozdravy, zdvorilosť, orientácia, jedlo, nakupovanie, doprava, zoznamovanie, práca). Frázy jednej lekcie patria k jednej téme. Postupuj od najčastejších a najjednoduchších k ťažším.
- de: celá fráza alebo krátka veta v štandardnej nemčine, správny pravopis a interpunkcia. sk: prirodzený slovenský ekvivalent (nie doslovný preklad, ak znie nepodarene).
- tip: 1–2 vety po slovensky o tom, kedy sa fráza používa (formálne/neformálne) alebo na čo dať pozor. V tipe nepíš nemecké slová.
- example_de a example_sk: jedna krátka ukážková veta s frázou a jej preklad.
- Nemecké slová nepíš do polí intro, topics ani outro.{notes}"""


def write_script(cfg, news, previous_titles, focus, taught, news_words):
    now = datetime.now(TZ)
    ep = cfg["episode"]
    german = cfg.get("german")
    topics = "\n".join(
        f'- {t["name"]} (kapitola „{t.get("chapter", t["name"])}“, ~{round(t["share"] * 100)} % času)'
        + (f': {t["notes"]}' if t.get("notes") else "")
        for t in cfg["topics"]
    )
    schema, system = EPISODE_SCHEMA, SYSTEM.format(
        news_words=news_words, german_hint=", vrátane lekcie nemčiny" if german else "")
    if german:
        schema = {**schema, "properties": {**schema["properties"], "german": GERMAN_SCHEMA},
                  "required": [*schema["required"], "german"]}
        system += GERMAN_RULES.format(
            level=german.get("level", "A1"),
            phrases=german.get("phrases", 6),
            notes=f"\n- {german['notes']}" if german.get("notes") else "",
        )
    prompt = (
        f"Dnes je {WEEKDAYS[now.weekday()]} {now.day}. {now.month}. {now.year}, {now:%H:%M}.\n\n"
        f"Témy v poradí:\n{topics}\n\n"
        + (f"Jednorazový dôraz pre túto epizódu: {focus}\n\n" if focus else "")
        + f"Titulky predchádzajúcich epizód: {json.dumps(previous_titles, ensure_ascii=False)}\n\n"
        + (f"Už naučené nemecké frázy (nezopakuj ich): {json.dumps(taught, ensure_ascii=False)}\n\n" if german else "")
        + f"Správy podľa tém (JSON):\n{json.dumps(news, ensure_ascii=False)}"
    )
    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        max_output_tokens=24000,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    # Bezplatné modely bývajú občas preťažené (503) -> skúšaj modely postupne, v niekoľkých kolách s čakaním.
    for wait in RETRY_WAITS:
        if wait:
            print(f"  … modely sú preťažené, čakám {wait // 60} min a skúsim znova")
            time.sleep(wait)
        for model in ep["models"]:
            try:
                response = client.models.generate_content(model=model, contents=prompt, config=config)
                script = json.loads(response.text)
                print(f"Model: {model}")
                return script
            except Exception as e:
                print(f"  ! {model}: {str(e)[:150]}")
    raise SystemExit("Gemini je momentálne nedostupné. Skús spustiť workflow neskôr.")


def pick_lesson(raw, cfg, lessons):
    """Z odpovede modelu ponechá iba frázy, ktoré ešte neboli, a oreže ich na nastavený počet."""
    if not raw:
        return None
    taught = {p["de"].lower() for lesson in lessons for p in lesson["phrases"]}
    limit = min(cfg["german"].get("phrases", 6), len(ORDINALS))
    fresh = [p for p in raw["phrases"] if p["de"].strip() and p["de"].lower() not in taught]
    return {**raw, "phrases": fresh[:limit]} if fresh else None


def pick_review(lessons):
    """Náhodne vyberie frázy z lekcií spred 1, 3, 7 a 14 lekcií (rozložené opakovanie)."""
    review = []
    for ago, count in REVIEW_PLAN:
        if ago <= len(lessons):
            phrases = lessons[ago - 1]["phrases"]
            review += random.sample(phrases, min(count, len(phrases)))
    return review


# ---------- 3. Audio (edge-tts + ffmpeg) ----------

def chunks(text, limit=2500):
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if current and len(current) + len(sentence) + 1 > limit:
            parts.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def tts(text, voice, rate, wav_path):
    mp3_path = wav_path.with_suffix(".mp3")
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            asyncio.run(communicate.save(str(mp3_path)))
            ffmpeg("-i", str(mp3_path), "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(wav_path))
            return
        except Exception as e:
            print(f"  ! TTS chyba ({e}), skúšam znova")
            time.sleep(10 * (attempt + 1))
    raise SystemExit("Hlas sa nepodarilo vygenerovať.")


def wav_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def ffmetadata(chapters, total):
    """Kapitoly vo formáte ffmpeg metadát. Každá trvá do začiatku ďalšej."""
    lines = [";FFMETADATA1"]
    ends = [start for start, _ in chapters[1:]] + [total]
    for (start, title), end in zip(chapters, ends):
        escaped = re.sub(r"([=;#\\\n])", r"\\\1", title)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(start * 1000)}", f"END={round(end * 1000)}",
                  f"title={escaped}"]
    return "\n".join(lines) + "\n"


class Track:
    """Skladá epizódu z replík a ticha a počíta si čas, aby vedela, kde začína ktorá kapitola.

    Hlas sa zadáva ako dvojica (názov hlasu, rýchlosť). Bez neho číta slovenský hlas.
    """

    def __init__(self, cfg, workdir):
        self.dir = workdir
        self.sk = (cfg["tts"]["voice"], cfg["tts"].get("rate", "+0%"))
        german = cfg.get("german", {})
        self.de = (german.get("voice", "de-DE-KatjaNeural"), "+0%")
        self.de_slow = (self.de[0], german.get("slow_rate", "-35%"))
        self.clips = []  # WAV kúsky v poradí prehrávania
        self.time = 0.0  # sekundy od začiatku epizódy
        self.last = 0.0  # dĺžka poslednej repliky, podľa nej sa počíta pauza na opakovanie
        self.chapters = []  # (sekundy, názov)
        self.cache = {}  # rovnaká replika sa syntetizuje iba raz

    def mark(self, title):
        self.chapters.append((self.time, title))

    def say(self, text, voice=None):
        voice = voice or self.sk
        for part in chunks(text):
            if (voice, part) not in self.cache:
                wav = self.dir / f"clip{len(self.clips):04d}.wav"
                tts(part, *voice, wav)
                self.cache[voice, part] = (wav, wav_seconds(wav))
            wav, seconds = self.cache[voice, part]
            self.clips.append(wav)
            self.time += seconds
            self.last = seconds

    def pause(self, seconds):
        frames = round(RATE * seconds)
        wav = self.dir / f"clip{len(self.clips):04d}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(bytes(2 * frames))
        self.clips.append(wav)
        self.time += frames / RATE

    def echo(self):
        """Ticho na zopakovanie práve vypočutej repliky nahlas."""
        self.pause(self.last * 1.3 + 0.8)

    def export(self, bitrate):
        listing = self.dir / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in self.clips))
        meta = self.dir / "chapters.txt"
        meta.write_text(ffmetadata(self.chapters, self.time), encoding="utf-8")
        mp3 = self.dir / "episode.mp3"
        ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(meta), "-map", "0:a",
               "-map_metadata", "1", "-map_chapters", "1", "-id3v2_version", "3",
               "-ac", "1", "-b:a", bitrate, str(mp3))
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(mp3)], capture_output=True, text=True, check=True)
        return mp3.read_bytes(), round(float(probe.stdout))


def sentence(text):
    text = text.strip()
    return text if text[-1:] in ".!?…" else text + "."


def quiz(track, phrases):
    """Slovensky zadanie, ticho na odpoveď, potom správna nemecká odpoveď."""
    for p in phrases:
        track.say(sentence(p["sk"]))
        track.pause(2.5 + 0.7 * len(p["de"].split()))
        track.say(p["de"], track.de)
        track.pause(0.8)


def add_lesson(track, cfg, lesson, review):
    track.mark(cfg["german"].get("chapter", "Nemčina"))
    track.say(f"Teraz nemčina. Dnešná téma: {lesson['theme']}. {lesson['intro']} "
              "Po každej nemeckej fráze bude chvíľa ticha. Zopakuj ju nahlas.")
    if review:
        track.say("Najprv opakovanie. Skús povedať po nemecky:")
        quiz(track, review)
    track.say("A teraz nové frázy.")
    for i, p in enumerate(lesson["phrases"]):
        track.say(f"{ORDINALS[i]} fráza: {sentence(p['sk'])} Po nemecky:")
        track.say(p["de"], track.de)
        track.echo()
        track.say(p["de"], track.de_slow)
        track.echo()
        track.say(p["tip"])
        track.say("Napríklad:")
        track.say(p["example_de"], track.de)
        track.say(p["example_sk"])
        track.pause(GAP)
    track.say("Overme si, čo sme sa naučili. Ako sa povie po nemecky:")
    quiz(track, random.sample(lesson["phrases"], len(lesson["phrases"])))


def build_audio(script, lesson, review, cfg, workdir):
    track = Track(cfg, workdir)
    track.mark("Úvod")
    track.say(script["intro"])
    for topic in script["topics"]:
        track.pause(GAP)
        track.mark(topic["heading"])
        track.say(topic["text"])
    if lesson:
        track.pause(GAP)
        add_lesson(track, cfg, lesson, review)
    track.pause(GAP)
    track.say(script["outro"])
    audio, duration = track.export(cfg["tts"].get("bitrate", "64k"))
    return audio, duration, track.chapters


def spoken_words(script):
    texts = [script["intro"], script["outro"], *(t["text"] for t in script["topics"])]
    return sum(len(text.split()) for text in texts)


# ---------- 4. Stránka (GitHub Pages) + feed ----------

SITE = Path("site")


def fetch_json(base, name):
    """Stiahne JSON zo živej stránky. Bez stránky alebo súboru vráti prázdny zoznam."""
    if not base:
        return []
    try:
        r = requests.get(f"{base}/{name}", params={"t": int(time.time())}, timeout=30)
        return r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        return []


def load_previous(base, keep):
    """Stiahne zoznam a MP3 predchádzajúcich epizód zo živej stránky."""
    kept = []
    for ep in fetch_json(base, "episodes.json")[: keep - 1]:
        try:
            r = requests.get(f"{base}/{ep['key']}", timeout=120)
        except requests.RequestException:
            continue
        if r.ok:
            path = SITE / ep["key"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            kept.append(ep)
    print(f"Predchádzajúce epizódy: {len(kept)}")
    return kept


def show_notes_html(script, lesson):
    links = "".join(
        f'<li><a href="{html.escape(n["url"], quote=True)}">{html.escape(n["title"])}</a></li>'
        for n in script["show_notes"]
    )
    notes = f"<p>Zdroje:</p><ul>{links}</ul>"
    if lesson:
        phrases = "".join(f"<li>{html.escape(p['de'])} – {html.escape(p['sk'])}</li>" for p in lesson["phrases"])
        notes = f"<p>Nemčina – {html.escape(lesson['theme'])}:</p><ul>{phrases}</ul>" + notes
    return notes


def build_feed(cfg, episodes, base):
    p = cfg["podcast"]
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = p["title"]
    ET.SubElement(ch, "link").text = f"{base}/feed.xml"
    ET.SubElement(ch, "language").text = p.get("language", "sk")
    ET.SubElement(ch, "description").text = p.get("description", "Súkromný ranný súhrn správ.")
    ET.SubElement(ch, f"{{{ITUNES}}}author").text = p["title"]
    ET.SubElement(ch, f"{{{ITUNES}}}explicit").text = "false"
    ET.SubElement(ch, f"{{{ITUNES}}}block").text = "Yes"
    if p.get("cover"):
        cover_url = f"{base}/{Path(p['cover']).name}"
        ET.SubElement(ch, f"{{{ITUNES}}}image", {"href": cover_url})
        image = ET.SubElement(ch, "image")  # pre aplikácie, ktoré itunes:image nečítajú
        ET.SubElement(image, "url").text = cover_url
        ET.SubElement(image, "title").text = p["title"]
        ET.SubElement(image, "link").text = f"{base}/feed.xml"
    for ep in episodes:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = ep["title"]
        ET.SubElement(item, "description").text = ep["description"]
        ET.SubElement(item, "pubDate").text = ep["pub_date"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = ep["guid"]
        ET.SubElement(item, "enclosure", {"url": f"{base}/{ep['key']}", "length": str(ep["length"]),
                                          "type": "audio/mpeg"})
        ET.SubElement(item, f"{{{ITUNES}}}duration").text = str(ep["duration"])
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


# ---------- main ----------

def make_episode(cfg, news, previous_titles, focus, lessons):
    """Vygeneruje scenár a audio. Ak epizóda prekročí limit, skráti scenár a skúsi to ešte raz."""
    limit = cfg["episode"]["max_minutes"] * 60
    news_words = cfg["episode"]["news_words"]
    taught = [p["de"] for lesson in lessons for p in lesson["phrases"]]
    review = pick_review(lessons)
    for attempt in range(2):
        script = write_script(cfg, news, previous_titles, focus, taught, news_words)
        lesson = pick_lesson(script.get("german"), cfg, lessons) if cfg.get("german") else None
        words = spoken_words(script)
        print(f"Scenár: {script['title']} ({words} slov)")
        with tempfile.TemporaryDirectory() as tmp:
            audio, duration, chapters = build_audio(script, lesson, review if lesson else [], cfg, Path(tmp))
        if duration <= limit or attempt:
            if duration > limit:
                print(f"  ! epizóda je dlhšia ako limit ({duration // 60}:{duration % 60:02d})")
            return script, lesson, audio, duration, chapters
        lesson_seconds = duration - chapters[-1][0] if lesson else 0  # lekcia má pevnú dĺžku, krátia sa správy
        news_words = max(300, int(words * (limit - lesson_seconds) / (duration - lesson_seconds) * 0.95))
        print(f"  … epizóda má {duration // 60}:{duration % 60:02d}, limit je {limit // 60}:00. "
              f"Skúšam scenár na {news_words} slov")


def main():
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    focus = os.environ.get("FOCUS", "").strip()
    on_demand = os.environ.get("RUN_KIND") == "workflow_dispatch"
    base = os.environ.get("PAGES_URL", "").rstrip("/")
    SITE.mkdir(exist_ok=True)
    keep = cfg["podcast"].get("keep_episodes", 14)
    episodes = load_previous(base, keep)
    lessons = fetch_json(base, "german.json") if cfg.get("german") else []

    since = datetime.now(timezone.utc) - timedelta(hours=cfg["episode"].get("lookback_hours", 24))
    seen = {u for ep in episodes[:3] for u in ep.get("used_urls", [])}
    news = {t["name"]: collect(t, since, seen, cfg["episode"]) for t in cfg["topics"]}
    print("Počet správ:", {name: len(items) for name, items in news.items()})
    if not any(news.values()):
        raise SystemExit("Žiadne nové správy, epizódu negenerujem.")

    script, lesson, audio, duration, chapters = make_episode(
        cfg, news, [ep["title"] for ep in episodes[:3]], focus, lessons)
    print("Kapitoly:", ", ".join(f"{int(start) // 60}:{int(start) % 60:02d} {title}" for start, title in chapters))

    now = datetime.now(TZ)
    guid = str(uuid.uuid4())
    key = f"episodes/{now:%Y-%m-%d-%H%M}-{guid[:8]}.mp3"
    (SITE / "episodes").mkdir(exist_ok=True)
    (SITE / key).write_bytes(audio)
    if not base:
        (SITE / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        base = SITE.resolve().as_uri()
    if cfg["podcast"].get("cover"):
        shutil.copyfile(cfg["podcast"]["cover"], SITE / Path(cfg["podcast"]["cover"]).name)

    if cfg.get("german"):  # stránka sa nasadzuje celá odznova, história sa musí zapísať pri každom behu
        if lesson:
            lessons.insert(0, {"date": f"{now:%Y-%m-%d}", "theme": lesson["theme"],
                               "phrases": [{"de": p["de"], "sk": p["sk"]} for p in lesson["phrases"]]})
        (SITE / "german.json").write_text(json.dumps(lessons[:KEEP_LESSONS], ensure_ascii=False, indent=1),
                                          encoding="utf-8")

    date = f"{now.day}. {now.month}." + (f" {now:%H:%M}" if on_demand else "")
    episodes.insert(0, {
        "guid": guid,
        "title": f"{date} – {script['title']}",
        "description": show_notes_html(script, lesson),
        "pub_date": format_datetime(now),
        "key": key,
        "length": len(audio),
        "duration": duration,
        "used_urls": [it["url"] for items in news.values() for it in items],
    })
    (SITE / "episodes.json").write_text(json.dumps(episodes, ensure_ascii=False, indent=1), encoding="utf-8")
    (SITE / "feed.xml").write_bytes(build_feed(cfg, episodes, base))
    (SITE / "index.html").write_text(f'<meta charset="utf-8"><p>{html.escape(cfg["podcast"]["title"])}: '
                                     f'<a href="feed.xml">feed.xml</a></p>', encoding="utf-8")
    print(f"Hotovo: {episodes[0]['title']} ({duration // 60}:{duration % 60:02d}) -> {base}/feed.xml")


if __name__ == "__main__":
    main()
