"""Build a freeform newsletter draft from second-brain updates, books, Bluesky,
and new blog posts. Edit the resulting markdown locally, then `send` to Listmonk.

Workflow:
    uv run python newsletter.py gather              # write drafts/newsletter-<date>.md
    $EDITOR drafts/newsletter-<date>.md             # edit the draft
    uv run python newsletter.py send drafts/...md   # push to Listmonk + advance .last_newsletter

The `.last_newsletter` file (committed to the repo) records the last successful
send time. `gather` only includes content newer than that date, so re-runs
between sends keep showing the same backlog — nothing is "consumed" until you
actually send.
"""

import os
import re
import shutil
import subprocess
import tomllib
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import click
import duckdb
from dotenv import load_dotenv
from jinja2 import Template

from listmonk_rss import (
    fetch_rss_feed,
    get_list_id,
    schedule_campaign,
)

load_dotenv()

ROOT = Path(__file__).parent
LAST_NEWSLETTER_FILE = ROOT / ".last_newsletter"
DRAFTS_DIR = ROOT / "drafts"
COPY_DIR = ROOT / ".copy"  # snapshots of sensitive source dirs (gitignored)
TEMPLATE_FILE = ROOT / "newsletter_template.md.j2"
CONFIG_FILE = ROOT / "newsletter.toml"


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    return {}


CONFIG = _load_config()
_BRAIN_CFG = CONFIG.get("brain", {})
_BLOG_CFG = CONFIG.get("blog", {})
_BOOKS_CFG = CONFIG.get("books", {})
_BSKY_CFG = CONFIG.get("bluesky", {})
_GATHER_CFG = CONFIG.get("gather", {})

# Brain content lives in a git submodule under second-brain-public/content,
# which has its own .git — git log must run inside that submodule path.
BRAIN_CONTENT = Path(os.getenv(
    "BRAIN_CONTENT",
    "/home/sspaeti/git/sspaeti.com/second-brain-public/content",
))
BOOKS_DIR = Path(os.getenv("BOOKS_DIR", "/home/sspaeti/Simon/SecondBrain/💡 Resources/📚 Books"))
BRAIN_BASE_URL = "https://www.ssp.sh/brain/"

BSKY_HANDLE = os.getenv("BSKY_HANDLE", "ssp.sh")
BSKY_DID = os.getenv("BSKY_DID", "did:plc:edglm4muiyzty2snc55ysuqx")

DEFAULT_THRESHOLD_WORDS = _BRAIN_CFG.get("threshold_words", 100)
DEFAULT_MAJOR_BUCKET_WORDS = _BRAIN_CFG.get("major_bucket_words", 500)
DEFAULT_BRAIN_LIMIT = _BRAIN_CFG.get("limit", 40)
DEFAULT_BLOG_LIMIT = _BLOG_CFG.get("limit", 2)
DEFAULT_BOOKS_LIMIT = _BOOKS_CFG.get("limit", 5)
DEFAULT_BSKY_TOP = _BSKY_CFG.get("top", 15)
DEFAULT_BSKY_RECENT = _BSKY_CFG.get("recent", 10)
DEFAULT_LOOKBACK_DAYS = _GATHER_CFG.get("default_lookback_days", 60)


# ----- State -----

def get_last_newsletter_date() -> datetime:
    if LAST_NEWSLETTER_FILE.exists():
        return datetime.fromisoformat(LAST_NEWSLETTER_FILE.read_text().strip())
    return datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def save_last_newsletter_date(dt: datetime) -> None:
    LAST_NEWSLETTER_FILE.write_text(dt.isoformat())


def slugify(text: str) -> str:
    slug = text.lower()
    slug = re.sub(r"[‘’']", "", slug)
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


# ----- Second Brain (git-based change detection) -----

