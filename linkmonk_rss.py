import os
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import feedparser
from jinja2 import Template
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import click

# Load environment variables
load_dotenv()

# Constants
STATE_FILE = Path("last_update.json")
TEMPLATE_FILE = Path("template.md.j2")

def get_opengraph_data(url):
    response = httpx.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    og_data = {}
    for meta in soup.find_all('meta'):
        prop = meta.get('property', '')
        if prop.startswith('og:'):
            key = prop[3:]
            og_data[key] = meta.get('content', '')
    return og_data


def load_last_update() -> datetime:
    """Load the last update timestamp from the state file."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return datetime.fromisoformat(data["last_update"])
    return datetime.min


def save_last_update(timestamp: datetime):
    """Save the last update timestamp to the state file."""
    with open(STATE_FILE, "w") as f:
        json.dump({"last_update": timestamp.isoformat()}, f)


def fetch_rss_feed(feed_url: str, last_update: datetime) -> list:
    """Fetch and parse RSS feed, returning new items since last update."""
    feed = feedparser.parse(feed_url)
    new_items = []
    for entry in feed.entries:
        if datetime(*entry.published_parsed[:6]) > last_update:
            og = get_opengraph_data(entry.link)
            if og.get("image"):
                entry.media_content=og.get("image")
            new_items.append(entry)

    return new_items


def get_list_id(host: str, api_user: str, api_token: str, list_name: str) -> int:
    """Get list ID from list name using Linkmonk API."""
    url = f"{host}/api/lists"
    auth=(api_user, api_token)
    headers = {
        "Content-Type": "application/json"
    }
    
    with httpx.Client() as client:
        response = client.get(url, headers=headers, auth=auth )
        response.raise_for_status()
        
        # Find the list with matching name
        lists = response.json()["data"]["results"]
        for lst in lists:
            if lst["name"] == list_name:
                return lst["id"]
        
        raise ValueError(f"List '{list_name}' not found")


def create_campaign_content(items: list, template: Template) -> str:
    """Generate campaign content using Jinja2 template."""
    return template.render(items=items)


def send_campaign(host: str, api_user: str, api_token: str, list_id: int, content: str):
    """Send campaign using Linkmonk API."""
    url = f"{host}/api/campaigns"
    auth=(api_user, api_token)
    headers = {
        "Content-Type": "application/json"
    }
    current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M")

    # for the send time, we assume that the linkmonk server runs in UTC (this
    # is what the default in PikaPods are)
    delay_mins = int(os.getenv("SEND_DELAY", 30))
    send_datetime = datetime.now(timezone.utc) + timedelta(minutes=delay_mins)
    send_datetime = send_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {
        "name" : f"RSS Update Newsletter, {current_datetime}",
        "subject": "Latest Updates from RSS Feed",
        "lists": [list_id],
        "body": content,
        "content_type": "markdown",
        "type": "regular",
        "send_at" : send_datetime
    }
    
    with httpx.Client() as client:
        response = client.post(url, json=data, headers=headers,auth=auth)
        response.raise_for_status()

        parsed = response.json()
        assert parsed.get("data",{}).get("id",None), "Cannot get the id of the created campaign"
        campaign_id = parsed.get("data",{}).get("id")

        print(f"Campaign draft {campaign_id} successfully created!")

        
    url = f"{url}/{campaign_id}/status"
    data = {"status": "scheduled"}
    headers = {
        "Content-Type": "application/json"
    }
    
    with httpx.Client() as client:
        response = client.put(url, json=data, auth=auth)
        response.raise_for_status()

        assert parsed.get("data",{}).get("id",None) == campaign_id, f"Cannot schedule campaign {campaign_id}"

        print(f"Campaign {campaign_id} successfully scheduled with {delay_mins} mins delay!")

    return True

@click.command()
@click.option("--dry-run", is_flag=True, help="Run without sending campaign")
def main(dry_run: bool):
    # Load template
    template = Template(TEMPLATE_FILE.read_text())
    
    # Get last update time
    last_update = load_last_update()
    
    # Fetch new RSS items
    items = fetch_rss_feed(os.getenv("RSS_FEED"), last_update)
    
    if not items:
        print("No new items found.")
        return
    
    # Create campaign content
    content = create_campaign_content(items, template)
    
    if dry_run:
        print("Dry run - would send campaign with content:")
        print(content)
        return
    
    # Get list ID
    list_id = get_list_id(
        host=os.getenv("LINKMONK_HOST"),
        api_user=os.getenv("LINKMONK_API_USER"),
        api_token=os.getenv("LINKMONK_API_TOKEN"),
        list_name=os.getenv("LIST_NAME")
    )
    
    # Send campaign
    success = send_campaign(
        host=os.getenv("LINKMONK_HOST"),
        api_user=os.getenv("LINKMONK_API_USER"),
        api_token=os.getenv("LINKMONK_API_TOKEN"),
        list_id=list_id,
        content=content
    )
    
    # Update last update time
    if success:
        save_last_update(datetime.now())


if __name__ == "__main__":
    main()
