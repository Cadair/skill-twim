import os
import random
import logging
from functools import partial

import jinja2
import markdown
import aiohttp_jinja2
from aiohttp import web

from opsdroid.matchers import match_regex, match_always

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


# Webserver Functions

async def prepare_twim_for_template(opsdroid):
    api = opsdroid.default_connector.connection
    twim = await opsdroid.memory.get("twim")
    for t in twim["twim"]:
        t["image"] = api.get_download_url(t["image"]) if ("image" in t and t["image"]) else None
    return twim if twim else {"twim": []}


@aiohttp_jinja2.template('updates.j2')
async def updates_page(opsdroid, request):
    """
    Serve the updates summary page.
    """
    return await prepare_twim_for_template(opsdroid)


@aiohttp_jinja2.template('updates_md.j2')
async def updates_md(opsdroid, request):
    """
    Serve the updates summary page.
    """
    return await prepare_twim_for_template(opsdroid)


def setup(opsdroid):
    """
    Setup the skill. Register the twim route with the webserver.
    """
    app = opsdroid.web_server.web_app
    cwd = os.path.dirname(__file__)

    markdownf = lambda text: jinja2.Markup(markdown.markdown(text))
    aiohttp_jinja2.setup(app,
                         loader=jinja2.FileSystemLoader(cwd),
                         filters={"markdown": markdownf})

    app.router.add_get('/twim', partial(updates_page, opsdroid))
    app.router.add_get('/twim.md', partial(updates_md, opsdroid))


# Helper Functions

async def process_twim_event(opsdroid, roomid, event):
    connector = opsdroid.default_connector

    mxid = event["sender"]
    nick = await connector._get_nick(roomid, event['sender'])
    body = event['content']['body']

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
    event = message.raw_message
    if not event:
        return

    reply_event_id = event["content"].get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id", None)
    if reply_event_id:
        event = await connector.connection.get_event_in_room(message.room, reply_event_id)

    post = await process_twim_event(opsdroid, message.room, event)

    responses = (f"Thanks {post['nick']} I have saved your update.",
                 f"Thanks for the update {post['nick']}.",
                 f"{post['nick']}: I have stored your update.")

    await message.respond(random.choice(responses))

    # Send the update to the echo room.
    if "echo" in connector.rooms:
        await message.respond(markdown.markdown(format_update(post)), room="echo")


@match_regex("^!get updates")
async def update(opsdroid, config, message):
    """
    Send a message into the room with all the updates.
    """
    connector = opsdroid.default_connector
    mxid = message.raw_message["sender"]
    room_name = connector.get_roomname(message.room)
    is_admin = await user_has_pl(connector.connection, message.room, mxid)
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
    mxid = message.raw_message["sender"]
    is_admin = await user_has_pl(connector.connection, message.room, mxid)
    if is_admin:
        await opsdroid.memory.put("twim", {"twim": []})
        await message.respond("Updates cleared")