def _git_word_stats(since: datetime) -> dict[str, dict]:
    """Parse `git log -p` to count added/deleted *words* per .md file since `since`.

    Returns `{path: {"added_words", "deleted_words", "last_commit_date"}}`.
    Words are whitespace-split tokens from `+`/`-` diff lines (markdown syntax
    counts too, but it's a relative measure, so that's fine for ranking)."""
    result = subprocess.run(
        [
            "git", "-c", "core.quotePath=false",
            "-C", str(BRAIN_CONTENT), "log",
            f"--since={since.strftime('%Y-%m-%d %H:%M:%S')}",
            "-p", "--format=__COMMIT__%H|%ai",
            "--", "*.md",
        ],
        capture_output=True, text=True, check=True,
    )

    stats: dict[str, dict] = defaultdict(
        lambda: {"added_words": 0, "deleted_words": 0, "last_commit_date": ""}
    )
    current_date = ""
    current_path: str | None = None
    is_binary = False

    for line in result.stdout.splitlines():
        if line.startswith("__COMMIT__"):
            _, _, rest = line.partition("__COMMIT__")
            _, _, current_date = rest.partition("|")
            continue
        if line.startswith("diff --git "):
            # `diff --git a/PATH b/PATH` — take everything after the last " b/".
            _, _, b_path = line.partition(" b/")
            current_path = b_path if b_path.endswith(".md") else None
            is_binary = False
            if current_path and not stats[current_path]["last_commit_date"]:
                stats[current_path]["last_commit_date"] = current_date
            continue
        if current_path is None:
            continue
        if line.startswith("Binary files"):
            is_binary = True
            continue
        if is_binary or line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            stats[current_path]["added_words"] += len(line[1:].split())
        elif line.startswith("-"):
            stats[current_path]["deleted_words"] += len(line[1:].split())

    return stats


def _first_commit_dates() -> dict[str, str]:
    """Map every .md file in BRAIN_CONTENT to the date of its first-ever commit.
    Used to flag notes that look like a "big edit" but are actually brand-new
    files (typical when content is exported from a private vault — git sees the
    whole file as one large addition)."""
    result = subprocess.run(
        [
            "git", "-c", "core.quotePath=false", "-C", str(BRAIN_CONTENT),
            "log", "--reverse", "--diff-filter=A",
            "--name-only", "--format=__COMMIT__%ai",
            "--", "*.md",
        ],
        capture_output=True, text=True, check=True,
    )
    first: dict[str, str] = {}
    current_date = ""
    for line in result.stdout.splitlines():
        if line.startswith("__COMMIT__"):
            current_date = line[len("__COMMIT__"):]
        elif line.endswith(".md") and line not in first:
            first[line] = current_date
    return first


def gather_brain_updates(since: datetime, threshold_words: int) -> list[dict]:
    """Notes in BRAIN_CONTENT with at least `threshold_words` added words since `since`.

    Each entry includes `is_new` — True when the file's first-ever commit falls
    within the window (so the word count reflects a fresh add, not a small edit
    on an old note)."""
    if not BRAIN_CONTENT.exists():
        click.echo(f"BRAIN_CONTENT {BRAIN_CONTENT} not found, skipping brain updates", err=True)
        return []

    stats = _git_word_stats(since)
    first_dates = _first_commit_dates()
    since_iso = since.strftime("%Y-%m-%d")

    updates = []
    for path, s in stats.items():
        net = s["added_words"] - s["deleted_words"]
        if net < threshold_words:
            continue
        full = BRAIN_CONTENT / path
        if not full.exists():
            continue  # file was deleted
        meta = _parse_brain_frontmatter(full)
        # Hugo derives the URL from the filename stem (slugified), not the
        # frontmatter title. E.g. file "zen mode for writing.md" with title
        # "Zen Mode for Writing (Obsidian, Neovim)" → /brain/zen-mode-for-writing/
        slug = slugify(Path(path).stem)
        title = meta.get("title") or Path(path).stem.title()
        description = meta.get("description") or _first_sentence(full)
        first = first_dates.get(path, "")
        is_new = bool(first) and first[:10] >= since_iso
        updates.append({
            "title": title,
            "description": description,
            "url": f"{BRAIN_BASE_URL}{slug}/",
            "added_words": s["added_words"],
            "deleted_words": s["deleted_words"],
            "net_words": net,
            "is_new": is_new,
            "last_commit_date": s["last_commit_date"][:10],
            "first_commit_date": first[:10],
            "path": path,
        })

    updates.sort(key=lambda u: u["net_words"], reverse=True)
    return updates


