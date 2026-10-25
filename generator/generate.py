"""Nightly sentence generator for the Mandarin listening app.

For each upcoming day that doesn't have sentences yet:
  1. load the known-words list (Supabase, or the seed in supabase/setup.sql with --local)
  2. ask Claude for natural sentences using only those words
  3. verify every sentence with jieba; regenerate any that use unknown words
  4. synthesize audio with Azure TTS, rotating through several voices
  5. save to Supabase (audio bucket + sentences table), or to out/ with --local

Usage:
  python generator/generate.py --local            # test run, writes to out/
  python generator/generate.py --days 3           # fill today + next 2 days in Supabase
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import jieba
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
jieba.setLogLevel(logging.WARNING)
log = logging.getLogger("generate")

TIMEZONE = ZoneInfo("America/Los_Angeles")
SENTENCES_PER_DAY = 12
MAX_ROUNDS = 5
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# Standard Mainland Mandarin neural voices, mixed genders and ages.
VOICES = [
    "zh-CN-XiaoxiaoNeural",  # F, warm
    "zh-CN-XiaoyiNeural",    # F, bright
    "zh-CN-XiaochenNeural",  # F, casual
    "zh-CN-XiaohanNeural",   # F, gentle
    "zh-CN-XiaomoNeural",    # F, deeper, calm
    "zh-CN-XiaoruiNeural",   # F, older
    "zh-CN-YunxiNeural",     # M, bright
    "zh-CN-YunjianNeural",   # M, deep, casual
    "zh-CN-YunyeNeural",     # M, deep, calm
    "zh-CN-YunzeNeural",     # M, older
    "zh-CN-YunjieNeural",    # M, casual
    "zh-CN-YunhaoNeural",    # M, soft
]

CJK = re.compile(r"[一-鿿]")

# Compounds whose parts may all be known but whose meaning isn't guessable from
# them (大家 = "everyone", not "big home"). These count as unknown words.
# Add to this list whenever one slips through.
OPAQUE_COMPOUNDS = {
    "大家", "多少", "好看", "好听", "好吃", "好喝", "人家", "大人", "小人",
    "东西", "小心", "不好意思", "没有人", "看来", "想来", "说是", "还是",
}


# ---------------------------------------------------------------- words

def load_words_local() -> list[dict]:
    """Parse the seed rows out of supabase/setup.sql."""
    sql = (ROOT / "supabase" / "setup.sql").read_text(encoding="utf-8")
    rows = re.findall(r"\('([^']+)',\s*'([^']*)',\s*'([^']*)'(?:,\s*(true))?\)", sql)
    return [
        {"hanzi": h, "pinyin": p, "english": e, "is_function_word": bool(f)}
        for h, p, e, f in rows
        if CJK.search(h)
    ]


class Supabase:
    def __init__(self) -> None:
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        key = os.environ["SUPABASE_SECRET_KEY"]
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    def _get(self, path: str, **params) -> list[dict]:
        r = requests.get(f"{self.url}/rest/v1/{path}", headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def words(self) -> list[dict]:
        return self._get("words", select="hanzi,pinyin,english,is_function_word")

    def dates_with_sentences(self, dates: list[dt.date]) -> set[dt.date]:
        rows = self._get("sentences", select="for_date",
                         for_date=f"in.({','.join(d.isoformat() for d in dates)})")
        return {dt.date.fromisoformat(r["for_date"]) for r in rows}

    def recent_sentences(self, limit: int = 200) -> list[str]:
        rows = self._get("sentences", select="hanzi", order="created_at.desc", limit=str(limit))
        return [r["hanzi"] for r in rows]

    def upload_audio(self, path: str, audio: bytes) -> None:
        r = requests.post(
            f"{self.url}/storage/v1/object/audio/{path}",
            headers={**self.headers, "Content-Type": "audio/mpeg", "x-upsert": "true"},
            data=audio, timeout=60,
        )
        r.raise_for_status()

    def insert_sentences(self, rows: list[dict]) -> None:
        r = requests.post(
            f"{self.url}/rest/v1/sentences",
            headers={**self.headers, "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates"},
            params={"on_conflict": "for_date,position"},
            data=json.dumps(rows, ensure_ascii=False).encode(), timeout=30,
        )
        r.raise_for_status()


# ---------------------------------------------------------------- validation

class Validator:
    """Checks that a sentence uses only known words, using jieba segmentation."""

    def __init__(self, known: set[str]) -> None:
        self.known = known
        for w in known:
            jieba.add_word(w, freq=50000)  # nudge jieba to keep known words whole

    def _split_known(self, token: str) -> list[str] | None:
        """Split a token into known words (fewest pieces), or None if impossible."""
        best: list[list[str] | None] = [[]] + [None] * len(token)
        for end in range(1, len(token) + 1):
            for start in range(end):
                piece = token[start:end]
                if best[start] is not None and piece in self.known:
                    cand = best[start] + [piece]
                    if best[end] is None or len(cand) < len(best[end]):
                        best[end] = cand
        return best[-1]

    def check(self, hanzi: str) -> tuple[list[str], list[str]]:
        """Return (words used, unknown words)."""
        if re.search(r"[0-9A-Za-z０-９]", hanzi):
            return [], ["(digits or Latin letters)"]
        text = "".join(ch for ch in hanzi if CJK.match(ch))
        used, unknown = [], []
        for tok in jieba.lcut(text, HMM=False):
            if tok in self.known:
                used.append(tok)
            elif tok in OPAQUE_COMPOUNDS:
                # made of known characters, but means something you can't guess
                unknown.append(tok)
            elif (parts := self._split_known(tok)) is not None:
                # transparent compound of known words, e.g. 喝茶 = 喝 + 茶
                used.extend(parts)
            else:
                unknown.append(tok)
        return used, unknown


# ---------------------------------------------------------------- Claude

SENTENCE_TOOL = {
    "name": "submit_sentences",
    "description": "Submit the generated Mandarin listening-practice sentences.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sentences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hanzi": {"type": "string", "description": "Simplified Chinese with full-width punctuation"},
                        "pinyin": {"type": "string", "description": "Pinyin with tone marks, words spaced, matching punctuation"},
                        "english": {"type": "string", "description": "Natural English translation"},
                    },
                    "required": ["hanzi", "pinyin", "english"],
                },
            }
        },
        "required": ["sentences"],
    },
}


def build_prompt(words: list[dict], count: int, avoid: list[str], rejected: list[tuple[str, list[str]]]) -> str:
    content = [w for w in words if not w["is_function_word"]]
    function = [w for w in words if w["is_function_word"]]
    fmt = lambda ws: "\n".join(f"{w['hanzi']} ({w['pinyin']}) – {w['english']}" for w in ws)

    prompt = f"""You are writing listening-practice sentences for an adult English speaker learning standard Mainland Mandarin.

