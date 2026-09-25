#!/usr/bin/env python3
"""Ranný podcast: RSS -> Gemini (scenár) -> edge-tts (hlas) -> MP3 + feed.xml pre GitHub Pages.

Výstup ide do priečinka ./site, ktorý workflow nasadí na GitHub Pages.
Predchádzajúce epizódy sa pri každom behu stiahnu zo živej stránky (PAGES_URL).
Bez PAGES_URL beží lokálne a začína od nuly.
"""

import asyncio
import html
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
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
WEEKDAYS = ["pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa"]


def env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Chýba premenná prostredia {name}")
    return value


# ---------- 1. Zber správ ----------

def clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def topic_feed_urls(topic):
    urls = list(topic.get("feeds", []))
    gn = topic.get("google_news")
    if gn:
        hl, gl = gn.get("hl", "sk"), gn.get("gl", "SK")
        q = quote(f'{gn["query"]} when:1d')
        urls.append(f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{hl}")
    return urls


def collect(topic, since, seen_urls, max_items=40):
    items = []
    for url in topic_feed_urls(topic):
        feed = feedparser.parse(url, agent="ranny-podcast/1.0")
        if feed.bozo and not feed.entries:
            print(f"  ! feed nefunguje: {url}")
            continue
        for e in feed.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            published = datetime(*ts[:6], tzinfo=timezone.utc) if ts else None
            link = e.get("link", "")
            if (published and published < since) or link in seen_urls:
                continue
            source = (e.get("source") or {}).get("title") or feed.feed.get("title", url)
            items.append({
                "title": clean(e.get("title"), 200),
                "summary": clean(e.get("summary")),
                "source": source,
                "url": link,
                "published": published.isoformat() if published else "",
            })
    unique = {}
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        unique.setdefault(it["title"].lower(), it)
    return list(unique.values())[:max_items]


# ---------- 2. Scenár (Gemini) ----------

EPISODE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Krátky titulok (max 70 znakov) s 2–3 hlavnými témami dňa."},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"heading": {"type": "string"}, "text": {"type": "string"}},
                "required": ["heading", "text"],
            },
        },
        "show_notes": {
            "type": "array",
            "description": "5–15 najdôležitejších použitých zdrojov.",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
            },
        },
    },
    "required": ["title", "sections", "show_notes"],
}

SYSTEM = """Si moderátor krátkeho ranného spravodajského podcastu pre jedného poslucháča.
Text bude čítať syntetický slovenský hlas, takže píšeš na počúvanie, nie na čítanie.

Pravidlá:
- Spisovná, ale hovorová slovenčina. Krátke vety, prirodzené prechody medzi správami.
- Spolu najviac {max_words} slov. Čas rozdeľ medzi témy podľa uvedených podielov.
- Prvá sekcia: krátky pozdrav s dňom a dátumom a jedna veta o tom, čo dnes zaznie.
  Potom jedna sekcia pre každú tému v danom poradí. Posledná sekcia: jedna-dve vety na rozlúčku.
- Používaj iba informácie z dodaných správ. Nič si nevymýšľaj. Ak k téme nie je nič podstatné, povedz to jednou vetou.
- Rumors a neoverené správy vždy výslovne označ a povedz, kto s nimi prišiel.
- Tvrdenia strán konfliktu pripisuj konkrétnej strane.
- Čísla, dátumy, meny a skratky vypíš slovami tak, ako sa vyslovujú. Anglické názvy produktov nechaj.
- Žiadny markdown, odrážky, emoji ani URL v texte sekcií.
- Neopakuj to, čo už bolo v predchádzajúcich epizódach, pokiaľ nie je podstatný posun."""


def write_script(cfg, news, previous_titles, focus):
    now = datetime.now(TZ)
    ep = cfg["episode"]
    topics = "\n".join(
        f'- {t["name"]} (~{round(t["share"] * 100)} % času)' + (f': {t["notes"]}' if t.get("notes") else "")
        for t in cfg["topics"]
    )
    prompt = (
        f"Dnes je {WEEKDAYS[now.weekday()]} {now.day}. {now.month}. {now.year}, {now:%H:%M}.\n\n"
        f"Témy v poradí:\n{topics}\n\n"
        + (f"Jednorazový dôraz pre túto epizódu: {focus}\n\n" if focus else "")
        + f"Titulky predchádzajúcich epizód: {json.dumps(previous_titles, ensure_ascii=False)}\n\n"
        f"Správy podľa tém (JSON):\n{json.dumps(news, ensure_ascii=False)}"
    )
    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    models = [ep.get("model", "gemini-3.8-flash")] + ([ep["fallback_model"]] if ep.get("fallback_model") else [])
    for i, model in enumerate(models):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM.format(max_words=ep["max_words"]),
                    response_mime_type="application/json",
                    response_json_schema=EPISODE_SCHEMA,
                    max_output_tokens=16000,
                ),
            )
            script = json.loads(response.text)
            print(f"Model: {model}")
            return script
        except Exception as e:  # limit, výpadok alebo nevalidný JSON -> skús záložný model
            print(f"  ! {model} zlyhal: {e}")
            if i == len(models) - 1:
                raise


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


