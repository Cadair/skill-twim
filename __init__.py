import os
import random
import logging
from functools import partial

import jinja2
import markdown
import aiohttp_jinja2
from aiohttp import web

from opsdroid.matchers import match_regex, match_event
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


MAGIC_EMOJI = '⭕'


# Helper Functions

async def add_post_to_memory(opsdroid, roomid, post):
    twim = await opsdroid.memory.get("twim")
    if not twim:
        twim = {"twim": []}

    twim["twim"].append(post)
    return await opsdroid.memory.put("twim", twim)


async def process_twim_event(opsdroid, roomid, event):
    body = event.raw_event['content']['body']

    image = None
    if isinstance(event, events.Image):
        image = event.url

    post = {"nick": event.user,
            "mxid": event.user_id,
            "message": body,
            "event_id": event.event_id,
            "room": roomid,
            "image": image}

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

@match_event(events.EditedMessage)
async def twim_edit(opsdroid, config, edit):
    """
    """

@match_event(events.Reaction)
async def twim_reaction(opsdroid, config, reaction):
    """
    If the original poster reacts with the magic emoji then TWIM the post.
    """
    if (reaction.emoji != MAGIC_EMOJI
        or reaction.user_id != reaction.linked_event.user_id):
        return

    return await twim_bot(opsdroid, config, reaction.linked_event)


@match_regex("^TWIM")
async def twim_bot(opsdroid, config, message):
    """
    React to a TWIM post.

    Check the contents of the message then put it in the opsdroid memory.
    """
    connector = opsdroid.default_connector

    # If the message starts with TWIM and it's a reply then we use the parent event.
    if isinstance(message, events.Reply):
        message = message.linked_event

    post = await process_twim_event(opsdroid, message.target, message)
    await add_post_to_memory(opsdroid, message.target, post)

    responses = (f"Thanks {post['nick']}; I have saved your update.",
                 f"Thanks {post['nick']}! I have saved your update.",
                 f"Thanks for the update {post['nick']}.",
                 f"{post['nick']}: I have stored your update.")

    await message.respond(random.choice(responses))

    await message.respond(events.Reaction(MAGIC_EMOJI))

    # Send the update to the echo room.
    if "echo" in connector.rooms:
        await message.respond(events.Message(markdown.markdown(format_update(post)), target="echo"))


@match_regex("^!get updates")
async def update(opsdroid, config, message):
    """
    Send a message into the room with all the updates.
    """
    connector = opsdroid.default_connector
    room_name = connector.get_roomname(message.target)
    is_admin = await user_has_pl(connector.connection, message.target, message.user_id)
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
    is_admin = await user_has_pl(connector.connection, message.target, message.user_id)
    if is_admin:
        await opsdroid.memory.put("twim", {"twim": []})
        await message.respond("Updates cleared")
