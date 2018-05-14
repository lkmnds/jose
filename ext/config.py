import pprint
import collections
import time
import logging

import asyncpg
import discord
import motor.motor_asyncio
from discord.ext import commands

from .common import Cog

log = logging.getLogger(__name__)


def _is_moderator(ctx):
    if not ctx.guild:
        return False

    member = ctx.guild.get_member(ctx.author.id)
    perms = ctx.channel.permissions_for(member)
    return (ctx.author.id == ctx.bot.owner_id) or perms.manage_guild


def is_moderator():
    return commands.check(_is_moderator)


class Config(Cog):
    """Guild-specific configuration commands."""

    def __init__(self, bot):
        super().__init__(bot)

        addr = getattr(bot.config, 'MONGO_LOC', None)
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(addr)
        self.bot.mongo = self.mongo_client

        self.jose_db = self.mongo_client['jose']

        self.config_coll = self.jose_db['config']
        self.block_coll = self.jose_db['block']
        self.bot.block_coll = self.block_coll

        # querying the db every time is not worth it
        self.config_cache = collections.defaultdict(dict)

        # used to check if cache has all defined objects in it
        self.default_keys = None

        # asyncpg connection pool
        self.db = None
        self.loop.create_task(self.pg_init())

    async def pg_init(self):
        self.db = await asyncpg.create_pool(**self.bot.config.postgres)

    def cfg_default(self, guild: int) -> dict:
        """Default configuration object for a guild"""
        if isinstance(guild, discord.Guild):
            guild = guild.id
        return {
            'guild_id': guild,
            'botblock': True,
            'speak_channel': None,
            'prefix': self.bot.config.prefix,

            # autoreply stuff from Speak cog
            'autoreply_prob': 0,
            'autoreply_disable': [],
            'fullwidth_prob': 0.1,
        }

    async def block_one(self, user_id, k='user_id', reason=None):
        """Block one thing from using jose."""
        if await self.block_coll.find_one({k: user_id}) is not None:
            return False

        try:
            await self.block_coll.insert_one({k: user_id, 'reason': reason})
            self.bot.block_cache[user_id] = True
            return True
        except:
            return False

    async def unblock_one(self, user_id, k='user_id', reason=''):
        """Unblock one thing from jose."""
        del_res = await self.block_coll.delete_one({k: user_id})
        self.bot.block_cache[user_id] = False

        return del_res.deleted_count > 0

    async def ensure_cfg(self, guild, query=False) -> dict:
        """Get a configuration object for a guild.
        If `query` is `False`, it checks for a configuration object in cache

        Parameters
        ----------
        guild: discord.Guild
            The guild to find a configuration object to.
        query: bool
            If this will check cache first or just query Mongo
            for a configuration object.
        """
        cached = self.config_cache[guild.id]
        default = self.cfg_default(guild)

        if not self.default_keys:
            self.default_keys = list(default.keys())

        if not query:
            # check if the cached object satisfies all configuration keys
            #  a default configuration would have
            satisfies = all([(field in cached) for field in self.default_keys])
            if satisfies:
                return cached

        cfg = await self.config_coll.find_one({'guild_id': guild.id})
        if not cfg:
            # Create a config for the guild, since it doesn't
            # exist.
            await self.config_coll.insert_one(default)

            # Recall ensure_cfg for caching
            return await self.ensure_cfg(guild)

        # We have a proper config object, cache it.
        self.config_cache[guild.id] = cfg
        return cfg

    async def cfg_get(self,
                      guild: discord.Guild,
                      key: str,
                      default: 'any' = None) -> 'any':
        """Get a configuration key for a guild."""
        if key in self.config_cache[guild.id]:
            return self.config_cache[guild.id][key]

        cfg = await self.ensure_cfg(guild)

        try:
            return cfg[key]
        except KeyError:
            cfg[key] = default
            return default

    async def cfg_set(self, guild, key: str, value: 'any') -> bool:
        """Set a configuration key."""
        await self.ensure_cfg(guild)
        res = await self.config_coll.update_one({
            'guild_id': guild.id
        }, {'$set': {
            key: value
        }})

        log.debug('[cfg:set] %s[gid=%d] k=%r <- v=%r', guild, guild.id, key,
                  value)

        self.config_cache[guild.id][key] = value
        return res.modified_count > 0

    @commands.command(name='cfg_get')
    @commands.guild_only()
    async def _config_get(self, ctx, key: str):
        """Get a configuration key"""
        res = await self.cfg_get(ctx.guild, key)
        await ctx.send(f'{res!r}')

    @commands.command(name='cfgall', hidden=True)
    @commands.guild_only()
    async def cfgall(self, ctx):
        """Get the configuration object for a guild."""
        t1 = time.monotonic()
        cfg = await self.ensure_cfg(ctx.guild)
        t2 = time.monotonic()
        delta = round((t2 - t1) * 1000, 2)
        await ctx.send(f'```py\n{pprint.pformat(cfg)}\nTook {delta}ms.\n```')

    @commands.command(aliases=['speakchan'])
    @commands.guild_only()
    @is_moderator()
    async def speakchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel José will gather messages to feed
        to his markov generator.

        By default, it will choose the same channel
        the command is invoked from.
        """
        old_id = await self.cfg_get(ctx.guild, 'speak_channel')
        success = await self.cfg_set(ctx.guild, 'speak_channel', channel.id)

        # invalidate texter
        speak = self.bot.get_cog('Speak')
        old = self.bot.get_channel(old_id)
        if speak and old:
            try:
                log.debug(f'invalidating texter on {ctx.guild} '
                          f'{ctx.guild.id}, {old} {old.id} '
                          f'=> {channel} {channel.id}')

                speak.text_generators.pop(ctx.guild.id)
            except KeyError:
                pass

        await ctx.success(success)

    @commands.command()
    @commands.guild_only()
    @is_moderator()
    async def jsprob(self, ctx, prob: float):
        """Set the probability per message that José will autoreply to it."""
        if prob < 0 or prob > 5:
            await ctx.send("`prob` is out of the range `[0-5]`")
            return

        success = await self.cfg_set(ctx.guild, 'autoreply_prob', prob / 100)
        await ctx.success(success)

    @commands.command()
    @commands.guild_only()
    @is_moderator()
    async def fwprob(self, ctx, prob: float):
        """Set the probability that josé will randomly respond in fullwidth."""
        if prob < 0 or prob > 10:
            await ctx.send('`prob` is out of the range `[0-10]`')
            return

        success = await self.cfg_set(ctx.guild, 'fullwidth_prob', prob / 100)
        await ctx.success(success)

    @commands.command()
    @commands.guild_only()
    @is_moderator()
    async def botblock(self, ctx):
        """Toggle bot blocking."""
        botblock = await self.cfg_get(ctx.guild, 'botblock')
        success = await self.cfg_set(ctx.guild, 'botblock', not botblock)
        await ctx.success(success)
        await ctx.send(f'`botblock` set to `{not botblock}`')

    @commands.command()
    @commands.is_owner()
    async def block(self, ctx, user: discord.User, *, reason: str = ''):
        """Block someone from using the bot, globally"""
        basic = self.bot.get_cog('Basic')
        try:
            await user.send('**You have been blocked from using José.**\n'
                            f'reason: `{reason}`\n'
                            'If you want to appeal this block, '
                            f'drop by the support guild: {basic.support_inv}')
        except discord.Forbidden:
            await ctx.send('Failed to DM user')

        await ctx.success(await self.block_one(user.id, 'user_id', reason))

    @commands.command()
    @commands.is_owner()
    async def unblock(self, ctx, user: discord.User, *, reason: str = ''):
        """Unblock someone from using the bot, globally"""
        await ctx.success(await self.unblock_one(user.id, 'user_id', reason))

    @commands.command()
    @commands.is_owner()
    async def blockguild(self, ctx, guild_id: int, *, reason: str = ''):
        """Block an entire guild from using José."""
        basic = self.bot.get_cog('Basic')
        botcoll = self.bot.get_cog('BotCollection')

        guild = self.bot.get_guild(guild_id)

        try:
            await botcoll.fallback(guild, '**This guild has been blocked'
                                   ' from using José.**\n'
                                   f'reason: `{reason}`\n'
                                   'If you want to appeal this block, '
                                   'drop by the support guild: '
                                   f'{basic.support_inv}')
        except discord.Forbidden:
            await ctx.send('Failed to message guild')

        await ctx.success(await self.block_one(guild_id, 'guild_id', reason))
        await guild.leave()

    @commands.command()
    @commands.is_owner()
    async def unblockguild(self, ctx, guild_id: int, *, reason: str = ''):
        """Unblock a guild from using José."""
        await ctx.success(await self.unblock_one(guild_id, 'guild_id', reason))

    @commands.command()
    async def blockreason(self, ctx, anything_id: int):
        """Get a reason for a block if it exists"""
        userblock = await self.block_coll.find_one({'user_id': anything_id})
        if userblock is not None:
            e = discord.Embed(title='User blocked', color=discord.Color.red())
            e.description = f'<@{anything_id}> - `{userblock.get("reason")}`'
            return await ctx.send(embed=e)

        guildblock = await self.block_coll.find_one({'guild_id': anything_id})
        if guildblock is not None:
            e = discord.Embed(title='Guild blocked', color=discord.Color.red())
            e.description = f'why? `{userblock.get("reason")}`'
            return await ctx.send(embed=e)

        await ctx.send('Block not found')

    @commands.command()
    @commands.guild_only()
    async def prefix(self, ctx, prefix: str = None):
        """Sets a guild prefix. Returns the prefix if no args are passed."""
        if not prefix:
            prefix = await self.cfg_get(ctx.guild, 'prefix')
            return await ctx.send(f'The prefix is `{prefix}`')

        if not _is_moderator(ctx):
            return await ctx.send('Unauthorized to set prefix.')

        if not (1 < len(prefix) < 20):
            return await ctx.send('Prefixes need to be 1-20 characters long')

        return await ctx.success(await self.cfg_set(ctx.guild, 'prefix',
                                                    prefix))

    @commands.command()
    @commands.guild_only()
    @is_moderator()
    async def notify(self, ctx, channel: discord.TextChannel = None):
        """Make a channel a notification channel.

        A notification channel will be used bu josé
        to say when your server/guild is successfully
        stolen from by another guild.

        José NEEDS TO HAVE "Send Message" permissions upfront
        for this to work.
        """
        channel = channel or ctx.channel
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages:
            return await ctx.send('Add `Send Messages` '
                                  'permission to josé please')

        return await ctx.success(await self.cfg_set(
            ctx.guild, 'notify_channel', channel.id))

    @commands.command()
    @commands.guild_only()
    @is_moderator()
    async def artoggle(self, ctx, channel: discord.TextChannel):
        """Toggle autoreply off/on in a channel.

        By default, José autoreplies in all channels, if there are
        some channels you don't want josé to autoreply on, use this command.

        Use this command again to toggle it back on.
        """
        channels = await self.config.cfg_get(ctx.guild, 'autoreply_disable',
                                             [])

        chan = channel.id
        if chan in channels:
            channels.remove(chan)
        else:
            channels.append(chan)

        ok = await self.config.cfg_set(ctx.guild, 'autoreply_disable',
                                       channels)
        await ctx.success(ok)


    @commands.command()
    @commands.guild_only()
    async def arlist(self, ctx):
        """Show the channels that josé can or can not autoreply to."""
        embed = discord.Embed(title='Autoreply is disabled in')
        channels = await self.config.cfg_get(ctx.guild, 'autoreply_disable', [])

        if channels:
            embed.description = ' '.join(f'<#{channel}>' for channel in channels)
        else:
            embed.description = '<no channels>'

        await ctx.send(embed=embed)


    @commands.command()
    @commands.is_owner()
    async def dbstats(self, ctx):
        """Show some mongoDB stuff because JSON sucks ass."""
        colls = await self.jose_db.collection_names()
        counts = collections.Counter()

        for coll_name in colls:
            coll = self.jose_db[coll_name]
            counts[coll_name] = await coll.count()

        coll_counts = '\n'.join([
            f'{collname:20} | {count}'
            for collname, count in counts.most_common()
        ])
        coll_counts = f'```\n{coll_counts}```'
        await ctx.send(coll_counts)


def setup(bot):
    bot.add_cog(Config(bot))