def tts(text, cfg, wav_path):
    mp3_path = wav_path.with_suffix(".mp3")
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(text, cfg["tts"]["voice"], rate=cfg["tts"].get("rate", "+0%"))
            asyncio.run(communicate.save(str(mp3_path)))
            ffmpeg("-i", str(mp3_path), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path))
            return
        except Exception as e:
            print(f"  ! TTS chyba ({e}), skúšam znova")
            time.sleep(10 * (attempt + 1))
    raise SystemExit("Hlas sa nepodarilo vygenerovať.")


def build_audio(script, cfg, workdir):
    pause = workdir / "pause.wav"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.8", "-c:a", "pcm_s16le", str(pause))
    wavs = []
    for i, section in enumerate(script["sections"]):
        for j, part in enumerate(chunks(section["text"])):
            wav = workdir / f"s{i:02d}_{j:02d}.wav"
            tts(part, cfg, wav)
            wavs.append(wav)
        wavs.append(pause)
    listing = workdir / "list.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in wavs))
    mp3 = workdir / "episode.mp3"
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-ac", "1",
           "-b:a", cfg["tts"].get("bitrate", "64k"), str(mp3))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                           capture_output=True, text=True, check=True)
    return mp3.read_bytes(), round(float(probe.stdout))


# ---------- 4. Stránka (GitHub Pages) + feed ----------

SITE = Path("site")


def load_previous(base, keep):
    """Stiahne zoznam a MP3 predchádzajúcich epizód zo živej stránky."""
    if not base:
        return []
    try:
        r = requests.get(f"{base}/episodes.json", params={"t": int(time.time())}, timeout=30)
        episodes = r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        episodes = []
    kept = []
    for ep in episodes[: keep - 1]:
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


def show_notes_html(script):
    links = "".join(
        f'<li><a href="{html.escape(n["url"], quote=True)}">{html.escape(n["title"])}</a></li>'
        for n in script["show_notes"]
    )
    return f"<p>Zdroje:</p><ul>{links}</ul>"


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
    if p.get("cover_url"):
        ET.SubElement(ch, f"{{{ITUNES}}}image", {"href": p["cover_url"]})
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

def main():
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    focus = os.environ.get("FOCUS", "").strip()
    on_demand = os.environ.get("RUN_KIND") == "workflow_dispatch"
    base = os.environ.get("PAGES_URL", "").rstrip("/")
    SITE.mkdir(exist_ok=True)
    keep = cfg["podcast"].get("keep_episodes", 14)
    episodes = load_previous(base, keep)

    since = datetime.now(timezone.utc) - timedelta(hours=cfg["episode"].get("lookback_hours", 24))
    seen = {u for ep in episodes[:3] for u in ep.get("used_urls", [])}
    news = {t["name"]: collect(t, since, seen) for t in cfg["topics"]}
    print("Počet správ:", {name: len(items) for name, items in news.items()})
    if not any(news.values()):
        raise SystemExit("Žiadne nové správy, epizódu negenerujem.")

    script = write_script(cfg, news, [ep["title"] for ep in episodes[:3]], focus)
    words = sum(len(s["text"].split()) for s in script["sections"])
    print(f"Scenár: {script['title']} ({words} slov)")

    with tempfile.TemporaryDirectory() as tmp:
        audio, duration = build_audio(script, cfg, Path(tmp))

    now = datetime.now(TZ)
    guid = str(uuid.uuid4())
    key = f"episodes/{now:%Y-%m-%d-%H%M}-{guid[:8]}.mp3"
    (SITE / "episodes").mkdir(exist_ok=True)
    (SITE / key).write_bytes(audio)
    if not base:
        (SITE / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        base = SITE.resolve().as_uri()

    date = f"{now.day}. {now.month}." + (f" {now:%H:%M}" if on_demand else "")
    episodes.insert(0, {
        "guid": guid,
        "title": f"{date} – {script['title']}",
        "description": show_notes_html(script),
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
