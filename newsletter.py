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
BSKY_FETCH_AMOUNT = 15

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

DEFAULT_THRESHOLD = 20  # min added lines to count a brain note as a meaningful update
DEFAULT_LOOKBACK_DAYS = 60  # used when .last_newsletter doesn't exist yet
MAJOR_BUCKET_LINES = 100  # brain notes with >= this many lines added go in "Major" bucket


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

def gather_brain_updates(since: datetime, threshold: int) -> list[dict]:
    """Notes in BRAIN_CONTENT with at least `threshold` added lines since `since`."""
    if not BRAIN_CONTENT.exists():
        click.echo(f"BRAIN_CONTENT {BRAIN_CONTENT} not found, skipping brain updates", err=True)
        return []

    result = subprocess.run(
        [
            "git", "-C", str(BRAIN_CONTENT), "log",
            f"--since={since.strftime('%Y-%m-%d %H:%M:%S')}",
            "--numstat", "--format=__COMMIT__%H|%ai",
        ],
        capture_output=True, text=True, check=True,
    )

    stats = defaultdict(lambda: {"added": 0, "deleted": 0, "last_commit_date": ""})
    current_date = ""
    for line in result.stdout.splitlines():
        if line.startswith("__COMMIT__"):
            _, _, rest = line.partition("__COMMIT__")
            _, _, current_date = rest.partition("|")
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        added, deleted, path = int(parts[0]), int(parts[1]), parts[2]
        if not path.endswith(".md"):
            continue
        s = stats[path]
        s["added"] += added
        s["deleted"] += deleted
        if not s["last_commit_date"]:  # git log is reverse-chrono, first seen = most recent
            s["last_commit_date"] = current_date

    updates = []
    for path, s in stats.items():
        if s["added"] < threshold:
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
        updates.append({
            "title": title,
            "description": description,
            "url": f"{BRAIN_BASE_URL}{slug}/",
            "added": s["added"],
            "deleted": s["deleted"],
            "last_commit_date": s["last_commit_date"][:10],
            "path": path,
        })

    updates.sort(key=lambda u: u["added"], reverse=True)
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

def gather_bluesky(since: datetime, top_n: int = BSKY_FETCH_AMOUNT) -> list[dict]:
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
            )
            SELECT uri, text, created_at, engagement, replies, reposts, likes, quotes
            FROM data
            WHERE created_at >= '{since_str}'
            ORDER BY engagement DESC
            LIMIT {top_n}
        """).fetchall()
    except Exception as e:
        click.echo(f"Bluesky fetch failed: {e}", err=True)
        return []

    posts = []
    for uri, text, _created, eng, replies, reposts, likes, _quotes in rows:
        rkey = uri.rsplit("/", 1)[-1]
        posts.append({
            "url": f"https://bsky.app/profile/{BSKY_HANDLE}/post/{rkey}",
            "text": (text or "").strip(),
            "engagement": eng,
            "likes": likes,
            "reposts": reposts,
            "replies": replies,
        })
    return posts


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
@click.option("--threshold", default=DEFAULT_THRESHOLD, show_default=True,
              help="Min added lines for a brain note to count as a meaningful update")
@click.option("--brain-limit", default=20, show_default=True,
              help="Max brain notes to include (top N by lines added)")
@click.option("--blog-limit", default=2, show_default=True,
              help="Max blog posts (kept low since listmonk_rss.py already announces these)")
@click.option("--books-limit", default=5, show_default=True)
@click.option("--bluesky-top", default=15, show_default=True)
def gather(since, threshold, brain_limit, blog_limit, books_limit, bluesky_top):
    """Build a draft markdown file from recent content."""
    since_dt = datetime.fromisoformat(since) if since else get_last_newsletter_date()
    click.echo(f"Gathering content since {since_dt.isoformat()}")

    blog_posts = gather_blog_posts(since_dt)[:blog_limit]
    brain_updates = gather_brain_updates(since_dt, threshold=threshold)[:brain_limit]
    brain_major = [n for n in brain_updates if n["added"] >= MAJOR_BUCKET_LINES]
    brain_minor = [n for n in brain_updates if n["added"] < MAJOR_BUCKET_LINES]
    books = gather_books(since_dt, limit=books_limit)
    bluesky = gather_bluesky(since_dt, top_n=bluesky_top)

    click.echo(
        f"  blog: {len(blog_posts)}  brain: {len(brain_updates)} "
        f"(major: {len(brain_major)}, minor: {len(brain_minor)})  "
        f"books: {len(books)}  bluesky: {len(bluesky)}"
    )

    if not any([blog_posts, brain_updates, books, bluesky]):
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
    click.echo(f"Edit it, then run:  uv run python newsletter.py send {out_path}")


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
