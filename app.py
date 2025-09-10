import os
import re
import json
from fastapi import FastAPI, Request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import uvicorn

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
ALLOWED_USERS = {"U03AA1ZBH5F"}  # Replace with your real IDs

app = FastAPI()
client = WebClient(token=SLACK_BOT_TOKEN)


@app.post("/slack/events")
async def slack_events(request: Request):
    data = await request.json()
    print("=== Incoming Slack event ===")
    print(json.dumps(data, indent=2))

    if "challenge" in data:
        return {"challenge": data["challenge"]}

    event = data.get("event", {})
    if event.get("type") == "message" and "bot_id" not in event:
        text = event.get("text", "")
        channel = event.get("channel")
        ts = event.get("ts")
        sender_id = event.get("user")

        # Case 1: DM command "remove task N"
        if event.get("channel_type") == "im":
            match = re.match(r"remove task (\d+)", text.strip(), re.I)
            if match:
                task_num = int(match.group(1))
                await remove_task(sender_id, task_num)
                return {"ok": True}

        # Case 2: Mentions in channels / groups
        await handle_mentions(text, channel, ts, sender_id)

    return {"ok": True}


async def handle_mentions(text: str, channel: str, ts: str, sender_id: str):
    words = text.split()
    for word in words:
        if word.startswith("<@") and word.endswith(">"):
            mention = word.strip("<@>")
            if mention.startswith("U") and mention in ALLOWED_USERS:
                await add_task(mention, text, word, channel, ts, sender_id)
        elif word.startswith("<!subteam^") and word.endswith(">"):
            group_id = word.split("^")[1].strip(">")
            members = get_usergroup_members(group_id)
            for user_id in members:
                if user_id in ALLOWED_USERS:
                    await add_task(user_id, text, word, channel, ts, sender_id)


async def add_task(user_id: str, full_text: str, mention: str, channel: str, ts: str, sender_id: str):
    task_text = full_text.replace(mention, "").strip()
    truncated = task_text[:100] + "..." if len(task_text) > 100 else task_text
    link = await get_permalink(channel, ts)
    sender_name = get_username(sender_id)
    new_task = f"{truncated} (from {sender_name}) - {link}"

    im_channel, old_ts, tasks = get_latest_tasklist(user_id)
    tasks.append(new_task)
    await post_tasklist(im_channel, tasks, old_ts, user_id)


async def remove_task(user_id: str, task_num: int):
    """Remove a task by its number and update the list."""
    im_channel, old_ts, tasks = get_latest_tasklist(user_id)

    if not tasks:
        client.chat_postMessage(channel=im_channel, text="No task list found.")
        return

    if task_num < 1 or task_num > len(tasks):
        client.chat_postMessage(channel=im_channel, text=f"Task {task_num} does not exist.")
        return

    removed = tasks.pop(task_num - 1)
    await post_tasklist(im_channel, tasks, old_ts, user_id, removed_task=removed)


async def post_tasklist(im_channel: str, tasks: list, old_ts: str, user_id: str, removed_task: str = None):
    if tasks:
        body = "Remaining tasks:\n" + "\n".join(
            [f"{i+1}. {task}" for i, task in enumerate(tasks)]
        )
    else:
        body = "🎉 All tasks completed!"

    if removed_task:
        body = f"✅ Removed: {removed_task}\n\n" + body

    try:
        resp = client.chat_postMessage(channel=im_channel, text=body)
        if old_ts:
            client.chat_delete(channel=im_channel, ts=old_ts)
    except SlackApiError as e:
        print(f"[ERROR] Posting task list: {e.response['error']}")


def get_latest_tasklist(user_id: str):
    try:
        resp = client.conversations_open(users=user_id)
        im_channel = resp["channel"]["id"]

        history = client.conversations_history(channel=im_channel, limit=20)
        for msg in history["messages"]:
            if msg.get("text", "").startswith("Remaining tasks:") or msg.get("text", "").startswith("✅ Removed:"):
                lines = [line for line in msg["text"].splitlines() if re.match(r"^\d+\.", line)]
                tasks = [re.sub(r"^\d+\.\s*", "", line) for line in lines]
                return im_channel, msg["ts"], tasks

        return im_channel, None, []
    except SlackApiError as e:
        print(f"[ERROR] Fetching task list: {e.response['error']}")
        return None, None, []


def get_usergroup_members(group_id: str):
    try:
        resp = client.usergroups_users_list(usergroup=group_id)
        return resp.get("users", [])
    except SlackApiError as e:
        print(f"[ERROR] Fetching group members: {e.response['error']}")
        return []


def get_username(user_id: str) -> str:
    try:
        resp = client.users_info(user=user_id)
        return resp["user"]["profile"].get("display_name") or resp["user"]["real_name"]
    except SlackApiError as e:
        print(f"[ERROR] Getting username for {user_id}: {e.response['error']}")
        return f"<@{user_id}>"


async def get_permalink(channel: str, ts: str):
    try:
        resp = client.chat_getPermalink(channel=channel, message_ts=ts)
        return resp["permalink"]
    except SlackApiError as e:
        print(f"[ERROR] Getting permalink: {e.response['error']}")
        return "#"


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
