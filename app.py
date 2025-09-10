# app.py
import os
import re
import json
from typing import Tuple, List, Optional
from fastapi import FastAPI, Request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import uvicorn

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
if not SLACK_BOT_TOKEN:
    raise RuntimeError("Set SLACK_BOT_TOKEN env var")

# put your allowed user IDs here
ALLOWED_USERS = {"U03AA1ZBH5F"}

app = FastAPI()
client = WebClient(token=SLACK_BOT_TOKEN)


@app.post("/slack/events")
async def slack_events(request: Request):
    data = await request.json()

    # dump the whole payload to logs (very helpful for debugging)
    print("=== Incoming Slack event ===")
    print(json.dumps(data, indent=2))

    # URL verification
    if "challenge" in data:
        return {"challenge": data["challenge"]}

    event = data.get("event", {})
    # ignore edited messages and message_changed events for now
    if event.get("type") != "message":
        print("[DEBUG] Ignoring non-message event type")
        return {"ok": True}

    # avoid processing messages that are Slack event subtypes we don't want
    if event.get("subtype"):
        # example subtypes: 'message_changed', 'bot_message', etc.
        print(f"[DEBUG] Ignoring message with subtype: {event.get('subtype')}")
        return {"ok": True}

    text = event.get("text", "") or extract_text_from_blocks(event)
    channel = event.get("channel")
    ts = event.get("ts")
    thread_ts = event.get("thread_ts")  # can be None
    sender_id = event.get("user")  # who typed the message

    print(f"[INFO] Handling message. text='{text}', channel={channel}, ts={ts}, thread_ts={thread_ts}, sender={sender_id}")

    # If this is a DM (im) to the bot, check for remove commands
    if event.get("channel_type") == "im":
        # handle removal command like "remove task 2"
        match = re.match(r"remove task\s+(\d+)", (text or "").strip(), re.I)
        if match:
            n = int(match.group(1))
            print(f"[INFO] Received remove command from {sender_id}: remove task {n}")
            await remove_task(sender_id, n)
            return {"ok": True}

    # Otherwise handle mentions in channel / thread
    await handle_mentions(text or "", channel, ts, thread_ts, sender_id)

    return {"ok": True}


def extract_text_from_blocks(event: dict) -> str:
    """Fallback to extracting text from blocks if event['text'] is empty."""
    blocks = event.get("blocks") or []
    parts = []
    for b in blocks:
        # common block types: section -> text -> text (mrkdwn)
        if "text" in b and isinstance(b["text"], dict):
            parts.append(b["text"].get("text", ""))
        # elements inside section
        if b.get("type") == "section" and "fields" in b:
            for f in b["fields"]:
                if isinstance(f, dict) and "text" in f:
                    parts.append(f["text"])
        # some blocks have 'elements' inside 'accessory' or 'text' -> handle minimally
        if "elements" in b:
            for e in b["elements"]:
                if e.get("type") == "text" and "text" in e:
                    parts.append(e["text"])
    combined = " ".join([p for p in parts if p])
    print(f"[DEBUG] Extracted text from blocks: '{combined}'")
    return combined


async def handle_mentions(text: str, channel: str, ts: str, thread_ts: Optional[str], sender_id: str):
    """Find mentions (user and group) in the text and add tasks for allowlisted users."""
    words = (text or "").split()
    print(f"[DEBUG] Words: {words}")

    for word in words:
        # direct user mention: <@U123...>
        if word.startswith("<@") and word.endswith(">"):
            mention = word.strip("<@>")
            # Slack sometimes adds a pipe and name: <@U12345|name>
            if "|" in mention:
                mention = mention.split("|", 1)[0]
            print(f"[DEBUG] Detected user mention token: {mention}")
            if mention.startswith("U") and mention in ALLOWED_USERS:
                await add_task(mention, text, word, channel, ts, thread_ts, sender_id)

        # user group mention: <!subteam^S123...>
        elif word.startswith("<!subteam^") and word.endswith(">"):
            # token form: <!subteam^S12345|groupname> or <!subteam^S12345>
            inside = word[1:-1]  # drop <> -> !subteam^S12345|groupname
            # take after '^' up to '|' or end
            if "^" in inside:
                after_caret = inside.split("^", 1)[1]
                group_id = after_caret.split("|", 1)[0]
                print(f"[DEBUG] Detected usergroup mention: {group_id}")
                members = get_usergroup_members(group_id)
                print(f"[DEBUG] usergroup members: {members}")
                for uid in members:
                    if uid in ALLOWED_USERS:
                        await add_task(uid, text, word, channel, ts, thread_ts, sender_id)
        else:
            # ignore other tokens
            continue


