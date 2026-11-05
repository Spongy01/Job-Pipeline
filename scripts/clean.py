#!/usr/bin/env python3
"""Usage: python3 scripts/clean.py [YYYY-MM-DD]   (default: today)

Reads runs/<date>/raw.json and writes runs/<date>/clean.json and
runs/<date>/clean_report.json. Never touches raw.json, jobs.json or config.json.
Prints one line of JSON (counts) on success. On failure prints a message to
stderr and exits non-zero without writing anything.
"""
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent

SHORT_DESCRIPTION_MIN = 300      # below this many chars: "description_short"
TRUNCATION_MULTIPLE = 1000       # an exact multiple of this (>= it): "description_truncated_suspect"

REQUIRED_STRING_FIELDS = ("posting_name", "company", "linkedin_url")
TRACKING_PARAMS = {"gclid", "fbclid", "trk", "trackingid", "refid", "referrer"}  # lowercase
TRACKING_PREFIXES = ("utm_",)
COMPANY_SUFFIXES = ("inc", "llc", "ltd", "corp", "corporation", "co", "company", "plc", "gmbh")
HARD_FLAGS_EXCLUDED_FROM_REVIEW = {"company_not_in_description"}

UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
RELATIVE_RE = re.compile(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE)

# host regex -> (path regex, board url template). Named groups in the path
# regex and {host} are available to the template. Add a row to support a new
# job board.
LOCALE = r"[a-z]{2}(?:-[A-Za-z]{2})?"
BOARD_RULES = [
    (r"jobs\.ashbyhq\.com", r"^/(?P<company>[^/]+)", "https://{host}/{company}"),
    (r"(?:job-boards|boards)\.greenhouse\.io", r"^/(?P<company>[^/]+)", "https://{host}/{company}"),
    (r"jobs\.lever\.co", r"^/(?P<company>[^/]+)", "https://{host}/{company}"),
    (r"[^.]+\.wd\d+\.myworkdayjobs\.com",
     r"^(?:/(?P<locale>" + LOCALE + r"))?/(?P<site>[^/]+)/job/",
     "https://{host}{locale}/{site}"),
]


# ---------- apply url ----------

def _is_tracking(name):
    n = name.lower()
    return n in TRACKING_PARAMS or n.startswith(TRACKING_PREFIXES)