def _first_sentence(path: Path, max_chars: int = 200) -> str:
    """Fallback when frontmatter has no description."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if text.startswith("---"):
        end = text.find("\n---", 4)
        if end >= 0:
            text = text[end + 4 :].lstrip()
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para or para.startswith(("#", "> [!", "-", "*", "|", "```")):
            continue
        # Strip wikilinks down to display text for the snippet
        para = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", para)
        para = re.sub(r"\s+", " ", para).strip()
        if len(para) > max_chars:
            para = para[:max_chars].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
        return para
    return ""


def _parse_brain_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ----- Books -----

_SKIP_BOOK_FOLDERS = {"Want to Read", "Not-read-anymore", "Goodread (Supplement)"}


def _snapshot_books() -> Path | None:
    """Mirror just the .md files from BOOKS_DIR into .copy/books/ so the script
    never touches the live Second Brain. Returns the snapshot path, or None
    if the source doesn't exist."""
    if not BOOKS_DIR.exists():
        click.echo(f"BOOKS_DIR {BOOKS_DIR} not found, skipping books", err=True)
        return None
    snapshot = COPY_DIR / "books"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    snapshot.mkdir(parents=True)
    count = 0
    for src in BOOKS_DIR.rglob("*.md"):
        rel = src.relative_to(BOOKS_DIR)
        dst = snapshot / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        count += 1
    click.echo(f"  snapshotted {count} book notes to {snapshot}")
    return snapshot


_BOOK_DATE_FIELDS = [
    ("Created", "Created"),
    ("Started", "Started reading"),
    ("Finished", "Finished reading"),
]


def gather_books(since: datetime, limit: int = 5) -> list[dict]:
    """Books where any of Created / Started reading / Finished reading falls
    after `since`. Reads from a local snapshot — never the live vault."""
    snapshot = _snapshot_books()
    if snapshot is None:
        return []

    books = []
    for path in snapshot.rglob("*.md"):
        if path.name.startswith("_"):
            continue
        rel = path.relative_to(snapshot).parts
        if len(rel) > 1 and rel[0] in _SKIP_BOOK_FOLDERS:
            continue

        meta = _parse_book_inline_meta(path)

        all_events = []  # every parseable date, for context
        new_events = []  # dates that fall in this newsletter window
        for label, key in _BOOK_DATE_FIELDS:
            raw = meta.get(key, "")
            m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
            if not m:
                continue
            try:
                dt = datetime.fromisoformat(m.group(1))
            except ValueError:
                continue
            iso = dt.date().isoformat()
            all_events.append((label, iso))
            if dt >= since:
                new_events.append((label, iso))

        if not new_events:
            continue

        sort_dt = max(datetime.fromisoformat(iso) for _, iso in new_events)

        books.append({
            "title": path.stem,
            "author": meta.get("Author", "").strip("[]"),
            "genre": meta.get("Genre", "").strip(),
            "events": all_events,
            "new_events": new_events,
            "summary": _extract_book_summary(path),
            "notes": _extract_book_notes(path),
            "sort_dt": sort_dt,
        })

    books.sort(key=lambda b: b["sort_dt"], reverse=True)
    return books[:limit]


