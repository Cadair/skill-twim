import os
import logging
from functools import partial

import jinja2
import markdown
import aiohttp_jinja2
from aiohttp import web

from opsdroid.matchers import match_regex, match_always


# Templates for the !get updates command

user_link = "https://matrix.to/#/{mxid}"
event_link = "https://matrix.to/#/{room}/{event_id}"

user_template = "[{{nick}}]({user_link}) [reports that]({event_link}):".format(user_link=user_link,
                                                                               event_link=event_link)

post_template = """
{user_template}


> {{message}}

""".format(user_template=user_template)


cwd = os.path.dirname(__file__)


@aiohttp_jinja2.template('updates.j2')
async def updates_page(opsdroid, request):
    """
    Serve the updates summary page.
    """
    twim = await opsdroid.memory.get("twim")
    return twim if twim else {"twim": []}


@aiohttp_jinja2.template('updates_md.j2')
async def updates_md(opsdroid, request):
    """
    Serve the updates summary page.
    """
    twim = await opsdroid.memory.get("twim")
    return twim if twim else {"twim": []}


def setup(opsdroid):
    """
    Setup the skill. Register the twim route with the webserver.
    """
    app = opsdroid.web_server.web_app

    markdownf = lambda text: jinja2.Markup(markdown.markdown(text))
    aiohttp_jinja2.setup(app,
                         loader=jinja2.FileSystemLoader(cwd),
                         filters={"markdown": markdownf})

    app.router.add_get('/twim', partial(updates_page, opsdroid))
    app.router.add_get('/twim.md', partial(updates_md, opsdroid))


@match_regex("^TWIM")
async def twim_bot(opsdroid, config, message):
    """
    React to a TWIM post.

    Check the contents of the message then put it in the opsdroid memory.
    """
    connector = opsdroid.default_connector
    bot_nick = connector.nick
    bot_mxid = connector.mxid

    if not bot_nick in message.text or bot_mxid in message.text:
        return

    event = message.raw_message
    if not event:
        return

    if event["content"].get("m.relates_to", {}).get("m.in_reply_to", None):
        return

    mxid = event["sender"]

    twim = await opsdroid.memory.get("twim")
    if not twim:
        twim = {"twim": []}

    twim["twim"].append({"nick": message.user, "mxid": mxid,
                         "message": message.text,
                         "event_id": event["event_id"],
                         "room": message.room})

    await opsdroid.memory.put("twim", twim)

    await message.respond(f"Thanks for the update {message.user}")


async def get_updates(opsdroid):
    """
    Get the messages for all the updates.
    """
    twim = await opsdroid.memory.get("twim")
    twim = twim if twim else {"twim": []}
    twim = twim["twim"]
    for post in twim:
        post["message"] = post["message"].replace("TWIM: ", "").replace("TWIM", "").replace("\n", "\n>")
    return [post_template.format(**post) for post in twim]


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


async def user_has_pl(api, room_id, mxid, pl=100):
    """
    Determine if a user is admin in a given room.
    """
    pls = await api.get_power_levels(room_id)
    users = pls["users"]
    user_pl = users.get(mxid, 0)
    return user_pl == pl


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
