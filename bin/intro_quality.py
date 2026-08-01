"""
Intro response quality checker.

Called by handle_introduction() to verify and auto-fix rec_sources before
posting, and to log issues for session-start review.

Quality log: data/intro_quality_log.jsonl (one entry per checked intro)
Review:      python3 bin/review_intro_quality.py
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

QUALITY_LOG_PATH = Path(__file__).parent.parent / "data" / "intro_quality_log.jsonl"

_USP_MARKERS = ("unreasonable sufficiency of protocols",)
_VGR_MARKERS = ("venkatesh rao", "venkatesh", "vgr")


def _title_in_answer(title: str, answer_lower: str) -> bool:
    title_core = re.sub(r'\s*\(.*?\)', '', title).strip().lower()
    if not title_core or len(title_core) <= 6:
        return False
    if title_core in answer_lower:
        return True
    words = re.findall(r'\b\w{4,}\b', title_core)
    if not words:
        return False
    return sum(1 for w in words if w in answer_lower) / len(words) >= 0.6


def _is_vgr_authored(src: dict) -> bool:
    pa = (src.get("primary_author") or "").lower()
    authors = [a or "" for a in (src.get("authors") or [])]
    return any(m in pa for m in _VGR_MARKERS) or any(
        any(m in a.lower() for m in _VGR_MARKERS) for a in authors
    )


def check_and_fix(
    answer: str,
    all_sources: list,
    rec_sources: list,
    intro_text: str,
    title_matched: bool,
) -> tuple:
    """
    Validate rec_sources against the answer, auto-fix simple problems.
    Returns (fixed_rec_sources, issues).

    Issues are dicts with keys: code, severity ("warning"|"fixed"|"info"), detail.
    Severity meanings:
      fixed   — problem detected and automatically corrected before posting
      warning — problem that affects response quality; cannot auto-fix
      info    — worth tracking but not a hard quality failure
    """
    answer_lower = answer.lower()
    issues = []
    fixed_sources = list(rec_sources)

    # 1. Primary link not coherent with answer text
    if fixed_sources and not fixed_sources[0].get("is_fallback"):
        primary_title = fixed_sources[0].get("title") or ""
        if not _title_in_answer(primary_title, answer_lower):
            issues.append({
                "code": "title_mismatch",
                "severity": "warning",
                "detail": f"Primary link '{primary_title}' not found in answer text",
            })

    # 2. No title match — primary chosen by rank, not by what Claude said
    if not title_matched:
        issues.append({
            "code": "no_title_match",
            "severity": "info",
            "detail": "Primary source chosen by rank-order; title match failed",
        })

    # 3. USoP mentioned in answer (tracked for over-referencing)
    if any(m in answer_lower for m in _USP_MARKERS):
        issues.append({
            "code": "usp_in_answer",
            "severity": "info",
            "detail": "Answer recommends 'Unreasonable Sufficiency' (VGR-authored; not linkable)",
        })

    # 4. Any VGR paper explicitly named in the answer
    vgr_hits = [s for s in all_sources if _is_vgr_authored(s) and
                _title_in_answer(s.get("title") or "", answer_lower)]
    if vgr_hits:
        titles = ", ".join(s.get("title") or "?" for s in vgr_hits[:2])
        issues.append({
            "code": "vgr_paper_mentioned",
            "severity": "info",
            "detail": f"Answer mentions VGR-authored paper(s): {titles}",
        })

    # 5. Short answer (< 80 chars is suspiciously terse)
    if answer and len(answer) < 80:
        issues.append({
            "code": "short_answer",
            "severity": "warning",
            "detail": f"Answer is very short ({len(answer)} chars); may be truncated",
        })

    # 6. Auto-fix: remove any sources without a URL that slipped through
    #    (should not happen, but guards against edge cases)
    no_url = [s for s in fixed_sources if not s.get("url") and not s.get("is_fallback")]
    if no_url:
        fixed_sources = [s for s in fixed_sources if s.get("url") or s.get("is_fallback")]
        issues.append({
            "code": "no_url_removed",
            "severity": "fixed",
            "detail": f"Removed {len(no_url)} source(s) with no URL from suggested reading",
        })

    return fixed_sources, issues


def log_quality(
    user_hash: str,
    intro_snippet: str,
    primary_title: str,
    title_matched: bool,
    rec_count: int,
    issues: list,
    answer: str = "",
    primary_source: dict | None = None,
) -> None:
    """Append a quality log entry for non-trivial cases."""
    # Only log if there's something worth reviewing
    has_warnings = any(i["severity"] == "warning" for i in issues)
    has_usp = any(i["code"] == "usp_in_answer" for i in issues)
    has_fixed = any(i["severity"] == "fixed" for i in issues)
    if not (has_warnings or has_usp or has_fixed):
        return

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_hash": user_hash,
        "intro_snippet": intro_snippet[:60],
        "primary_title": primary_title,
        "title_matched": title_matched,
        "rec_count": rec_count,
        "issues": issues,
        # Full answer text + primary source type/sig, so a title_mismatch can
        # actually be diagnosed later instead of re-guessed each session —
        # see status.md session 46.
        "answer": answer[:600],
        "primary_source_type": (primary_source or {}).get("source"),
        "primary_sig_display": (primary_source or {}).get("sig_display"),
        "reviewed": False,
    }
    try:
        with QUALITY_LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        log.warning(f"Intro quality log write failed: {exc}")
