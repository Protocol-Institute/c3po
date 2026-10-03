#!/usr/bin/env python3
"""
sync_symposium_videos.py — Protocol Symposium 2026 recordings from the YouTube
playlist into the Pinecone `symposium` namespace.

Phase C of plans/symposium-ingest.md. Reuses the ordinary YouTube steps for
captions and enrichment — so the recordings also become protocolized.io
resources like any other video — and owns only two things:

  1. Matching each recording to its programme entry (the D1 slug), so a
     recording, its slides and its listing are one talk to retrieval.
  2. Embedding into `symposium`, not `videos`. A question that names the
     symposium is answered from `symposium` alone (session 55 scoping), so a
     recording stored in `videos` would be invisible to exactly the questions
     most likely to want it.

Two chunk types:
  symposium_recording   one per video: summary, key concepts, link
  symposium_transcript  ~2.3K-char segment with its start time; the url
                        deep-links to that moment (&t=Ns)

Runs on the LAPTOP. YouTube answers the VM's caption requests with HTTP 429
(datacenter IP, probed 2026-10-02), so this cannot be a daemon step. Re-run
whenever more recordings are uploaded; unchanged videos cost nothing.

Captions come from youtube-transcript-api, not yt-dlp: forty back-to-back
yt-dlp subtitle requests got the laptop 429'd too, while the transcript API
kept working — and it returns timed lines without the rolling-caption
repetition that VTT needs deduplicating.

Usage:
    python3 ingest/sync_symposium_videos.py            # fetch → match → enrich → embed
    python3 ingest/sync_symposium_videos.py --dry-run  # match report only, no writes
    python3 ingest/sync_symposium_videos.py --no-fetch # skip yt-dlp (captions already local)
    python3 ingest/sync_symposium_videos.py --force    # re-embed everything
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index
from sync_symposium import fetch_json, hosts_of, track_label, EVENT_NAME
from sync_symposium_decks import resolve, norm, MAX_META_TEXT, EMAIL_RE

load_dotenv()

ROOT        = Path(__file__).resolve().parent.parent
SERIES      = "symposium-2026"
NAMESPACE   = "symposium"
YT_DIR      = ROOT / "sources" / "youtube"
META_PATH   = YT_DIR / "video_meta.json"
ENRICHED    = YT_DIR / "enriched_meta.json"
TEXT_DIR    = YT_DIR / "captions"          # plain text, read by enrich_youtube.py
TIMED_DIR   = YT_DIR / "captions_timed"    # [[start_sec, text], ...] for transcript chunks
MAP_PATH    = ROOT / "config" / "symposium_video_map.json"
STATE_PATH  = ROOT / "data" / "symposium_videos_state.json"
REVIEW_PATH = ROOT / "data" / "symposium_videos_review.json"
PY          = sys.executable
FETCH_PAUSE = 4.0       # seconds between transcript requests; YouTube rate-limits bursts

CHUNK_CHARS = 2300      # matches the shared chunker's target, so slides and talk weigh alike


# ── Titles ────────────────────────────────────────────────────────────────────
# Uploads are titled "NN - Speaker(s) - Title"; two have a stray separator
# ("17  Jose Leal…", "22  -Jesse Katz…"), and the opening talk has no number.
TITLE_RE = re.compile(r"^\s*(\d+)\s*-?\s*(.+?)\s+[-–]\s+(.+)$")


def split_title(raw: str) -> tuple[str, str, str]:
    """-> (number, speakers, title). Unnumbered titles keep the whole string."""
    m = TITLE_RE.match(raw)
    if not m:
        return "", "", raw.strip()
    return m.group(1), m.group(2).strip(" -"), m.group(3).strip()


def name_tokens(s: str) -> set[str]:
    """Lower-case name words, 3+ letters, minus connectives — for corroboration."""
    words = set(re.findall(r"[a-zà-ÿ]{3,}", s.lower()))
    return words - {"and", "the", "with"}


def corroborated(video_speakers: str, p: dict) -> bool:
    """Does any name in the video title appear among the talk's hosts?

    The video names its speaker and the deck filenames mostly did not, so the
    resolver's surname step never had this. Without it the trial run matched
    Yuhan Liu's OpenCourier talk to "Open Mic: Frontier-Pacing Protocols" on
    the word "protocol".
    """
    if not video_speakers:
        return True
    return bool(name_tokens(video_speakers) & name_tokens(" ".join(hosts_of(p))))


def match(vid: str, raw_title: str, proposals: list[dict], overrides: dict):
    """-> (proposal|None, why)."""
    _, speakers, title = split_title(raw_title)
    p, why = resolve({"id": vid, "name": title}, proposals, overrides)
    if p and why != "override" and not why.startswith("override") and not corroborated(speakers, p):
        return None, f"title matched \"{p['title'][:40]}\" but its speakers are {hosts_of(p)}, not {speakers!r}"
    return p, why


# ── Captions with time ────────────────────────────────────────────────────────

def fetch_transcript(vid: str) -> list[tuple[int, str]] | None:
    """-> [(start_seconds, text)] from YouTube's English captions, or None."""
    from youtube_transcript_api import YouTubeTranscriptApi
    try:
        snippets = YouTubeTranscriptApi().fetch(vid, languages=["en"])
    except Exception as e:                                      # noqa: BLE001
        print(f"    no transcript: {type(e).__name__}: {str(e)[:80]}")
        return None
    return [(int(sn.start), sn.text.replace("\n", " ").strip()) for sn in snippets if sn.text.strip()]


