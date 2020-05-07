import re
import logging
import random
import unicodedata

import matrix_client.errors

from opsdroid import events
from opsdroid.matchers import match_event, match_regex

_LOGGER = logging.getLogger(__name__)

user_link = "https://matrix.to/#/{mxid}"
event_link = "https://matrix.to/#/{room}/{event_id}"

user_template = ' <a href="{user_link}">{{nick}}</a> <a href="{event_link}">reports that</a>:'.format(user_link=user_link,
                                                                                                      event_link=event_link)

post_template = """
{user_template}
<p></p>
<blockquote>
{{message}}
</blockquote>

""" .format(user_template=user_template)

MAGIC_EMOJI = '\u2b55'

TWIM_REGEX = "^TWIM(?:[:\s]|$)"

# Helper Functions

def emoji_is_magic(s1):
    def NFD(s):
        return unicodedata.normalize('NFD', s)
    return NFD(MAGIC_EMOJI) in NFD(s1)


async def add_post_to_memory(opsdroid, roomid, post):
    twim = await opsdroid.memory.get("twim")
    if not twim:
        twim = {"twim": {}}

    twim['twim'] = {**post, **twim['twim']}
    return await opsdroid.memory.put("twim", twim)


async def process_twim_event(opsdroid, roomid, event):
    body = event.raw_event['content'].get('formatted_body',
                                          event.raw_event['content']['body'])

    image = None
    if isinstance(event, events.Image):
        # Get mxc:// url not http url.
        image = event.raw_event['content']['url']

    post = {event.event_id: {"nick": event.user,
                             "mxid": event.user_id,
                             "message": body,
                             "room": roomid,
                             "image": image}}

    return post


def format_update(post):
    event_id = list(post.keys())[0]
    post = post[event_id]
    post['event_id'] = event_id
    post["message"] = re.sub(TWIM_REGEX, "", post["message"])
    post["message"] = re.sub('<a href="https://matrix.to/#/.*">TWIM</a>[:\s]', "", post["message"])

    if "image" in post and post["image"]:
        post["message"] = f"<img src=\"{post['image']}\" alt=\"{post['message']}\" />"

    return post_template.format(**post)


async def get_updates(opsdroid):
    """
    Get the messages for all the updates.
    """
    twim = await opsdroid.memory.get("twim")
    twim = twim or {"twim": {}}
    twim = twim['twim']
    return [format_update({event_id: post}) for event_id, post in twim.items()]


async def user_has_pl(api, room_id, mxid, pl=100):
    """
    Determine if a user is admin in a given room.
    """
    pls = await api.get_power_levels(room_id)
    users = pls["users"]
    user_pl = users.get(mxid, 0)
    return user_pl == pl

# Matcher Functions

@match_event(events.OpsdroidStarted)
async def update_database(opsdroid, config, event):
    """
    Ensure consistency of the database.
    """
    twim = await opsdroid.memory.get("twim")
    if twim is None:
        return

    if isinstance(twim, list):
        new_twim = {}
        for post in twim:
            event_id = post.pop('event_id')
            new_twim[event_id] = post

        await opsdroid.memory.put("twim", {"twim": new_twim})
        _LOGGER.info("Updated TWIM memory format.")


@match_event(events.EditedMessage)
async def twim_edit(opsdroid, config, edit):
    """
    Update a stored TWIM post if an edit arrives.
    """
    twim = await opsdroid.memory.get("twim")
    if twim is None:
        return

    original_event_id = edit.linked_event.event_id
    if original_event_id in twim['twim']:
        post = twim['twim'][original_event_id]
        post['message'] = edit.text

        await opsdroid.memory.put("twim", twim)

        if 'echo_event_id' in post:
            await opsdroid.send(events.EditedMessage(
                format_update({original_event_id: post}),
                target="echo",
                linked_event=post['echo_event_id']))


@match_event(events.Reaction)
async def twim_reaction(opsdroid, config, reaction):
    """
    If the original poster reacts with the magic emoji then TWIM the post.
    """
    if not reaction.linked_event:
        _LOGGER.error("The reaction object has not got a linked_event")
        return

    if reaction.user_id == reaction.linked_event.user_id:
        if emoji_is_magic(reaction.emoji):
            _LOGGER.debug(f"TWIMing original post {reaction.linked_event}")
            return await twim_bot(opsdroid, config, reaction.linked_event)



@match_regex(TWIM_REGEX)
async def twim_bot(opsdroid, config, message):
    """
    React to a TWIM post.

    Check the contents of the message then put it in the opsdroid memory.
    """
    if isinstance(message, events.EditedMessage):
        return

    # If the message starts with TWIM and it's a reply then we use the parent event.
    if isinstance(message, events.Reply):
        message = message.linked_event

    post = await process_twim_event(opsdroid, message.target, message)
    _LOGGER.debug(f"Processed TWIM event, got: {post}")

    content = list(post.values())[0]
    nick = content['nick']

    responses = (f"Thanks {nick}; I have saved your update.",
                 f"Thanks {nick}! I have saved your update.",
                 f"Thanks for the update {nick}.",
                 f"{nick}: I have stored your update.")

    await message.respond(events.Message(random.choice(responses)))

    try:
        await message.respond(events.Reaction(MAGIC_EMOJI))
    except matrix_client.errors.MatrixRequestError:
        _LOGGER.error("Failed to react to submission with magic emoji.")
        pass

    # Send the update to the echo room.
    if "echo" in message.connector.rooms:
        echo_event_id = await message.respond(
            events.Message(format_update(post), target="echo"))
        echo_event_id = echo_event_id['event_id']
        content['echo_event_id'] = echo_event_id

    await add_post_to_memory(opsdroid, message.target, post)


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
    if updates:
        response = "<br />".join(updates)
        await message.respond(response)
    else:
        await message.respond("No updates yet.")


@match_regex("^!clear updates")
async def clear_updates(opsdroid, config, message):
    """
    Admin command to clear the memory of the bot.
    """
    connector = opsdroid.default_connector
    is_admin = await user_has_pl(connector.connection, message.target, message.user_id)
    if is_admin:
        await opsdroid.memory.put("twim", {"twim": {}})
        await message.respond("Updates cleared")
