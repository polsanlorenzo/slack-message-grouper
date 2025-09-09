import os
from fastapi import FastAPI, Request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import uvicorn

# Environment variables
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
ALLOWED_USERS = {"U03AA1ZBH5F", }  # Replace with real Slack user IDs

app = FastAPI()
client = WebClient(token=SLACK_BOT_TOKEN)

@app.post("/slack/events")
async def slack_events(request: Request):
    data = await request.json()

    # URL verification challenge
    if "challenge" in data:
        return {"challenge": data["challenge"]}

    event = data.get("event", {})
    if event.get("type") == "message" and "bot_id" not in event:
        text = event.get("text", "")
        mentioned_users = [word for word in text.split() if word.startswith("<@") and word.endswith(">")]

        for mention in mentioned_users:
            user_id = mention.strip("<@>")
            if user_id not in ALLOWED_USERS:
                continue

            # Get permalink
            channel = event.get("channel")
            ts = event.get("ts")
            link = await get_permalink(channel, ts)

            # Truncate text for DM
            task_text = text.replace(mention, "").strip()
            truncated_text = task_text[:100] + "..." if len(task_text) > 100 else task_text

            # Send DM
            await send_dm(user_id, truncated_text, link)

    return {"ok": True}

async def get_permalink(channel, ts):
    try:
        resp = client.chat_getPermalink(channel=channel, message_ts=ts)
        return resp["permalink"]
    except SlackApiError as e:
        print(f"Error getting permalink: {e}")
        return "#"

async def send_dm(user_id, text, link):
    try:
        # Open IM channel with user
        resp = client.conversations_open(users=user_id)
        im_channel = resp["channel"]["id"]

        # Send new message
        message = f"New task assigned:\n{text}\n{link}"
        client.chat_postMessage(channel=im_channel, text=message)
    except SlackApiError as e:
        print(f"Error sending DM: {e}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