def _parse_book_inline_meta(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^\s*-\s+([A-Za-z][^:]*):\s*(.*)$", line)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def _extract_book_summary(path: Path) -> str:
    """Pull the `> [!summary]` callout body."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^> \[!summary\][^\n]*\n((?:^>.*\n?)*)", text, re.MULTILINE)
    if not m:
        return ""
    body = "\n".join(re.sub(r"^>\s?", "", ln) for ln in m.group(1).splitlines()).strip()
    if body.lower() in {"todo", "tbd"} or "Write a summary" in body:
        return ""
    return body


def _extract_book_notes(path: Path, max_chars: int = 800) -> str:
    """Pull the `## Notes During Reading` body, dropping placeholders and
    wikilink syntax (book notes may reference private-vault notes)."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(
        r"^##\s+Notes\s+During\s+Reading[^\n]*\n(.*?)(?=^##\s|\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    if not m:
        return ""

    lines = []
    for line in m.group(1).splitlines():
        if line.strip() in {"", "-", "- ...", "-..."} or re.match(r"^-\s*\.{2,}$", line.strip()):
            continue
        lines.append(line)
    notes = "\n".join(lines).strip()

    # Strip wikilink syntax to plain text (private-vault links can't be resolved publicly)
    notes = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", notes)

    # If the only remaining content is sub-headings, treat the section as empty
    if not any(ln.strip() and not ln.strip().startswith("#") for ln in notes.splitlines()):
        return ""

    if len(notes) > max_chars:
        notes = notes[:max_chars].rsplit(" ", 1)[0] + "…"
    return notes.strip()


# ----- Bluesky -----

def gather_bluesky(
    since: datetime,
    top_n: int = DEFAULT_BSKY_TOP,
    recent_n: int = DEFAULT_BSKY_RECENT,
) -> dict[str, list[dict]]:
    """Fetch the author feed once, return both top-by-engagement and most-recent
    posts. `recent` is deduped against `top` so the same post never appears twice."""
    url = (
        f"https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
        f"?actor={BSKY_DID}&limit=100"
    )
    since_str = since.strftime("%Y-%m-%d")
    try:
        rows = duckdb.sql(f"""
            INSTALL httpfs; LOAD httpfs;
            WITH raw AS (SELECT * FROM read_json_auto('{url}')),
            unnested AS (SELECT unnest(feed) AS p FROM raw),
            data AS (
                SELECT
                    p.post.uri AS uri,
                    p.post.record.text AS text,
                    p.post.record.createdAt AS created_at,
                    p.post.replyCount AS replies,
                    p.post.repostCount AS reposts,
                    p.post.likeCount AS likes,
                    p.post.quoteCount AS quotes,
                    (p.post.replyCount + p.post.repostCount +
                     p.post.likeCount  + p.post.quoteCount) AS engagement
                FROM unnested
                WHERE p.post.author.handle = '{BSKY_HANDLE}'
                  AND p.post.record.createdAt >= '{since_str}'
            ),
            ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (ORDER BY engagement DESC, created_at DESC) AS rn_eng,
                    ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn_recent
                FROM data
            )
            SELECT uri, text, created_at, engagement, replies, reposts, likes, quotes,
                   rn_eng, rn_recent
            FROM ranked
            WHERE rn_eng <= {top_n} OR rn_recent <= {recent_n}
        """).fetchall()
    except Exception as e:
        click.echo(f"Bluesky fetch failed: {e}", err=True)
        return {"top": [], "recent": []}

    top, recent = [], []
    for uri, text, _created, eng, replies, reposts, likes, _quotes, rn_eng, rn_recent in rows:
        rkey = uri.rsplit("/", 1)[-1]
        post = {
            "url": f"https://bsky.app/profile/{BSKY_HANDLE}/post/{rkey}",
            "text": (text or "").strip(),
            "engagement": eng,
            "likes": likes,
            "reposts": reposts,
            "replies": replies,
        }
        if rn_eng <= top_n:
            top.append((rn_eng, post))
        if rn_recent <= recent_n:
            recent.append((rn_recent, post))

    top.sort(key=lambda x: x[0])
    recent.sort(key=lambda x: x[0])
    top_posts = [p for _, p in top]
    top_urls = {p["url"] for p in top_posts}
    recent_posts = [p for _, p in recent if p["url"] not in top_urls]
    return {"top": top_posts, "recent": recent_posts}


# ----- Blog posts (reuse RSS logic) -----

def gather_blog_posts(since: datetime) -> list:
    feed_url = os.getenv("RSS_FEED")
    if not feed_url:
        return []
    try:
        return fetch_rss_feed(feed_url, since)
    except Exception as e:
        click.echo(f"RSS fetch failed: {e}", err=True)
        return []


# ----- CLI -----

@click.group()
def cli():
    """Newsletter automation: gather → edit → send."""


@cli.command()
@click.option("--since", default=None,
              help="Override start date (YYYY-MM-DD). Default: read from .last_newsletter")
@click.option("--threshold-words", default=DEFAULT_THRESHOLD_WORDS, show_default=True,
              help="Min net words changed (added - deleted) for a brain note to count")
@click.option("--major-bucket-words", default=DEFAULT_MAJOR_BUCKET_WORDS, show_default=True,
              help="Brain notes with at least this many net words go in the 'Major' bucket")
@click.option("--brain-limit", default=DEFAULT_BRAIN_LIMIT, show_default=True,
              help="Max brain notes to include (top N by net words changed)")
@click.option("--blog-limit", default=DEFAULT_BLOG_LIMIT, show_default=True,
              help="Max blog posts (kept low since listmonk_rss.py already announces these)")
@click.option("--books-limit", default=DEFAULT_BOOKS_LIMIT, show_default=True)
@click.option("--bluesky-top", default=DEFAULT_BSKY_TOP, show_default=True,
              help="Top N Bluesky posts by engagement")
@click.option("--bluesky-recent", default=DEFAULT_BSKY_RECENT, show_default=True,
              help="Most recent N Bluesky posts (deduped against --bluesky-top)")
def gather(since, threshold_words, major_bucket_words, brain_limit, blog_limit,
           books_limit, bluesky_top, bluesky_recent):
    """Build a draft markdown file from recent content."""
    since_dt = datetime.fromisoformat(since) if since else get_last_newsletter_date()
    click.echo(f"Gathering content since {since_dt.isoformat()}")

    blog_posts = gather_blog_posts(since_dt)[:blog_limit]
    brain_updates = gather_brain_updates(since_dt, threshold_words=threshold_words)[:brain_limit]
    brain_major = [n for n in brain_updates if n["net_words"] >= major_bucket_words]
    brain_minor = [n for n in brain_updates if n["net_words"] < major_bucket_words]
    books = gather_books(since_dt, limit=books_limit)
    bluesky = gather_bluesky(since_dt, top_n=bluesky_top, recent_n=bluesky_recent)

    click.echo(
        f"  blog: {len(blog_posts)}  brain: {len(brain_updates)} "
        f"(major: {len(brain_major)}, minor: {len(brain_minor)})  "
        f"books: {len(books)}  "
        f"bluesky: top {len(bluesky['top'])} / recent {len(bluesky['recent'])}"
    )

    if not any([blog_posts, brain_updates, books, bluesky["top"], bluesky["recent"]]):
        click.echo("Nothing to include. Skipping draft creation.")
        return

    today = datetime.now().date().isoformat()
    out = Template(TEMPLATE_FILE.read_text()).render(
        today=today,
        blog_posts=blog_posts,
        brain_major=brain_major,
        brain_minor=brain_minor,
        books=books,
        bluesky=bluesky,
    )

    DRAFTS_DIR.mkdir(exist_ok=True)
    out_path = DRAFTS_DIR / f"newsletter-{today}.md"
    out_path.write_text(out)
    click.echo(f"\nDraft written: {out_path}")
    click.echo(f"Edit it, then run: `make newsletter-send` to schedule on Listmonk {out_path}")


@cli.command()
@click.argument("draft", type=click.Path(exists=True, path_type=Path))
@click.option("--subject", default=None, help="Email subject (default: '[ssp.sh] Newsletter — <Month YYYY>')")
@click.option("--dry-run", is_flag=True, help="Push to Listmonk with a 10-year delay (for testing)")
def send(draft, subject, dry_run):
    """Push an edited draft to Listmonk as a scheduled campaign."""
    content = draft.read_text()
    if subject is None:
        subject = f"[ssp.sh] Newsletter — {datetime.now().strftime('%B %Y')}"

    list_id = get_list_id(
        host=os.getenv("LISTMONK_HOST"),
        api_user=os.getenv("LISTMONK_API_USER"),
        api_token=os.getenv("LISTMONK_API_TOKEN"),
        list_name=os.getenv("LIST_NAME"),
    )

    success = schedule_campaign(
        host=os.getenv("LISTMONK_HOST"),
        api_user=os.getenv("LISTMONK_API_USER"),
        api_token=os.getenv("LISTMONK_API_TOKEN"),
        list_id=list_id,
        content=content,
        subject=subject,
        dry_run=dry_run,
    )

    if success and not dry_run:
        save_last_newsletter_date(datetime.now())
        click.echo("✓ .last_newsletter advanced")
    elif dry_run:
        click.echo("✓ dry run — .last_newsletter NOT advanced")


if __name__ == "__main__":
    cli()