Write {count} sentences that use ONLY the words listed below. This is a strict rule: the learner must already know every single word they hear. Do not use any other word, measure word, name, number, or compound, even very common ones. If an idea can't be expressed with these words, write a different sentence. Combining known words is fine only when the meaning is obvious from the parts (喝 + 茶 = drink tea); avoid set expressions whose meaning isn't (大家 means "everyone", not "big home").

Known content words:
{fmt(content)}

Grammar/function words (always allowed):
{fmt(function)}

Make the sentences:
- natural and plausible: things a real person would actually say in everyday life (questions, statements, short replies)
- varied in length, from very short (3–5 characters) to longer (up to ~15 characters)
- varied in topic, sentence pattern, and which words they use; spread usage across the whole list
- in simplified characters with full-width Chinese punctuation (，。？！)

Provide pinyin with tone marks (words separated by spaces) and a natural English translation."""

    if avoid:
        prompt += "\n\nDo not repeat any of these recent sentences:\n" + "\n".join(avoid[:80])
    if rejected:
        prompt += "\n\nThese earlier attempts were rejected because they used unknown words; avoid those words:\n"
        prompt += "\n".join(f"{h}  (unknown: {', '.join(u)})" for h, u in rejected[-30:])
    return prompt


def ask_claude(client: anthropic.Anthropic, prompt: str) -> list[dict]:
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        tools=[SENTENCE_TOOL],
        messages=[{"role": "user", "content": prompt + "\n\nSubmit your sentences by calling the submit_sentences tool."}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input.get("sentences", [])
    log.warning("  Claude didn't call the tool (stop_reason=%s)", msg.stop_reason)
    return []


def generate_sentences(client, words, validator, count, avoid) -> list[dict]:
    accepted: list[dict] = []
    rejected: list[tuple[str, list[str]]] = []
    seen = set(avoid)
    for round_no in range(1, MAX_ROUNDS + 1):
        need = count - len(accepted)
        if need <= 0:
            break
        # ask for a few extra so a couple of rejections don't cost a whole round
        candidates = ask_claude(client, build_prompt(words, need + 4, list(seen), rejected))
        for s in candidates:
            hanzi = s["hanzi"].strip()
            if hanzi in seen or len(accepted) >= count:
                continue
            seen.add(hanzi)
            used, unknown = validator.check(hanzi)
            if unknown:
                rejected.append((hanzi, unknown))
                log.info("  rejected %s  unknown: %s", hanzi, ", ".join(unknown))
                continue
            accepted.append({**s, "hanzi": hanzi, "words": used})
        log.info("  round %d: %d/%d accepted", round_no, len(accepted), count)
    if len(accepted) < count:
        log.warning("  only got %d/%d valid sentences", len(accepted), count)
    return accepted


# ---------------------------------------------------------------- TTS

def synthesize(text: str, voice: str) -> bytes:
    region = os.environ["AZURE_SPEECH_REGION"]
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="zh-CN">'
        f'<voice name="{voice}">{text}</voice></speak>'
    )
    r = requests.post(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        headers={
            "Ocp-Apim-Subscription-Key": os.environ["AZURE_SPEECH_KEY"],
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
            "User-Agent": "mandarin-listening-app",
        },
        data=ssml.encode("utf-8"),
        timeout=60,
    )
    r.raise_for_status()
    return r.content


def assign_voices(n: int) -> list[str]:
    """Shuffle so each day hears as many different speakers as possible."""
    pool = VOICES[:]
    random.shuffle(pool)
    return [pool[i % len(pool)] for i in range(n)]


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local", action="store_true", help="use seed words and write to out/ instead of Supabase")
    ap.add_argument("--days", type=int, default=3, help="how many days ahead to fill, starting today")
    ap.add_argument("--count", type=int, default=SENTENCES_PER_DAY)
    ap.add_argument("--no-audio", action="store_true", help="skip TTS (faster when tuning prompts)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    db = None if args.local else Supabase()
    words = load_words_local() if args.local else db.words()
    validator = Validator({w["hanzi"] for w in words})
    client = anthropic.Anthropic()
    log.info("%d known words (%d function words)", len(words), sum(w["is_function_word"] for w in words))

    today = dt.datetime.now(TIMEZONE).date()
    dates = [today + dt.timedelta(days=i) for i in range(args.days)]
    if db:
        done = db.dates_with_sentences(dates)
        dates = [d for d in dates if d not in done]
        avoid = db.recent_sentences()
    else:
        avoid = []
    if not dates:
        log.info("All %d days already have sentences. Nothing to do.", args.days)
        return 0

    for day in dates:
        log.info("Generating for %s", day)
        sentences = generate_sentences(client, words, validator, args.count, avoid)
        avoid = [s["hanzi"] for s in sentences] + avoid
        voices = assign_voices(len(sentences))
        out_dir = ROOT / "out" / day.isoformat()
        if args.local:
            out_dir.mkdir(parents=True, exist_ok=True)

        rows = []
        for pos, (s, voice) in enumerate(zip(sentences, voices), start=1):
            audio_path = f"{day.isoformat()}/{pos:02d}.mp3"
            if not args.no_audio:
                audio = synthesize(s["hanzi"], voice)
                if db:
                    db.upload_audio(audio_path, audio)
                else:
                    (out_dir / f"{pos:02d}.mp3").write_bytes(audio)
            rows.append({
                "for_date": day.isoformat(), "position": pos,
                "hanzi": s["hanzi"], "pinyin": s["pinyin"], "english": s["english"],
                "words": s["words"], "voice": voice,
                "audio_path": None if args.no_audio else audio_path,
            })
            log.info("  %02d %-6s %s  |  %s", pos, voice.split("-")[2].replace("Neural", ""), s["hanzi"], s["english"])

        if db:
            db.insert_sentences(rows)
        else:
            (out_dir / "sentences.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Saved %d sentences for %s", len(rows), day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