async def add_task(user_id: str, full_text: str, mention_token: str, channel: str, ts: str, thread_ts: Optional[str], sender_id: str):
    """Append a new task and post an updated task list DM, deleting the old one."""
    # remove mention token from the visible text
    visible_text = full_text.replace(mention_token, "").strip()
    truncated = visible_text[:200] + ("..." if len(visible_text) > 200 else "")
    link = await get_permalink(channel, ts, thread_ts)
    sender_name = get_username(sender_id)
    new_task = f"{truncated} (from {sender_name}) - {link}"

    im_channel, old_ts, tasks = get_latest_tasklist(user_id)
    if im_channel is None:
        print(f"[ERROR] Could not open IM channel for {user_id}")
        return

    tasks.append(new_task)
    await post_tasklist(im_channel, tasks, old_ts)


async def remove_task(user_id: str, task_num: int):
    """Remove task (1-based index) for the user and update task list DM."""
    im_channel, old_ts, tasks = get_latest_tasklist(user_id)
    if im_channel is None:
        print("[INFO] No IM channel available for user when removing")
        return

    if not tasks:
        client.chat_postMessage(channel=im_channel, text="No tasks to remove.")
        return

    if task_num < 1 or task_num > len(tasks):
        client.chat_postMessage(channel=im_channel, text=f"Task {task_num} does not exist.")
        return

    removed = tasks.pop(task_num - 1)
    # post updated list and indicate what was removed
    await post_tasklist(im_channel, tasks, old_ts, removed_task=removed)


def get_latest_tasklist(user_id: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    """Return (im_channel, old_message_ts, tasks[]). If error returns (None, None, [])."""
    try:
        resp = client.conversations_open(users=user_id)
        im_channel = resp["channel"]["id"]
    except SlackApiError as e:
        print(f"[ERROR] conversations_open failed: {e.response.get('error')}")
        return None, None, []

    try:
        history = client.conversations_history(channel=im_channel, limit=50)
        messages = history.get("messages", [])
        # find the most recent bot message that starts with "Remaining tasks:" or "✅ Removed:"
        for msg in messages:
            text = msg.get("text", "")
            if not text:
                continue
            if text.startswith("Remaining tasks:") or text.startswith("✅ Removed:") or text.startswith("🎉 All tasks completed!"):
                # parse numbered lines into tasks
                lines = [line for line in text.splitlines() if re.match(r"^\d+\.", line.strip())]
                tasks = [re.sub(r"^\d+\.\s*", "", line.strip()) for line in lines]
                return im_channel, msg.get("ts"), tasks
        return im_channel, None, []
    except SlackApiError as e:
        print(f"[ERROR] conversations_history failed: {e.response.get('error')}")
        return im_channel, None, []


def get_usergroup_members(group_id: str) -> List[str]:
    try:
        resp = client.usergroups_users_list(usergroup=group_id)
        users = resp.get("users", []) or []
        return users
    except SlackApiError as e:
        print(f"[ERROR] usergroups_users_list failed: {e.response.get('error')}")
        return []


def get_username(user_id: str) -> str:
    try:
        resp = client.users_info(user=user_id)
        u = resp.get("user", {})
        profile = u.get("profile", {}) or {}
        return profile.get("display_name") or profile.get("real_name") or f"<@{user_id}>"
    except SlackApiError as e:
        print(f"[ERROR] users_info failed for {user_id}: {e.response.get('error')}")
        return f"<@{user_id}>"


async def get_permalink(channel: str, ts: str, thread_ts: Optional[str] = None) -> str:
    try:
        target_ts = thread_ts or ts
        resp = client.chat_getPermalink(channel=channel, message_ts=target_ts)
        permalink = resp.get("permalink", "#")
        print(f"[DEBUG] Permalink resolved: {permalink}")
        return permalink
    except SlackApiError as e:
        print(f"[ERROR] chat_getPermalink failed: {e.response.get('error')}")
        return "#"


async def post_tasklist(im_channel: str, tasks: List[str], old_ts: Optional[str], removed_task: Optional[str] = None):
    if tasks:
        body = "Remaining tasks:\n" + "\n".join([f"{i+1}. {task}" for i, task in enumerate(tasks)])
    else:
        body = "🎉 All tasks completed!"

    if removed_task:
        body = f"✅ Removed: {removed_task}\n\n" + body

    try:
        resp = client.chat_postMessage(channel=im_channel, text=body)
        print(f"[INFO] Posted new task list to {im_channel}, ts={resp.get('ts')}")
        if old_ts:
            try:
                client.chat_delete(channel=im_channel, ts=old_ts)
                print(f"[INFO] Deleted old tasklist ts={old_ts}")
            except SlackApiError as e:
                print(f"[WARN] Failed to delete old tasklist: {e.response.get('error')}")
    except SlackApiError as e:
        print(f"[ERROR] chat_postMessage failed: {e.response.get('error')}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