def timed_chunks(segments: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Group caption lines into ~CHUNK_CHARS chunks; each keeps its first line's time."""
    chunks, buf, t0 = [], [], None
    for t, text in segments:
        if t0 is None:
            t0 = t
        buf.append(text)
        if sum(len(b) + 1 for b in buf) >= CHUNK_CHARS:
            chunks.append((t0, " ".join(buf)))
            buf, t0 = [], None
    if buf:
        chunks.append((t0 or 0, " ".join(buf)))
    return chunks


def mmss(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


# ── Steps ─────────────────────────────────────────────────────────────────────

def load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def run_step(args: list[str]):
    print(f"\n$ {' '.join(Path(a).name if a.endswith('.py') else a for a in args)}")
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode:
        sys.exit(f"step failed ({r.returncode}): {args}")


def programme_block(p: dict) -> dict:
    return {"slug": p.get("slug") or "", "title": p["title"],
            "speakers": hosts_of(p), "abstract": (p.get("abstract") or "")[:2000],
            "scheduled_date": p.get("scheduled_date") or "",
            "track": (track_label(p.get("track")) or "")}


def run(dry_run=False, fetch=True, force=False):
    meta = load(META_PATH, {})
    # The playlist only — listing every series would pick up new uploads
    # elsewhere, which would then be published as resources without ever being
    # embedded in `videos`.
    from fetch_youtube_meta import fetch_playlist_videos, PLAYLISTS
    pid = next(k for k, v in PLAYLISTS.items() if v == SERIES)
    listed = fetch_playlist_videos(pid)
    for v in listed:
        meta.setdefault(v["video_id"], {
            **v, "url": f"https://www.youtube.com/watch?v={v['video_id']}",
            "playlist_ids": [pid], "series": SERIES, "has_captions": False})
        meta[v["video_id"]].update(series=SERIES, duration_sec=v["duration_sec"])

    if fetch and not dry_run:
        TEXT_DIR.mkdir(parents=True, exist_ok=True)
        TIMED_DIR.mkdir(parents=True, exist_ok=True)
        todo = [v["video_id"] for v in listed if not (TIMED_DIR / f"{v['video_id']}.json").exists()]
        print(f"Fetching transcripts for {len(todo)} of {len(listed)} recordings …")
        for n, vid in enumerate(todo):
            if n:
                time.sleep(FETCH_PAUSE)
            print(f"  [{n + 1}/{len(todo)}] {meta[vid].get('youtube_title', meta[vid]['title'])[:60]}")
            segs = fetch_transcript(vid)
            if not segs:
                continue
            save(TIMED_DIR / f"{vid}.json", segs)
            (TEXT_DIR / f"{vid}.txt").write_text(" ".join(t for _, t in segs), encoding="utf-8")
        for v in listed:
            meta[v["video_id"]]["has_captions"] = (TIMED_DIR / f"{v['video_id']}.json").exists()
        save(META_PATH, meta)
    vids = {k: v for k, v in meta.items() if v.get("series") == SERIES}
    if not vids:
        sys.exit(f"No {SERIES} videos in {META_PATH.name} — run without --no-fetch first.")

    proposals = fetch_json("/api/symposium/proposals").get("proposals", [])
    overrides = load(MAP_PATH, {})

    # ── Match, and give each video a clean title and the programme context ──
    print(f"\n── Matching {len(vids)} recordings ──")
    review = []
    for vid, v in sorted(vids.items(), key=lambda kv: kv[1].get("youtube_title", kv[1]["title"])):
        raw = v.get("youtube_title") or v["title"]
        num, speakers, title = split_title(raw)
        p, why = match(vid, raw, proposals, overrides)
        flag = "OK  " if p else "??  "
        print(f"  {flag}{raw[:58]:<60} -> {(p['slug'][:44] if p else why[:70])}")
        if not p:
            review.append({"video_id": vid, "title": raw, "reason": why})
        prog = programme_block(p) if p else {}
        # The resource library publishes `title`, so it gets the talk title
        # without the upload's running number and speaker prefix (speakers are
        # their own field). The raw upload title is kept for re-matching.
        v.update({"youtube_title": raw, "title": title, "programme": prog})
        if prog.get("scheduled_date"):
            v["date"] = prog["scheduled_date"]      # the talk's date, not the upload's

    print(f"\n  resolved {len(vids) - len(review)}   needs review {len(review)}")
    if dry_run:
        print("\n(dry run — nothing written)")
        return
    save(REVIEW_PATH, {"unmatched": review})
    if review:
        print(f"  Unmatched recordings are still embedded, attached to the event rather than a "
              f"talk. Add overrides to {MAP_PATH.relative_to(ROOT)}; see {REVIEW_PATH.name}.")
    save(META_PATH, meta)

    # ── Enrich: new videos, plus any whose programme match changed ──
    enriched = load(ENRICHED, {})
    stale = [vid for vid in vids
             if vid in enriched and (enriched[vid].get("programme") or {}).get("slug", "")
             != (meta[vid].get("programme") or {}).get("slug", "")]
    for vid in stale:
        run_step([PY, "ingest/enrich_youtube.py", "--video", vid, "--force"])
    run_step([PY, "ingest/enrich_youtube.py", "--series", SERIES])
    enriched = load(ENRICHED, {})

    # ── Embed ──
    state = load(STATE_PATH, {"videos": {}})
    done = state["videos"]
    vc, idx = get_voyage_client(), get_pinecone_index()
    upserted = skipped = missing = 0
    print(f"\n── Embedding into `{NAMESPACE}` ──")
    for vid in sorted(vids):
        e = enriched.get(vid)
        timed_path = TIMED_DIR / f"{vid}.json"
        if not e or not timed_path.exists():
            print(f"  WAIT  {meta[vid]['title'][:60]:<62} no captions/enrichment yet")
            missing += 1
            continue
        segs = [tuple(x) for x in load(timed_path, [])]
        prog = e.get("programme") or {}
        fingerprint = hashlib.sha256(json.dumps(
            [segs, e.get("summary"), e.get("key_concepts"), prog, e["title"],
             meta[vid].get("duration_sec", 0)],
            sort_keys=True).encode()).hexdigest()[:16]
        prev = done.get(vid, {})
        if not force and prev.get("hash") == fingerprint:
            skipped += 1
            continue

        title    = prog.get("title") or e["title"]
        speakers = prog.get("speakers") or e.get("speakers") or []
        url      = f"https://www.youtube.com/watch?v={vid}"
        when     = prog.get("scheduled_date", "")
        header = (f"{EVENT_NAME} - recording of \"{title}\"\n"
                  f"Speakers: {', '.join(speakers) or 'Protocol Institute'}\n"
                  + (f"Track: {prog['track']}\n" if prog.get("track") else "")
                  + (f"Presented: {when}\n" if when else ""))

        summary_text = (f"{header}Recording: {url} ({meta[vid].get('duration_sec', 0) // 60} min)\n\n"
                        f"Summary: {e.get('summary', '')}\n"
                        f"Key concepts: {', '.join(e.get('key_concepts') or [])}")
        pieces = [(None, summary_text)] + [
            (t, f"{header}Transcript (auto-captions) from {mmss(t)}:\n\n{text}")
            for t, text in timed_chunks(segs)
        ]
        # Speakers sometimes read out an address; captions would carry it.
        pieces = [(t, EMAIL_RE.sub("[email removed]", c)) for t, c in pieces]

        ids = [f"symposium__video-{vid}__summary"] + \
              [f"symposium__video-{vid}__{i}" for i in range(len(pieces) - 1)]
        stale_ids = [i for i in prev.get("vector_ids", []) if i not in ids]
        if stale_ids:
            idx.delete(ids=stale_ids, namespace=NAMESPACE)

        vecs = embed_chunks([c for _, c in pieces], vc)
        base = {"event": EVENT_NAME, "title": title, "slug": prog.get("slug", ""),
                "speakers": ", ".join(speakers), "scheduled_date": when,
                "track": (prog.get("track") or "").split(" (")[0],
                "video_id": vid, "duration_sec": meta[vid].get("duration_sec", 0)}
        records = []
        for i, ((t, c), vec, vid_id) in enumerate(zip(pieces, vecs, ids)):
            md = {**base, "text": c[:MAX_META_TEXT]}
            if t is None:
                md.update(chunk_type="symposium_recording", url=url)
            else:
                md.update(chunk_type="symposium_transcript", url=f"{url}&t={t}s",
                          start_sec=t, chunk_index=i - 1, chunk_total=len(pieces) - 1)
            records.append({"id": vid_id, "values": vec, "metadata": md})
        for i in range(0, len(records), 100):
            idx.upsert(vectors=records[i:i + 100], namespace=NAMESPACE)

        done[vid] = {"hash": fingerprint, "vector_ids": ids, "slug": prog.get("slug", ""),
                     "title": title}
        save(STATE_PATH, state)
        upserted += 1
        print(f"  EMBED {title[:56]:<58} 1 summary + {len(pieces) - 1} transcript chunks")

    print(f"\n── Summary ──  embedded {upserted}   unchanged {skipped}   "
          f"waiting {missing}   unmatched {len(review)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ingest symposium recordings into the symposium namespace")
    ap.add_argument("--dry-run",  action="store_true", help="Match report only; no fetch, no writes")
    ap.add_argument("--no-fetch", action="store_true", help="Skip the yt-dlp caption fetch")
    ap.add_argument("--force",    action="store_true", help="Re-embed every recording")
    a = ap.parse_args()
    run(dry_run=a.dry_run, fetch=not a.no_fetch, force=a.force)