def clean_apply_url(url):
    """Unwrap LinkedIn redirects, strip tracking params. Idempotent. None stays None."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    for _ in range(5):  # redirects could in theory be nested
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (host == "linkedin.com" or host.endswith(".linkedin.com")) and parts.path.startswith("/safety/go"):
            target = parse_qs(parts.query).get("url")
            if target and target[0].strip():
                url = target[0].strip()
                continue
        break
    parts = urlsplit(url)
    kept = [p for p in parts.query.split("&") if p and not _is_tracking(p.split("=", 1)[0])]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment))


def derive_job_board_url(apply_url):
    if not apply_url:
        return None
    parts = urlsplit(apply_url)
    host = (parts.hostname or "").lower()
    for host_re, path_re, template in BOARD_RULES:
        if not re.fullmatch(host_re, host):
            continue
        m = re.match(path_re, parts.path)
        if not m:
            return None
        fields = {k: v or "" for k, v in m.groupdict().items()}
        if fields.get("locale"):
            fields["locale"] = "/" + fields["locale"]
        fields["host"] = host
        return template.format(**fields)
    return None


# ---------- dates ----------

def parse_posted_on(posted_text, scraped_at):
    """Return YYYY-MM-DD or None if it cannot be worked out."""
    if not isinstance(posted_text, str) or not isinstance(scraped_at, str):
        return None
    try:
        scraped = date.fromisoformat(scraped_at[:10])
    except ValueError:
        return None
    m = RELATIVE_RE.search(posted_text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    days = n * UNIT_DAYS[unit] if unit in UNIT_DAYS else 0  # minutes/hours: scrape date
    try:
        return (scraped - timedelta(days=days)).isoformat()
    except OverflowError:
        return None


# ---------- description ----------

def normalize_description(text):
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _company_core(company):
    name = company.strip().lower()
    pattern = re.compile(r"[\s,]+(?:" + "|".join(COMPANY_SUFFIXES) + r")\.?$")
    while True:
        new = pattern.sub("", name).strip(" ,.")
        if new == name:
            return name
        name = new


def compute_flags(job, original_length, description):
    flags = []
    if original_length < SHORT_DESCRIPTION_MIN:
        flags.append("description_short")
    if original_length >= TRUNCATION_MULTIPLE and original_length % TRUNCATION_MULTIPLE == 0:
        flags.append("description_truncated_suspect")
    if job.get("apply_type") == "external" and job.get("apply_url") is None:
        flags.append("missing_apply_url")
    core = _company_core(job["company"])
    if core and core not in (description or "").lower():
        flags.append("company_not_in_description")
    return flags


# ---------- per job ----------

def validate(job):
    """Return (normalized_id, None) or (None, reason)."""
    if not isinstance(job, dict):
        return None, "not an object"
    problems = []
    raw_id = job.get("id")
    job_id = None
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        problems.append("id missing or invalid")
    else:
        job_id = str(raw_id).strip()
        if not re.fullmatch(r"\d+", job_id):
            problems.append("id is not digits only")
    for field in REQUIRED_STRING_FIELDS:
        v = job.get(field)
        if not isinstance(v, str) or not v.strip():
            problems.append("missing or empty " + field)
    if problems:
        return None, "; ".join(problems)
    return job_id, None


def clean_job(job, job_id):
    out = dict(job)  # keeps raw field order
    out["id"] = job_id
    closed = job.get("closed") is True
    out["closed"] = closed

    out["reposted"] = isinstance(job.get("posted_text"), str) and "repost" in job["posted_text"].lower()
    posted_on = parse_posted_on(job.get("posted_text"), job.get("scraped_at"))
    unparsed = posted_on is None
    out["posted_on"] = posted_on if posted_on else (job.get("posted_on") or None)

    if closed:
        out["description"] = None
        out["flags"] = []
        out["review"] = False
        return out, unparsed

    description = job.get("description")
    original_length = len(description) if isinstance(description, str) else 0
    out["description"] = normalize_description(description) if isinstance(description, str) else None

    out["apply_url"] = clean_apply_url(job.get("apply_url"))
    out["job_board_url"] = derive_job_board_url(out["apply_url"])

    flags = compute_flags(out, original_length, out["description"])
    out["flags"] = flags
    out["review"] = any(f not in HARD_FLAGS_EXCLUDED_FROM_REVIEW for f in flags)
    return out, unparsed


def clean_all(raw):
    cleaned, report = [], {
        "dropped_invalid": [], "duplicates": [], "unparsed_posted_text": [], "flagged": [],
    }
    seen = {}  # id -> index of the kept job
    for index, job in enumerate(raw):
        job_id, reason = validate(job)
        if reason:
            shown = job.get("id") if isinstance(job, dict) else None
            report["dropped_invalid"].append({"index": index, "id": shown, "reason": reason})
            continue
        if job_id in seen:
            report["duplicates"].append({"id": job_id, "index": index, "kept_index": seen[job_id]})
            continue
        seen[job_id] = index
        out, unparsed = clean_job(job, job_id)
        cleaned.append(out)
        if unparsed:
            report["unparsed_posted_text"].append(job_id)
        if out["flags"]:
            report["flagged"].append({
                "id": job_id, "company": out["company"],
                "posting_name": out["posting_name"], "flags": out["flags"],
            })
    counts = {
        "input": len(raw),
        "written": len(cleaned),
        "closed": sum(1 for j in cleaned if j["closed"]),
        "dropped_invalid": len(report["dropped_invalid"]),
        "duplicates": len(report["duplicates"]),
        "review_true": sum(1 for j in cleaned if j["review"]),
    }
    return cleaned, {"counts": counts, **report}


# ---------- io ----------

def write_json_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(root, run_date):
    run_dir = Path(root) / "runs" / run_date
    raw_path = run_dir / "raw.json"
    if not raw_path.is_file():
        raise SystemExit("clean.py: %s not found" % raw_path)
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SystemExit("clean.py: %s is not valid JSON (%s)" % (raw_path, e))
    if not isinstance(raw, list):
        raise SystemExit("clean.py: %s must contain a JSON list" % raw_path)
    cleaned, report = clean_all(raw)
    write_json_atomic(run_dir / "clean.json", cleaned)
    write_json_atomic(run_dir / "clean_report.json", report)
    return {"date": run_date, **report["counts"]}


def main(argv):
    if len(argv) > 1:
        raise SystemExit("usage: clean.py [YYYY-MM-DD]")
    run_date = argv[0] if argv else date.today().isoformat()
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("clean.py: date must be YYYY-MM-DD, got %r" % run_date)
    print(json.dumps(run(ROOT, run_date)))


if __name__ == "__main__":
    main(sys.argv[1:])
