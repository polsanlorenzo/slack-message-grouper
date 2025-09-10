import os
import json
from fastapi import FastAPI, Request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import uvicorn

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
ALLOWED_USERS = {"U03AA1ZBH5F"}  # Replace with real IDs

app = FastAPI()
client = WebClient(token=SLACK_BOT_TOKEN)


@app.post("/slack/events")
async def slack_events(request: Request):
    data = await request.json()

    # Log entire request for debugging
    print("=== Incoming Slack event ===")
    print(json.dumps(data, indent=2))

    if "challenge" in data:  # URL verification
        return {"challenge": data["challenge"]}

    event = data.get("event", {})
    if event.get("type") == "message" and "bot_id" not in event:
        text = event.get("text", "")
        channel = event.get("channel")
        ts = event.get("ts")
        sender_id = event.get("user")

        print(f"[INFO] Handling message: {text} from {sender_id}")
        await handle_mentions(text, channel, ts, sender_id)

    return {"ok": True}


async def handle_mentions(text: str, channel: str, ts: str, sender_id: str):
    words = text.split()
    print(f"[DEBUG] Words detected: {words}")

    for word in words:
        if word.startswith("<@") and word.endswith(">"):
            mention = word.strip("<@>")
            print(f"[DEBUG] Found mention: {mention}")

            # --- Case 1: Direct user mention ---
            if mention.startswith("U"):
                if mention in ALLOWED_USERS:
                    await notify_user(mention, text, word, channel, ts, sender_id)
                else:
                    print(f"[DEBUG] User {mention} not in ALLOWED_USERS")

            # --- Case 2: User group mention ---
            elif mention.startswith("subteam^S"):
                group_id = mention.split("^")[1]
                print(f"[DEBUG] User group mention: {group_id}")

                members = get_usergroup_members(group_id)
                print(f"[DEBUG] Group members: {members}")

                for user_id in members:
                    if user_id in ALLOWED_USERS:
                        await notify_user(user_id, text, word, channel, ts, sender_id)
                    else:
                        print(f"[DEBUG] Skipping {user_id}, not in ALLOWED_USERS")


async def notify_user(user_id: str, full_text: str, mention: str, channel: str, ts: str, sender_id: str):
    """Send DM with who pinged + truncated text + link."""
    task_text = full_text.replace(mention, "").strip()
    truncated = task_text[:100] + "..." if len(task_text) > 100 else task_text
    link = await get_permalink(channel, ts)

    sender_name = get_username(sender_id)

    message = f"New task from {sender_name}:\n{truncated}\n{link}"
    print(f"[INFO] Notifying {user_id} with: {message}")
    await send_dm(user_id, message)


def get_usergroup_members(group_id: str):
    try:
        resp = client.usergroups_users_list(usergroup=group_id)
        print(f"[DEBUG] usergroups.users.list response: {resp}")
        return resp.get("users", [])
    except SlackApiError as e:
        print(f"[ERROR] Fetching group members: {e.response['error']}")
        return []


def get_username(user_id: str) -> str:
    """Resolve user ID into Slack display name."""
    try:
        resp = client.users_info(user=user_id)
        return resp["user"]["profile"].get("display_name") or resp["user"]["real_name"]
    except SlackApiError as e:
        print(f"[ERROR] Getting username for {user_id}: {e.response['error']}")
        return f"<@{user_id}>"


async def get_permalink(channel: str, ts: str):
    try:
        resp = client.chat_getPermalink(channel=channel, message_ts=ts)
        print(f"[DEBUG] Permalink: {resp['permalink']}")
        return resp["permalink"]
    except SlackApiError as e:
        print(f"[ERROR] Getting permalink: {e.response['error']}")
        return "#"


async def send_dm(user_id: str, message: str):
    try:
        resp = client.conversations_open(users=user_id)
        im_channel = resp["channel"]["id"]
        print(f"[DEBUG] Opened IM channel {im_channel} for {user_id}")

        client.chat_postMessage(channel=im_channel, text=message)
        print(f"[INFO] DM sent to {user_id}")
    except SlackApiError as e:
        print(f"[ERROR] Sending DM: {e.response['error']}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
