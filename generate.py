#!/usr/bin/env python3
"""Ranný podcast: RSS -> Claude (scenár) -> Azure TTS -> MP3 + feed.xml na Cloudflare R2.

Spustenie:  python generate.py            (produkcia, upload na R2)
            python generate.py --dry-run  (bez R2, výstup do ./out)
"""

import argparse
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

import anthropic
import boto3
import feedparser
import requests
import yaml
from botocore.exceptions import ClientError

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


# ---------- 2. Scenár (Claude) ----------

EPISODE_TOOL = {
    "name": "episode",
    "description": "Odovzdaj hotový scenár epizódy.",
    "input_schema": {
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
    },
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
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=ep.get("model", "claude-sonnet-5"),
        max_tokens=8000,
        system=SYSTEM.format(max_words=ep["max_words"]),
        tools=[EPISODE_TOOL],
        tool_choice={"type": "tool", "name": "episode"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(block.input for block in msg.content if block.type == "tool_use")


# ---------- 3. Audio (Azure TTS + ffmpeg) ----------

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


def tts(text, cfg, out_path):
    voice = cfg["tts"]["voice"]
    lang = "-".join(voice.split("-")[:2])
    body = escape(text)
    if cfg["tts"].get("rate"):
        body = f'<prosody rate="{cfg["tts"]["rate"]}">{body}</prosody>'
    ssml = f'<speak version="1.0" xml:lang="{lang}"><voice name="{voice}">{body}</voice></speak>'
    url = f"https://{env('AZURE_SPEECH_REGION')}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": env("AZURE_SPEECH_KEY"),
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
        "User-Agent": "ranny-podcast",
    }
    for attempt in range(3):
        r = requests.post(url, headers=headers, data=ssml.encode("utf-8"), timeout=120)
        if r.ok:
            out_path.write_bytes(r.content)
            return
        print(f"  ! TTS chyba {r.status_code}, skúšam znova")
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


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


# ---------- 4. Úložisko (R2 alebo lokálne) + feed ----------

class Storage:
    def __init__(self, dry_run=False):
        self.local = Path("out") if dry_run else None
        if self.local:
            self.local.mkdir(exist_ok=True)
            self.prefix, self.base = "", self.local.resolve().as_uri()
            return
        self.prefix = env("FEED_TOKEN") + "/"
        self.base = env("R2_PUBLIC_URL").rstrip("/")
        self.bucket = env("R2_BUCKET")
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
            aws_access_key_id=env("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
            region_name="auto",
        )

    def url(self, key):
        return f"{self.base}/{self.prefix}{key}"

    def read_json(self, key, default):
        try:
            if self.local:
                return json.loads((self.local / key).read_text(encoding="utf-8"))
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=self.prefix + key)["Body"].read())
        except FileNotFoundError:
            return default
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return default
            raise

    def write(self, key, data, content_type, cache="no-cache"):
        if self.local:
            path = self.local / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return
        self.s3.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data,
                           ContentType=content_type, CacheControl=cache)

    def delete(self, key):
        if self.local:
            (self.local / key).unlink(missing_ok=True)
        else:
            self.s3.delete_object(Bucket=self.bucket, Key=self.prefix + key)


def show_notes_html(script):
    links = "".join(
        f'<li><a href="{html.escape(n["url"], quote=True)}">{html.escape(n["title"])}</a></li>'
        for n in script["show_notes"]
    )
    return f"<p>Zdroje:</p><ul>{links}</ul>"


def build_feed(cfg, episodes, storage):
    p = cfg["podcast"]
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = p["title"]
    ET.SubElement(ch, "link").text = storage.url("feed.xml")
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
        ET.SubElement(item, "enclosure", {"url": storage.url(ep["key"]), "length": str(ep["length"]),
                                          "type": "audio/mpeg"})
        ET.SubElement(item, f"{{{ITUNES}}}duration").text = str(ep["duration"])
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez uploadu na R2, výstup do ./out")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    focus = os.environ.get("FOCUS", "").strip()
    on_demand = os.environ.get("RUN_KIND") == "workflow_dispatch"
    storage = Storage(args.dry_run)
    episodes = storage.read_json("episodes.json", [])

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
    date = f"{now.day}. {now.month}." + (f" {now:%H:%M}" if on_demand else "")
    guid = str(uuid.uuid4())
    key = f"episodes/{now:%Y-%m-%d-%H%M}-{guid[:8]}.mp3"
    storage.write(key, audio, "audio/mpeg", cache="public, max-age=31536000")
    if args.dry_run:
        storage.write("script.json", json.dumps(script, ensure_ascii=False, indent=2).encode(), "application/json")

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
    keep = cfg["podcast"].get("keep_episodes", 14)
    for old in episodes[keep:]:
        storage.delete(old["key"])
    episodes = episodes[:keep]

    storage.write("episodes.json", json.dumps(episodes, ensure_ascii=False, indent=1).encode(), "application/json")
    storage.write("feed.xml", build_feed(cfg, episodes, storage), "application/rss+xml; charset=utf-8")
    print(f"Hotovo: {episodes[0]['title']} ({duration // 60}:{duration % 60:02d})")


if __name__ == "__main__":
    main()
