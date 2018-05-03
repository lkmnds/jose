import collections
import asyncio
import random
import logging
import re

import pymongo
import discord

from discord.ext import commands
from discord.raw_models import RawReactionActionEvent

from .common import Cog

log = logging.getLogger(__name__)

# muh regex
IMAGE_REGEX = re.compile(r'(https?:\/\/.*\.(?:png|jpeg|jpg|gif|webp))',
                         re.M | re.I)
ID_REGEX = re.compile(r'\d+', re.M | re.I)
EMOJI_REGEX = re.compile(r'<a?:\w+:(\d+)>', re.I)

DEFAULT_STAR_EMOJI = '\N{WHITE MEDIUM STAR}'


class StarColor:
    """:releaseColoure:"""
    BLUE = 0x5dadec
    BRONZE = 0xc67931
    SILVER = 0xC0C0C0
    GOLD = 0xD4AF37
    RED = 0xff0000
    WHITE = 0xffffff


class StarAddError(Exception):
    """Error when adding a star to a message"""
    pass


class StarRemoveError(Exception):
    """Error when removing a star"""
    pass


class StarError(Exception):
    """General error"""
    pass


def empty_star_object(message: discord.Message) -> dict:
    """Empty star object generator"""
    return {
        'message_id': message.id,
        'channel_id': message.channel.id,
        'guild_id': message.guild.id,

        # add a bit of context
        'author_id': message.author.id,
        'starrers': [],

        # NOTE: I really hate mongo.
        'starrers_count': 0,
    }


def empty_starconfig(guild) -> dict:
    """Empty star configuration for a guild"""
    log.info(f'Generating starconfig for [{guild!s} {guild.id}]')
    return {
        'guild_id': guild.id,
        'starboard_id': None,

        # can be int or str (unicode emoji)
        # default star
        'star_emoji': DEFAULT_STAR_EMOJI,

        # list of allowed channels to
        # have messages starred from

        # if this is empty, all channels are allowed
        'allowed_chans': [],

        #: how many stars until a starboard message
        #  is posted?
        'threshold': 1,
    }


def get_humans(message) -> int:
    """Get all humans in a guild."""
    humans = sum(1 for m in message.guild.members if not m.bot)

    # Since selfstarring isn't allowed,
    # we need to remove 1 from the total amount.
    humans -= 1

    return 1 if humans < 0 else humans


def make_color(star, message) -> int:
    """Generate a color for a embed depending on its star ratio."""
    color = 0x0
    stars = len(star['starrers'])

    star_ratio = stars / get_humans(message)

    if star_ratio >= 0:
        color = StarColor.BLUE
    if star_ratio >= 0.1:
        color = StarColor.BRONZE
    if star_ratio >= 0.2:
        color = StarColor.SILVER
    if star_ratio >= 0.4:
        color = StarColor.GOLD
    if star_ratio >= 0.8:
        color = StarColor.RED
    if star_ratio >= 1:
        color = StarColor.WHITE

    return color


def get_emoji(star, message) -> str:
    """Generate a jose star emoji depending on its star ratio."""
    emoji = ''
    stars = len(star['starrers'])

    star_ratio = stars / get_humans(message)

    if star_ratio >= 0:
        emoji = '<:josestar1:353997747772456980>'
    if star_ratio >= 0.1:
        emoji = '<:josestar2:353997748216922112>'
    if star_ratio >= 0.2:
        emoji = '<:josestar3:353997748288225290>'
    if star_ratio >= 0.4:
        emoji = '<:josestar4:353997749341126657>'
    if star_ratio >= 0.8:
        emoji = '<:josestar5:353997749949300736>'
    if star_ratio >= 1:
        emoji = '<:josestar6:353997749630402561>'

    return emoji


def make_star_embed(star, message):
    """Create the starboard embed."""
    star_emoji = get_emoji(star, message)
    embed_color = make_color(star, message)

    title = (f'{len(star["starrers"])} {star_emoji} '
             f'{message.channel.mention}, ID: {message.id}')

    content = message.content
    em = discord.Embed(description=content, colour=embed_color)
    em.timestamp = message.created_at

    au = message.author
    avatar = au.avatar_url or au.default_avatar_url
    em.set_author(name=au.display_name, icon_url=avatar)

    # check for image urls
    search_res = IMAGE_REGEX.search(content)
    if search_res:
        em.set_image(url=search_res.group(0))

    attch = message.attachments
    if attch:
        attch_url = attch[0].url
        if attch_url.lower().endswith((
                'png',
                'jpeg',
                'jpg',
                'gif',
        )):
            em.set_image(url=attch_url)
        else:
            attachments = '\n'.join(
                [f'[Attachment]({attch_s.url})' for attch_s in attch])
            em.description += attachments

    return title, em


