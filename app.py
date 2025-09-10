import os
from fastapi import FastAPI, Request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import uvicorn

# Environment variables
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

# Replace these with your actual allowed Slack user IDs
ALLOWED_USERS = {"U03AA1ZBH5F"}

app = FastAPI()
client = WebClient(token=SLACK_BOT_TOKEN)


@app.post("/slack/events")
async def slack_events(request: Request):
    data = await request.json()

    # URL verification (Slack challenge)
    if "challenge" in data:
        return {"challenge": data["challenge"]}

    event = data.get("event", {})
    if event.get("type") == "message" and "bot_id" not in event:
        text = event.get("text", "")
        channel = event.get("channel")
        ts = event.get("ts")

        await handle_mentions(text, channel, ts)

    return {"ok": True}


async def handle_mentions(text: str, channel: str, ts: str):
    """Process all mentions (user or group) in the message text."""
    words = text.split()

    for word in words:
        if word.startswith("<@") and word.endswith(">"):
            mention = word.strip("<@>")

            # --- Case 1: Direct user mention ---
            if mention.startswith("U"):
                if mention in ALLOWED_USERS:
                    await notify_user(mention, text, word, channel, ts)

            # --- Case 2: User group mention ---
            elif mention.startswith("subteam^S"):
                group_id = mention.split("^")[1]  # Extract S12345
                members = get_usergroup_members(group_id)
                for user_id in members:
                    if user_id in ALLOWED_USERS:
                        await notify_user(user_id, text, word, channel, ts)


async def notify_user(user_id: str, full_text: str, mention: str, channel: str, ts: str):
    """Send DM to a user with truncated message and permalink."""
    # Clean and truncate task text
    task_text = full_text.replace(mention, "").strip()
    truncated = task_text[:100] + "..." if len(task_text) > 100 else task_text

    # Get message link
    link = await get_permalink(channel, ts)

    # Send DM
    await send_dm(user_id, truncated, link)


def get_usergroup_members(group_id: str):
    """Fetch members of a user group (needs usergroups:read scope)."""
    try:
        resp = client.usergroups_users_list(usergroup=group_id)
        return resp.get("users", [])
    except SlackApiError as e:
        print(f"Error fetching user group members: {e}")
        return []


async def get_permalink(channel: str, ts: str):
    """Fetch permalink to a Slack message."""
    try:
        resp = client.chat_getPermalink(channel=channel, message_ts=ts)
        return resp["permalink"]
    except SlackApiError as e:
        print(f"Error getting permalink: {e}")
        return "#"


async def send_dm(user_id: str, text: str, link: str):
    """Send a private DM to the user."""
    try:
        # Open IM channel with user
        resp = client.conversations_open(users=user_id)
        im_channel = resp["channel"]["id"]

        # Send message
        message = f"New task assigned:\n{text}\n{link}"
        client.chat_postMessage(channel=im_channel, text=message)
    except SlackApiError as e:
        print(f"Error sending DM: {e}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
