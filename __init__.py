import os
import random
import logging
from functools import partial

import jinja2
import markdown
import aiohttp_jinja2
from aiohttp import web

from opsdroid.matchers import match_regex, match_always
from opsdroid import events

_LOGGER = logging.getLogger(__name__)

# Templates for the !get updates command
# TODO: Combine these with the jinja stuff somehow

user_link = "https://matrix.to/#/{mxid}"
event_link = "https://matrix.to/#/{room}/{event_id}"

user_template = "[{{nick}}]({user_link}) [reports that]({event_link}):".format(user_link=user_link,
                                                                               event_link=event_link)

post_template = """
{user_template}


> {{message}}

""".format(user_template=user_template)


# Helper Functions

def trim_reply_fallback_text(text: str) -> str:
    # Copyright (C) 2018 Tulir Asokan
    # Borrowed from https://github.com/tulir/mautrix-telegram/blob/master/mautrix_telegram/formatter/util.py
    # Having been given explicit permission to include it "under the terms of any OSI approved licence"
    # https://matrix.to/#/!FPUfgzXYWTKgIrwKxW:matrix.org/$15365871364925maRqg:maunium.net

    if not text.startswith("> ") or "\n" not in text:
        return text
    lines = text.split("\n")
    while len(lines) > 0 and lines[0].startswith("> "):
        lines.pop(0)
    return "\n".join(lines).strip()


async def process_twim_event(opsdroid, roomid, event):
    connector = opsdroid.default_connector

    mxid = event["sender"]
    nick = await connector.get_nick(roomid, event['sender'])
    body = event['content']['body']

    # If this message is a reply then trim the reply fallback
    if event["content"].get("m.relates_to", {}).get("m.in_reply_to", None):
        body = trim_reply_fallback_text(body)

    msgtype = event["content"]["msgtype"]
    if msgtype == "m.image":
        image = event['content']['url']
    else:
        image = None

    post = {"nick": nick, "mxid": mxid,
            "message": body,
            "event_id": event["event_id"],
            "room": roomid,
            "image": image}

    twim = await opsdroid.memory.get("twim")
    if not twim:
        twim = {"twim": []}

    twim["twim"].append(post)
    await opsdroid.memory.put("twim", twim)

    return post


def format_update(post):
    message = post["message"]
    if "TWIM: " in message:
        message = message.replace("TWIM: ", "", 1)
    else:
        message = message.replace("TWIM", "", 1)
    post["message"] = message.replace("\n", "\n>")
    if "image" in post and post["image"]:
        post["message"] = f"\n![]({post['image']})" + post["message"]
    return post_template.format(**post)


async def get_updates(opsdroid):
    """
    Get the messages for all the updates.
    """
    twim = await opsdroid.memory.get("twim")
    twim = twim if twim else {"twim": []}
    return [format_update(post) for post in twim["twim"]]


async def user_has_pl(api, room_id, mxid, pl=100):
    """
    Determine if a user is admin in a given room.
    """
    pls = await api.get_power_levels(room_id)
    users = pls["users"]
    user_pl = users.get(mxid, 0)
    return user_pl == pl


# Matcher Functions

@match_regex("^TWIM")
async def twim_bot(opsdroid, config, message):
    """
    React to a TWIM post.

    Check the contents of the message then put it in the opsdroid memory.
    """
    connector = opsdroid.default_connector
    event = message.raw_event
    if not event:
        return

    reply_event_id = event["content"].get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id", None)
    if reply_event_id:
        event = await connector.connection.get_event_in_room(message.target, reply_event_id)

    post = await process_twim_event(opsdroid, message.target, event)

    responses = (f"Thanks {post['nick']}; I have saved your update.",
                 f"Thanks {post['nick']}! I have saved your update.",
                 f"Thanks for the update {post['nick']}.",
                 f"{post['nick']}: I have stored your update.")

    await message.respond(random.choice(responses))

    await message.respond(events.Reaction('⭕'))

    # Send the update to the echo room.
    if "echo" in connector.rooms:
        await message.respond(events.Message(markdown.markdown(format_update(post)), target="echo"))


@match_regex("^!get updates")
async def update(opsdroid, config, message):
    """
    Send a message into the room with all the updates.
    """
    connector = opsdroid.default_connector
    mxid = message.raw_event["sender"]
    room_name = connector.get_roomname(message.target)
    is_admin = await user_has_pl(connector.connection, message.target, mxid)
    if room_name == "main" and not is_admin:
        return

    updates = await get_updates(opsdroid)
    twim = await opsdroid.memory.get("twim")
    if twim:
        twim = twim["twim"]
        if not twim:
            await message.respond("No updates yet.")
            return
        response = "\n".join(updates)
        await message.respond(markdown.markdown(response))


@match_regex("^!clear updates")
async def clear_updates(opsdroid, config, message):
    """
    Admin command to clear the memory of the bot.
    """
    connector = opsdroid.default_connector
    mxid = message.raw_event["sender"]
    is_admin = await user_has_pl(connector.connection, message.target, mxid)
    if is_admin:
        await opsdroid.memory.put("twim", {"twim": []})
        await message.respond("Updates cleared")