def check_nsfw(guild, config, message):
    """Check NSFW rules on channels and the current starboard."""
    starboard = guild.get_channel(config['starboard_id'])
    if starboard is None:
        raise StarError('No starboard found')

    nsfw_starboard = starboard.is_nsfw()
    nsfw_message = message.channel.is_nsfw()
    if nsfw_starboard:
        return

    if nsfw_message:
        raise StarError('NSFW message in SFW starboard')


class Starboard(Cog, requires=['config']):
    """Starboard.

    lol starboard u kno the good shit
    """

    def __init__(self, bot):
        super().__init__(bot)
        self.bot.simple_exc.extend([StarError, StarAddError, StarRemoveError])

        # prevent race conditions on all starboard operations
        self._locks = collections.defaultdict(asyncio.Lock)

        # janitor
        #: the janitor semaphore keeps things up and running
        #  by only allowing 1 janitor task each time.
        #  a janitor task cleans stuff out of mongo
        self.janitor_semaphore = asyncio.Semaphore(1)

        # collectiones
        self.starboard_coll = self.config.jose_db['starboard']
        self.starconfig_coll = self.config.jose_db['starconfig']

    async def get_starconfig(self, guild_id: int) -> dict:
        """Get a starboard configuration object for a guild.

        If the guild is blocked, deletes the starboard configuration.
        """

        if await self.bot.is_blocked_guild(guild_id):
            guild = self.bot.get_guild(guild_id)
            g = guild
            res = await self.starconfig_coll.delete_many(
                {'guild_id': g.id})

            log.info(f'Deleted {res.deleted_count} sconfig: `{g.name}[g.id]`'
                     ' from blocking')
            return

        return await self.starconfig_coll.find_one({'guild_id': guild_id})

    async def _get_starconfig(self, guild_id: int) -> dict:
        """Same as :meth:`Starboard.get_starconfig` but raises `StarError` when
        no configuration is found.
        """
        cfg = await self.get_starconfig(guild_id)
        if not cfg:
            raise StarError('No starboard configuration was '
                            'found for this guild')

        return cfg

    async def get_star(self, guild_id: int, message_id: int) -> dict:
        """Get a star object from a guild+message ID pair."""
        return await self.starboard_coll.find_one({
            'message_id': message_id,
            'guild_id': guild_id
        })

    async def janitor_task(self, guild_id: int):
        """Deletes all star objects that refer to a specific Guild ID.

        This will aquire the :attr:`Stars.janitor_semaphore` semaphore,
        and because of that, it will block the calling coroutine until
        some other coroutine releases the semaphore.
        """
        try:
            await self.janitor_semaphore.acquire()

            log.warning('[janitor] deleting star objectss from %d', guild_id)
            res = await self.starboard_coll.delete_many({'guild_id': guild_id})
            g = self.bot.get_guild(guild_id)

            log.warning('[janitor] Deleted %d star objects from '
                        'janitoring %s[%d]', res.deleted_count, g.name, g.id)

        except Exception:
            log.exception('error on janitor task')
        finally:
            self.janitor_semaphore.release()

    async def raw_add_star(self, config: dict, message: discord.Message,
                           author_id: int) -> dict:
        """Add a star to a message.

        Returns
        -------
        dict
            Created star object.
        """
        guild_id = config['guild_id']
        guild = message.guild

        check_nsfw(guild, config, message)

        # check if we already have a star or not
        star = await self.get_star(guild_id, message.id)

        if not star:
            star_object = empty_star_object(message)
            res = await self.starboard_coll.insert_one(star_object)

            if not res.acknowledged:
                raise StarAddError('Insert OP not acknowledged by db')

            star = star_object

        try:
            star['starrers'].index(author_id)
            raise StarAddError('Already starred')
        except ValueError:
            star['starrers'].append(author_id)
            star['starrers_count'] += 1

        await self.update_starobj(star)
        return star

    async def raw_remove_star(self, config: dict, message: discord.Message,
                              author_id: int) -> dict:
        """Remove a star from someone, updates the star object
        in the starboard collection.

        Returns
        -------
        dict
            Modified star object
        """
        guild_id = config['guild_id']
        star = await self.get_star(guild_id, message.id)
        if star is None:
            raise StarRemoveError('No message starred to be unstarred')

        try:
            star['starrers'].index(author_id)
            star['starrers'].remove(author_id)
            star['starrers_count'] -= 1
        except ValueError:
            raise StarRemoveError("Author didn't star the message.")

        if star['starrers_count'] < 1:
            res = await self.starboard_coll.delete_many({
                'message_id':
                message.id,
                'guild_id':
                guild_id
            })

            if res.deleted_count != 1:
                log.error(f'Deleted {res.deleted_count} document from 0 stars,'
                          ' different than 1')
            return star

        await self.starboard_coll.update_one({
            'message_id': message.id,
            'guild_id': guild_id
        }, {'$set': star})
        return star

    async def raw_remove_all(self, config: dict,
                             message: discord.Message) -> dict:
        """Remove all starrers from a message(deletes from the collection)."""
        guild_id = config['guild_id']
        star = await self.get_star(guild_id, message.id)
        if star is None:
            raise StarError('Star object not found to be reset')

        star['starrers'] = []
        star['starrers_count'] = 0
        await self.starboard_coll.delete_one({
            'message_id': message.id,
            'guild_id': guild_id
        })
        return star

    def debug_log(self, message: str, star: dict):
        """Send a debug log call with the star as a context."""
        channel_id = star.get('channel_id')
        guild_id = star.get('guild_id')
        chan = self.bot.get_channel(channel_id)

        log.debug(f'{message}\n'
                  f'{star["starrers_count"]} starrers right now\n'
                  f'message {star.get("message_id")}\n'
                  f'channel "{chan}" {channel_id}\n'
                  f'guild "{self.bot.get_guild(guild_id)}" {guild_id}')

    async def update_starobj(self, star: dict, **kwargs):
        """Given a star object, update it in the database."""
        if kwargs.get('log', True):
            self.debug_log('update star - '
                           f'{star["starrers_count"]} sc '
                           f'VS {len(star["starrers"])} ls', star)

        await self.starboard_coll.update_one(
            {
                # guild_id and message_id serve as the "primary key"
                # of this.
                'guild_id': star['guild_id'],
                'message_id': star['message_id']
            },
            {'$set': star})

    async def delete_starobj(self, star: dict, msg=None):
        """Delete a star object from the starboard collection.
        Removes the message from starboard if provided.
        """
        if msg:
            await msg.delete()

        self.debug_log('deleting star', star)

        return await self.starboard_coll.delete_one({
            'guild_id':
            star['guild_id'],
            'message_id':
            star['message_id']
        })

    async def starboard_send(self, starboard: discord.TextChannel, star: dict,
                             message: discord.Message) -> discord.Message:
        """Sends a message to the starboard."""
        title, embed = make_star_embed(star, message)
        return await starboard.send(title, embed=embed)

    async def update_star(self, config: dict, star: dict, **kwargs):
        """Update a star.

        Posts it to the starboard, edits if a message already exists.

        Parameters
        ----------
        config: dict
            Starboard configuration for the guild.
        star: dict
            Star object being updated.
        delete: bool, optional
            If this should delete the star.
        msg: discord.Message, optional
            A message object reffering to the star.

        Raises
        ------
        StarError
            For any error that happened while updating that star.
        """

        delete_mode = kwargs.get('delete', False)
        message = kwargs.get('msg')

        if message:
            assert star['message_id'] == message.id
            assert star['channel_id'] == message.channel.id

        guild_id = config['guild_id']
        guild = self.bot.get_guild(guild_id)
        if not guild:
            raise StarError('No guild found with the starboard configuration')

        starboard = guild.get_channel(config['starboard_id'])
        if not starboard:
            await self.delete_starconfig(config)
            raise StarError('No starboard channel found')

        try:
            star_message = await starboard.get_message(star['star_message_id'])
        except (KeyError, discord.errors.NotFound):
            star_message = None

        starcount = star['starrers_count']
        threshold = config.get('threshold', 1)
        below_threshold = starcount < threshold
        above_threshold = starcount >= threshold

        if delete_mode:
            return await self.delete_starobj(star, msg=star_message)

        if below_threshold and star_message is not None:
            await star_message.delete()

        # do update/send here
        # if it isnt above threshold, we shouldn't do anything
        if not above_threshold:
            return

        if star_message is None:
            star_message = await self.starboard_send(starboard, star, message)
            star['star_message_id'] = star_message.id
            await self.update_starobj(star, log=False)
        else:
            title, embed = make_star_embed(star, kwargs.get('msg'))
            await star_message.edit(content=title, embed=embed)

    async def add_star(self,
                       message: discord.Message,
                       author_id: int,
                       config: dict = None) -> dict:
        """Add a star to a message.

        Parameters
        ----------
        message: `discord.Message`
            Message to be starred.
        author_id: int
            Author ID of the star.

        Raises
        ------
        StarAddError
            If any kind of error happened while adding the star.
        """
        lock = self._locks[message.guild.id]
        await lock
        star = None

        try:
            if not config:
                config = await self._get_starconfig(message.guild.id)

            self.check_allow(config, message.channel.id)

            if hasattr(author_id, 'id'):
                author_id = author_id.id

            if author_id == message.author.id:
                raise StarAddError('No selfstarring allowed')

            star = await self.raw_add_star(config, message, author_id)
            star = await self.update_star(config, star, msg=message)
        finally:
            lock.release()

        return star

    async def remove_star(self,
                          message: discord.Message,
                          author_id: int,
                          config: dict = None) -> dict:
        """Remove a star from a message.

        Parameters
        ----------
        message: `discord.Message`
            Message.
        author_id: int
            ID of the person that is getting their star removed.

        Raises
        ------
        StarRemoveError
            Any kind of error while remoing the star.
        """
        lock = self._locks[message.guild.id]
        await lock
        star = None

        try:
            if not config:
                config = await self._get_starconfig(message.guild.id)

            self.check_allow(config, message.channel.id)

            if hasattr(author_id, 'id'):
                author_id = author_id.id

            if author_id == message.author.id:
                raise StarRemoveError('No selfstarring allowed')

            star = await self.raw_remove_star(config, message, author_id)
            star = await self.update_star(config, star, msg=message)
        finally:
            lock.release()

        return star

    async def remove_all(self, message: discord.Message, config: dict = None):
        """Remove all stars from a message.

        Parameters
        ----------
        message: `discord.Message`
            Message that is going to have all stars removed.
        """
        lock = self._locks[message.guild.id]
        await lock

        try:
            if not config:
                config = await self._get_starconfig(message.guild.id)

            star = await self.raw_remove_all(config, message)
            await self.update_star(config, star, delete=True)
        finally:
            lock.release()

    async def delete_starconfig(self, config: dict) -> bool:
        """Deletes a starboard configuration from the collection.

        Returns
        -------
        bool
            Success/Failure of the operation.
        """
        guild = self.bot.get_guild(config['guild_id'])
        log.debug('Deleting starconfig for %s[%d]', guild.name, guild.id)

        res = await self.starconfig_coll.delete_many(config)
        return res.deleted_count > 0

    def check_star(self, cfg: dict,
                   emoji_partial: discord.PartialEmoji) -> bool:
        """Check if the given partial reaction data
        match the starbaord configuration data for custom star emotes.
        """

        star_emoji = cfg.get('star_emoji', DEFAULT_STAR_EMOJI)
        is_star = False

        # check unicode
        if emoji_partial.name == star_emoji:
            is_star = True

        # check custom emotes (by id)
        elif emoji_partial.id == star_emoji:
            is_star = True

        return is_star

    def check_allow(self, cfg: dict, channel_id: int):
        """Check if the current channel is allowed to have
        messages starred from."""
        allowed_chans = cfg.get('allowed_chans', [])

        if not allowed_chans:
            return

        try:
            allowed_chans.index(channel_id)
        except ValueError:
            raise StarError('Channel not allowed to be starred')

    async def _sbhandle(self, message, sbctx, cfg):
        channel_id = sbctx['channel_id']
        user_id = sbctx['user_id']

        if channel_id == cfg['starboard_id'] and \
                user_id != self.bot.user.id:
            # This reaction is coming from the starboard.
            content = message.content
            log.debug(f'parsing things out from {content!r}')

            matches = ID_REGEX.findall(content)
            try:
                new_message_id = int(matches[-1])
                new_channel_id = int(matches[-2])
                return new_message_id, new_channel_id
            except (IndexError, ValueError):
                # no matches found, rip.
                log.warning(f'[sbhandle] failure parsing {content!r}')
                return None, None

        return None, None

    async def on_raw_reaction_add(self, payload):
        """Handle a reaction add."""
        emoji_partial = payload.emoji
        message_id = payload.message_id
        channel_id = payload.channel_id
        user_id = payload.user_id

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        if isinstance(channel, discord.DMChannel):
            return

        cfg = await self.get_starconfig(channel.guild.id)
        if not cfg:
            return

        is_star = self.check_star(cfg, emoji_partial)
        if not is_star:
            return

        # ignore blocked people
        if await self.bot.is_blocked_guild(channel.guild.id) or \
                await self.bot.is_blocked(user_id):
            return

        try:
            self.check_allow(cfg, channel_id)
            message = await channel.get_message(message_id)

            new_message_id, new_channel_id = await self._sbhandle(
                message, {
                    'channel_id': channel_id,
                    'user_id': user_id,
                }, cfg)

            if new_message_id and new_channel_id:
                payload = RawReactionActionEvent({
                    'message_id': new_message_id,
                    'channel_id': new_channel_id,
                    'user_id': user_id,
                }, emoji_partial)

                return await self.on_raw_reaction_add(payload)

            await self.add_star(message, user_id, cfg)
        except (StarError, StarAddError) as err:
            log.warning(f'raw_reaction_add: {err!r}')
        except Exception:
            log.exception('add_star @ reaction_add, %s[cid=%d] %s[gid=%d]',
                          channel.name, channel.id, channel.guild.name,
                          channel.guild.id)

    async def on_raw_reaction_remove(self, payload):
        emoji_partial = payload.emoji
        message_id = payload.message_id
        channel_id = payload.channel_id
        user_id = payload.user_id
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        if isinstance(channel, discord.DMChannel):
            return

        cfg = await self.get_starconfig(channel.guild.id)
        if not cfg:
            return

        is_star = self.check_star(cfg, emoji_partial)
        if not is_star:
            return

        # ignore blocked people
        if await self.bot.is_blocked_guild(channel.guild.id) or \
                await self.bot.is_blocked(user_id):
            return

        try:
            self.check_allow(cfg, channel_id)
            message = await channel.get_message(message_id)

            new_message_id, new_channel_id = await self._sbhandle(
                message, {
                    'channel_id': channel_id,
                    'user_id': user_id,
                }, cfg)

            if new_message_id and new_channel_id:
                payload = RawReactionActionEvent({
                    'message_id': new_message_id,
                    'channel_id': new_channel_id,
                    'user_id': user_id,
                }, emoji_partial)

                return await self.on_raw_reaction_remove(payload)

            await self.remove_star(message, user_id, cfg)
        except (StarError, StarRemoveError) as err:
            log.warning(f'raw_reaction_remove: {err!r}')
        except Exception:
            log.exception('reaction_remove, %s[cid=%d] %s[gid=%d]',
                          channel.name, channel.id, channel.guild.name,
                          channel.guild.id)

    async def on_raw_reaction_clear(self, payload):
        """Remove all stars in the message."""
        message_id = payload.message_id
        channel_id = payload.channel_id

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        if isinstance(channel, discord.DMChannel):
            return

        cfg = await self.get_starconfig(channel.guild.id)
        if not cfg:
            return

        # ignore blocked stuff
        if await self.bot.is_blocked_guild(channel.guild.id):
            return

        try:
            message = await channel.get_message(message_id)
            await self.remove_all(message, cfg)
        except (StarError, StarRemoveError) as err:
            log.warning(f'raw_reaction_clear: {err!r}')
        except Exception:
            log.exception('remove_all @ reaction_clear, %s[cid=%d] %s[gid=%d]',
                          channel.name, channel.id, channel.guild.name,
                          channel.guild.id)

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def starboard(self, ctx, channel_name: str):
        """Create a starboard channel.

        If the name specifies a NSFW channel,
        the starboard gets marked as NSFW.

        NSFW starboards allow messages from NSFW
        channels to be starred without any censoring.

        If your starboard gets marked as a SFW starboard,
        messages from NSFW channels get completly ignored.
        """

        guild = ctx.guild
        config = await self.get_starconfig(guild.id)
        if config is not None:
            await ctx.send("You already have a starboard. If you want"
                           " to detach josé from it, use the "
                           "`stardetach` command")
            return

        po = discord.PermissionOverwrite
        overwrites = {
            guild.default_role: po(read_messages=True, send_messages=False),
            guild.me: po(read_messages=True, send_messages=True),
        }

        try:
            starboard_chan = await guild.create_text_channel(
                channel_name,
                overwrites=overwrites,
                reason='Created starboard channel')

        except discord.Forbidden:
            return await ctx.send('No permissions to make a channel.')
        except discord.HTTPException as err:
            log.exception('Got HTTP error from starboard create')
            return await ctx.send(f'**SHIT!!!!**:  {err!r}')

        log.info(f'[starboard] Init starboard @ {guild.name}[{guild.id}]')

        # create config here
        config = empty_starconfig(guild)
        config['starboard_id'] = starboard_chan.id

        res = await self.starconfig_coll.insert_one(config)
        if not res.acknowledged:
            raise self.SayException('Failed to create '
                                    'starboard config (mongo: no ack)')

        await ctx.send('All done, I guess!')

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def starattach(self, ctx, starboard_chan: discord.TextChannel):
        """Attach an existing channel as a starboard.

        With this command you can create your starboard
        without needing José to automatically create the starboard for you
        """
        config = await self.get_starconfig(ctx.guild.id)
        if config:
            return await ctx.send('You already have a starboard config setup.')

        config = empty_starconfig(ctx.guild)
        config['starboard_id'] = starboard_chan.id
        res = await self.starconfig_coll.insert_one(config)

        if not res.acknowledged:
            raise self.SayException('Failed to create starboard '
                                    'config (no ack)')
            return

        await ctx.send('Done!')
        await ctx.ok()

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def stardetach(self, ctx, confirm: bool = False):
        """Detaches José from your starboard.

        Detaching means José will remove your starboard's configuration.
        And will stop detecting starred/unstarred posts, etc.

        Provide "y" as your confirmation.

        Manage Guild permission is required.
        """
        if not confirm:
            return await ctx.send('Operation not confirmed by user.')

        config = await self._get_starconfig(ctx.guild.id)
        await ctx.success(await self.delete_starconfig(config))

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def stardelete(self, ctx, confirm: bool = False):
        """Completly delete all starboard data from the guild.

        Follows the same logic as `j!stardetach`, but it
        deletes all starboard data, not just the configuration.
        """
        if confirm != 'y':
            return await ctx.send('not confirmed')

        config = await self._get_starconfig(ctx.guild.id)
        await self.delete_starconfig(config)

        self.loop.create_task(self.janitor_task(ctx.guild.id))
        await ctx.send('Data deletion scheduled.')

    @commands.command()
    @commands.guild_only()
    async def star(self, ctx, message_id: int):
        """Star a message."""
        try:
            message = await ctx.channel.get_message(message_id)
        except discord.NotFound:
            return await ctx.send('Message not found in the current channel')
        except discord.Forbidden:
            return await ctx.send("Can't retrieve message")
        except discord.HTTPException as err:
            return await ctx.send(f'Failed to retrieve message: {err!r}')

        try:
            await self.add_star(message, ctx.author)
            await ctx.ok()
        except (StarAddError, StarError) as err:
            log.warning(f'[star_command] Errored: {err!r}')
            return await ctx.send(f'Failed to add star: {err!r}')

    @commands.command()
    @commands.guild_only()
    async def unstar(self, ctx, message_id: int):
        """Unstar a message."""
        try:
            message = await ctx.channel.get_message(message_id)
        except discord.NotFound:
            return await ctx.send('Message not found in the current channel')
        except discord.Forbidden:
            return await ctx.send("Can't retrieve message")
        except discord.HTTPException as err:
            return await ctx.send(f'Failed to retrieve message: {err!r}')

        try:
            await self.remove_star(message, ctx.author)
            await ctx.ok()
        except (StarRemoveError, StarError) as err:
            log.warning(f'[unstar_cmd] Errored: {err!r}')
            return await ctx.send(f'Failed to remove star: {err!r}')

    @commands.command()
    @commands.guild_only()
    async def starrers(self, ctx, message_id: int):
        """Get the list of starrers from a message in the current channel."""
        guild = ctx.guild
        await self._get_starconfig(guild.id)
        star = await self.get_star(guild.id, message_id)
        if not star:
            return await ctx.send('Star object not found')

        channel = self.bot.get_channel(star['channel_id'])
        if not channel:
            return await ctx.send('Star found, Channel not found')

        try:
            message = await channel.get_message(message_id)
        except discord.NotFound:
            return await ctx.send('Message not found in the channel')
        except discord.Forbidden:
            return await ctx.send("Can't retrieve message")
        except discord.HTTPException as err:
            return await ctx.send(f'Failed to retrieve message: {err!r}')

        _, em = make_star_embed(star, message)
        starrers = [(guild.get_member(starrer_id), starrer_id)
                    for starrer_id in star['starrers']]

        def try_name(m, uid: int) -> str:
            """Try to get a name for a member."""
            if m is None:
                return f'Unfindable {uid}'

            return m.display_name

        starrer_list = (try_name(m[0], m[1]) for m in starrers)
        em.add_field(name='Starrers', value=', '.join(starrer_list))
        await ctx.send(embed=em)

    @commands.command()
    @commands.guild_only()
    async def starstats(self, ctx):
        """Get statistics about your starboard."""
        # This function is true hell.

        guild_query = {'guild_id': ctx.guild.id}
        await self._get_starconfig(ctx.guild.id)

        em = discord.Embed(
            title='Starboard statistics', colour=discord.Colour(0xFFFF00))

        total_messages = await self.starboard_coll.find(guild_query).count()
        em.add_field(name='Total messages starred', value=total_messages)

        starrers = collections.Counter()
        authors = collections.Counter()

        # calculate top 5
        top_stars = await self.starboard_coll.find(guild_query)\
            .sort('starrers_count', pymongo.DESCENDING).limit(5)\
            .to_list(length=None)

        # people who starred the most / received stars the most
        all_stars = self.starboard_coll.find(guild_query)
        async for star in all_stars:
            try:
                authors[star['author_id']] += 1
            except KeyError:
                pass

            for starrer_id in star['starrers']:
                starrers[starrer_id] += 1

        # process top 5
        res_sm = []
        for (idx, star) in enumerate(top_stars):
            if 'author_id' not in star:
                continue

            stctx = (f'<@{star["author_id"]}>, {star["message_id"]} '
                     f'@ <#{star["channel_id"]}> '
                     f'({star["starrers_count"]} stars)')

            res_sm.append(f'{idx + 1}\N{COMBINING ENCLOSING KEYCAP} {stctx}')

        em.add_field(
            name='Most starred messages',
            value='\n'.join(res_sm),
            inline=False)

        # process people who received stars the most
        mc_receivers = authors.most_common(5)
        res_sr = []

        for idx, data in enumerate(mc_receivers):
            user_id, received_stars = data

            # ALWAYS make sure the member is in the guild.
            member = ctx.guild.get_member(user_id)
            if not member:
                continue

            auctx = f'<@{user_id}> ({received_stars} stars)'
            res_sr.append(f'{idx + 1}\N{COMBINING ENCLOSING KEYCAP} {auctx}')

        em.add_field(
            name='Top 5 Star Receivers', value='\n'.join(res_sr), inline=False)

        # process people who *gave* stars the most
        mc_givers = starrers.most_common(5)
        res_gr = []

        for idx, data in enumerate(mc_givers):
            member_id, star_count = data
            member = ctx.guild.get_member(member_id)
            if not member:
                continue

            srctx = f'{member.mention} ({star_count} stars)'
            res_gr.append(f'{idx + 1}\N{COMBINING ENCLOSING KEYCAP} {srctx}')

        em.add_field(
            name=f'Top 5 Star Givers', value='\n'.join(res_gr), inline=False)

        await ctx.send(embed=em)

    @commands.command(aliases=['rs'])
    @commands.guild_only()
    async def randomstar(self, ctx):
        """Get a random star from your starboard."""
        guild = ctx.guild
        await self._get_starconfig(ctx.guild.id)
        all_stars = await self.starboard_coll.find({
            'guild_id': guild.id
        }).count()
        random_idx = random.randint(0, all_stars)

        guild_stars_cur = self.starboard_coll.find({
            'guild_id': guild.id
        }).limit(1).skip(random_idx)

        # ugly, I know.
        star = None
        async for star in guild_stars_cur:
            star = star

        if star is None:
            return await ctx.send('No star object found')

        channel = self.bot.get_channel(star['channel_id'])
        if channel is None:
            return await ctx.send('Star references a non-findable channel.')

        message_id = star['message_id']
        try:
            message = await channel.get_message(message_id)
        except discord.NotFound:
            raise self.SayException('Message not found')
        except discord.Forbidden:
            raise self.SayException("Can't retrieve message")
        except discord.HTTPException as err:
            raise self.SayException(f'Failed to retrieve message: {err!r}')

        current = ctx.channel.is_nsfw()
        schan = channel.is_nsfw()
        if not current and schan:
            raise self.SayException(f'channel nsfw={current}, '
                                    f'nsfw={schan}, nope')

        title, embed = make_star_embed(star, message)
        await ctx.send(title, embed=embed)

    @commands.command()
    @commands.guild_only()
    async def streload(self, ctx, message_id: int):
        """Star reload.

        Reload a message, its starrers and update the star in the starboard.
        Useful if the starred message was edited.
        """
        channel = ctx.channel
        cfg = await self._get_starconfig(channel.guild.id)

        try:
            message = await channel.get_message(message_id)
        except discord.NotFound:
            raise self.SayException('Message not found in the current channel')

        star = await self.get_star(ctx.guild.id, message_id)
        if not star:
            raise self.SayException('Star object not found')

        try:
            await self.update_star(cfg, star, msg=message)
        except StarError as err:
            log.error(f'force_reload: {err!r}')
            raise self.SayException(f'rip {err!r}')

        await ctx.ok()

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def sbsetc(self, ctx, emoji: str = None):
        """Set a custom emote (or unicode emoji) as your starboard emote.

        This does not check against bad values (like "a")
        which are un-reactable. Use with caution.

        Only people with the "Manage Server" permission
        can use this command.
        """
        config = await self._get_starconfig(ctx.guild.id)

        if not emoji:
            emoji = config.get('star_emoji', DEFAULT_STAR_EMOJI)
            try:
                emoji = self.bot.get_emoji(int(emoji))
            except ValueError:
                pass

            return await ctx.send(f'The starboard emote is {str(emoji)}')

        match = EMOJI_REGEX.match(emoji)

        custom = bool(match)
        emoji_res = ''

        if not custom:
            emoji_res = emoji
        else:
            try:
                emoji_res = int(match.group(1))
            except ValueError:
                raise self.SayException(':x: Custom Emote ID is not a number')

        await self.starconfig_coll.update_one({
            'guild_id': ctx.guild.id
        }, {'$set': {
            'star_emoji': emoji_res
        }})

        await ctx.ok()

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def sbtoggle(self, ctx, channel: discord.TextChannel):
        """Toggle a channel's allowance on starboard.

        By default, all channels are allowed to have
        their messages starred.

        As soon as you filter it to be at least 1 channel,
        all the others will become blocked by default.
        """
        config = await self._get_starconfig(ctx.guild.id)

        allowed_chans = config.get('allowed_chans', [])

        try:
            allowed_chans.remove(channel.id)
            await ctx.send(f'<#{channel.id}> is **disallowed** to be starred')
        except ValueError:
            allowed_chans.append(channel.id)
            await ctx.send(f'<#{channel.id}> is **allowed** to be starred')

        if not allowed_chans:
            await ctx.send('All channels are available to be starred')

        await self.starconfig_coll.update_one({
            'guild_id': ctx.guild.id
        }, {'$set': {
            'allowed_chans': allowed_chans,
        }})

        await ctx.ok()

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def sbthreshold(self, ctx, stars: int):
        """Set a threshold for stars to enter starboard.
        """
        await self._get_starconfig(ctx.guild.id)

        if stars < 1:
            raise self.SayException('Invalid threshold.')

        await self.starconfig_coll.update_one({
            'guild_id': ctx.guild.id
        }, {
            '$set': {
                'threshold': stars,
            }
        })

        await ctx.ok()

    @commands.command(aliases=['gss'])
    @commands.cooldown(1, 10)
    async def gstarstats(self, ctx):
        """Global Starboard statistics.

        This has a global cooldown of 1/10s.
        """
        em = discord.Embed(
            title='Global Starboard Stats', color=discord.Color(0xFFFF00))

        # calculate global top 5
        top_stars = await self.starboard_coll.find({}) \
            .sort('starrers_count', pymongo.DESCENDING).limit(5) \
            .to_list(length=None)

        res_gs = []

        for idx, star in enumerate(top_stars):
            if 'author_id' not in star:
                continue

            guild = self.bot.get_guild(star['guild_id'])
            if not guild:
                continue

            sctx = (f'`{guild}` [{guild.id}]\n'
                    f', channel <#{star["channel_id"]}>'
                    f', message {star["message_id"]}'
                    f', author <@{star["author_id"]}>')

            res_gs.append(f'{idx + 1}\N{COMBINING ENCLOSING KEYCAP} {sctx}')

        em.add_field(
            name='Top messages', value='\n'.join(res_gs), inline=False)

        # TODO: top star givers / receivers

        await ctx.send(embed=em)


def setup(bot):
    bot.add_jose_cog(Starboard)
