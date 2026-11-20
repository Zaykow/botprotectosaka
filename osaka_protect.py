# pip install -U discord.py
# export DISCORD_TOKEN=ton_token   (Windows: set DISCORD_TOKEN=...)
import asyncio
import json
import os
import random
import re
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discord
from discord.ext import commands

PREFIX = "!"
BOT_NAME = "Osaka PROTECT"
LOG_CHANNEL = "osaka-logs"  # salon texte à créer sur ton serveur, sinon les logs vont en console
REQUEST_CHANNEL = "only-admins"  # salon où les demandes d'ajout de bot sont postées
# ---------- sauvegarde (osaka_data.json, à côté du script) ----------
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osaka_data.json")


def load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"allowed_bots": [], "infractions": {}}


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DATA, f, ensure_ascii=False, indent=2)


DATA = load_data()
ALLOWED_BOTS: set[int] = set(DATA["allowed_bots"])


def record_infraction(guild_id: int, user_id: int, reason: str):
    DATA["infractions"].setdefault(str(guild_id), {}).setdefault(str(user_id), []).append(
        {"ts": int(time.time()), "reason": reason}
    )
    save_data()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

DURATION_RE = re.compile(r"^(\d+)([smhd])$")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_TIMEOUT = timedelta(days=28)  # limite Discord

# ---------- anti-spam ----------
ANTISPAM = {"enabled": True}
FLOOD_MSGS = 6
FLOOD_WINDOW = 5
DUP_COUNT = 3
DUP_WINDOW = 30
MENTION_LIMIT = 5
INVITE_RE = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/\S+", re.I)
_history: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=50))

# ---------- filtre de mots ----------
BANNED_WORDS: set[str] = set()  # ex: {"mot1", "mot2"}
WORD_RE = re.compile(r"\b(" + "|".join(map(re.escape, BANNED_WORDS)) + r")\b", re.I) if BANNED_WORDS else None

# ---------- anti-raid / comptes récents ----------
MIN_ACCOUNT_DAYS = 7        # 0 = désactivé
RAID_JOINS = 5
RAID_WINDOW = 10
LOCKDOWN_SECONDS = 300
_joins: deque = deque(maxlen=50)
LOCKDOWN = {"until": 0.0}


def parse_duration(text: str) -> timedelta | None:
    m = DURATION_RE.match(text.lower())
    if not m:
        return None
    return timedelta(seconds=int(m.group(1)) * UNITS[m.group(2)])


def can_act_on(ctx: commands.Context, target: discord.Member) -> str | None:
    """Retourne un message d'erreur si l'action est interdite, sinon None."""
    if target == ctx.author:
        return "tu ne peux pas te cibler toi-même"
    if target == ctx.guild.owner:
        return "impossible sur le propriétaire du serveur"
    if target.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return "ce membre a un rôle égal ou supérieur au tien"
    if target.top_role >= ctx.guild.me.top_role:
        return "ce membre est au-dessus du rôle de Osaka PROTECT"
    return None


def is_exempt(message: discord.Message) -> bool:
    perms = message.author.guild_permissions
    return message.author.bot or perms.administrator or perms.manage_messages


async def log(guild: discord.Guild, text: str):
    print(f"[{guild.name}] {text}")
    ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL)
    if ch:
        try:
            await ch.send(text)
        except discord.HTTPException:
            pass


async def purge_user(channel: discord.TextChannel, user: discord.Member, seconds: int) -> int:
    """Supprime les messages récents d'un utilisateur dans le salon."""
    cutoff = discord.utils.utcnow() - timedelta(seconds=seconds)
    try:
        deleted = await channel.purge(limit=100, check=lambda m: m.author.id == user.id and m.created_at > cutoff)
        return len(deleted)
    except discord.HTTPException:
        return 0


async def punish(message: discord.Message, reason: str, minutes: int = 0, purge_seconds: int = 0):
    """Supprime le message (et les messages spam récents si purge_seconds > 0), mute si demandé, avertit."""
    deleted_count = 0
    if purge_seconds:
        deleted_count = await purge_user(message.channel, message.author, purge_seconds)
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    if minutes > 0:
        try:
            await message.author.timeout(timedelta(minutes=minutes), reason=f"auto-mod: {reason}")
        except discord.HTTPException:
            pass
    extra = f" ({deleted_count} message(s) spam supprimé(s))" if purge_seconds else ""
    await message.channel.send(f"🛡️ {message.author.mention} : {reason}{extra}", delete_after=10)
    await log(message.guild, f"🛡️ {message.author} ({reason}){extra} dans #{message.channel.name}")
    record_infraction(message.guild.id, message.author.id, reason)


async def check_spam(message: discord.Message) -> bool:
    if message.mention_everyone:
        await punish(message, "mention @everyone/@here interdite", minutes=30)
        return True
    if INVITE_RE.search(message.content):
        await punish(message, "lien d'invitation interdit")
        return True
    if len({u.id for u in message.mentions}) >= MENTION_LIMIT:
        await punish(message, "trop de mentions", minutes=30, purge_seconds=60)
        return True

    now = time.monotonic()
    q = _history[(message.guild.id, message.author.id)]
    q.append((now, message.content))
    while q and now - q[0][0] > DUP_WINDOW:
        q.popleft()

    if sum(1 for t, _ in q if now - t <= FLOOD_WINDOW) >= FLOOD_MSGS:
        q.clear()
        await punish(message, "flood détecté", minutes=10, purge_seconds=60)
        return True
    if sum(1 for _, c in q if c and c == message.content) >= DUP_COUNT:
        q.clear()
        await punish(message, "message répété", minutes=10, purge_seconds=60)
        return True
    return False


async def check_words(message: discord.Message) -> bool:
    if WORD_RE and WORD_RE.search(message.content):
        await punish(message, "langage interdit")
        return True
    return False


@bot.event
async def on_ready():
    bot.add_view(TicketView())  # boutons de ticket actifs après redémarrage
    print(f"{BOT_NAME} connecté en tant que {bot.user}")


async def handle_bot_join(member: discord.Member):
    """Retire un bot non autorisé et demande l'accord des admins dans #only-admins."""
    guild = member.guild
    try:
        await member.kick(reason="bot non autorisé (en attente d'accord admin)")
    except discord.HTTPException:
        pass

    ch = discord.utils.get(guild.text_channels, name=REQUEST_CHANNEL)
    admins = [m for m in guild.members if not m.bot and m.guild_permissions.administrator]
    pings = " ".join(a.mention for a in admins[:20])
    embed = discord.Embed(
        title="🤖 demande d'ajout de bot",
        description=(
            f"**{member}** (`{member.id}`) a été ajouté puis retiré automatiquement.\n"
            f"pour l'autoriser, tape `{PREFIX}allowbot {member.id}` ici, puis réinvite-le."
        ),
        color=0xF1C40F,
    )
    if ch:
        try:
            await ch.send(content=pings or None, embed=embed)
        except discord.HTTPException:
            pass
    await log(guild, f"🤖 bot {member} ({member.id}) retiré, demande envoyée dans #{REQUEST_CHANNEL}")


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        if member.id not in ALLOWED_BOTS and member.id != bot.user.id:
            await handle_bot_join(member)
        return

    now = time.monotonic()
    _joins.append(now)
    recent = sum(1 for t in _joins if now - t <= RAID_WINDOW)
    if recent >= RAID_JOINS and now > LOCKDOWN["until"]:
        LOCKDOWN["until"] = now + LOCKDOWN_SECONDS
        await log(member.guild, f"🚨 raid détecté ({recent} arrivées en {RAID_WINDOW}s), lockdown {LOCKDOWN_SECONDS // 60} min")

    if now <= LOCKDOWN["until"]:
        try:
            await member.kick(reason="lockdown anti-raid")
            await log(member.guild, f"🔒 {member} kické (lockdown)")
        except discord.HTTPException:
            pass
        return

    if MIN_ACCOUNT_DAYS and (discord.utils.utcnow() - member.created_at).days < MIN_ACCOUNT_DAYS:
        try:
            await member.kick(reason=f"compte de moins de {MIN_ACCOUNT_DAYS} jours")
            await log(member.guild, f"🆕 {member} kické (compte trop récent)")
        except discord.HTTPException:
            pass


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return await bot.process_commands(message)
    if not is_exempt(message):
        if ANTISPAM["enabled"] and await check_spam(message):
            return
        if await check_words(message):
            return
    await bot.process_commands(message)


@bot.command(name="antispam")
@commands.has_permissions(manage_guild=True)
async def antispam(ctx: commands.Context, state: str | None = None):
    """!antispam on / off"""
    if state is None:
        return await ctx.send(f"anti-spam {'activé' if ANTISPAM['enabled'] else 'désactivé'}")
    if state.lower() in ("on", "off"):
        ANTISPAM["enabled"] = state.lower() == "on"
        return await ctx.send(f"anti-spam {'activé' if ANTISPAM['enabled'] else 'désactivé'}")
    await ctx.send("usage: `!antispam on` ou `!antispam off`")


@bot.command(name="lockdown")
@commands.has_permissions(manage_guild=True)
async def lockdown(ctx: commands.Context, state: str | None = None):
    """!lockdown on / off — kick tous les nouveaux arrivants"""
    if state is None:
        active = time.monotonic() <= LOCKDOWN["until"]
        return await ctx.send(f"lockdown {'actif' if active else 'inactif'}")
    if state.lower() == "on":
        LOCKDOWN["until"] = time.monotonic() + LOCKDOWN_SECONDS
        await ctx.send(f"🔒 lockdown activé pour {LOCKDOWN_SECONDS // 60} min")
        await log(ctx.guild, f"🔒 lockdown activé par {ctx.author}")
    elif state.lower() == "off":
        LOCKDOWN["until"] = 0.0
        await ctx.send("🔓 lockdown désactivé")
        await log(ctx.guild, f"🔓 lockdown désactivé par {ctx.author}")
    else:
        await ctx.send("usage: `!lockdown on` ou `!lockdown off`")


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, amount: int):
    """!purge 20 — supprime les N derniers messages (max 100)"""
    amount = max(1, min(amount, 100))
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🧹 {len(deleted) - 1} message(s) supprimé(s)", delete_after=5)


@bot.command(name="allowbot")
@commands.has_permissions(administrator=True)
async def allowbot(ctx: commands.Context, bot_id: int):
    """!allowbot <id> — autorise un bot à rejoindre le serveur"""
    ALLOWED_BOTS.add(bot_id)
    DATA["allowed_bots"] = sorted(ALLOWED_BOTS)
    save_data()
    await ctx.send(f"✅ bot `{bot_id}` autorisé. tu peux le réinviter maintenant.")
    await log(ctx.guild, f"✅ bot {bot_id} autorisé par {ctx.author}")


@bot.command(name="mode")
async def mode(ctx: commands.Context):
    """!mode — affiche toutes les commandes"""
    embed = discord.Embed(title=f"{BOT_NAME} — commandes", color=0x5865F2)
    embed.add_field(
        name="🛡️ Modération",
        value=(
            f"`{PREFIX}kick @user raison` expulser\n"
            f"`{PREFIX}ban @user|<id> raison` bannir (même hors serveur)\n"
            f"`{PREFIX}tempmute @user 10m raison` mute (s/m/h/d, max 28j)\n"
            f"`{PREFIX}unmute @user` enlever le mute\n"
            f"`{PREFIX}unban <id> [raison]` débannir par ID"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 Salons",
        value=(
            f"`{PREFIX}lock [#salon]` verrouiller\n"
            f"`{PREFIX}unlock [#salon]` déverrouiller\n"
            f"`{PREFIX}purge N` supprimer N messages (max 100)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🚨 Protection",
        value=(
            f"`{PREFIX}antispam on/off` anti-spam (sans argument : état)\n"
            f"`{PREFIX}lockdown on/off` anti-raid manuel (sans argument : état)\n"
            f"`{PREFIX}allowbot <id>` autoriser un bot (admin)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔊 Infos",
        value=f"`{PREFIX}vc` statistiques du serveur\n`{PREFIX}vocal` membres en vocal\n`{PREFIX}gw 10m #salon cadeau` lancer un giveaway\n`{PREFIX}say texte` le bot écrit à ta place\n`{PREFIX}poll Q | a | b` sondage\n`{PREFIX}ticketpanel` panneau de tickets\n`{PREFIX}close` fermer un ticket\n`{PREFIX}infractions @user|<id>` historique\n`{PREFIX}rolepanel` panneau de rôles 🔵 moid / 🌸 foid\n`{PREFIX}mode` cette liste",
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="lock")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def lock(ctx: commands.Context, channel: discord.TextChannel | None = None):
    """!lock [#salon] — bloque l'envoi de messages pour @everyone"""
    channel = channel or ctx.channel
    try:
        await channel.set_permissions(ctx.guild.default_role, send_messages=False, reason=f"lock par {ctx.author}")
    except discord.Forbidden:
        return await ctx.send("❌ je n'ai pas la permission de modifier ce salon (vérifie le rôle du bot et sa permission Gérer les salons)")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ échec du lock: {e}")
    embed = discord.Embed(title="🔒 Salon verrouillé", description=f"verrouillé par {ctx.author.mention}", color=0xE74C3C)
    await channel.send(embed=embed)
    if channel != ctx.channel:
        await ctx.send(f"🔒 {channel.mention} verrouillé")
    await log(ctx.guild, f"🔒 #{channel.name} verrouillé par {ctx.author}")


@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context, channel: discord.TextChannel | None = None):
    """!unlock [#salon] — rétablit l'envoi de messages pour @everyone"""
    channel = channel or ctx.channel
    try:
        await channel.set_permissions(ctx.guild.default_role, send_messages=None, reason=f"unlock par {ctx.author}")
    except discord.Forbidden:
        return await ctx.send("❌ je n'ai pas la permission de modifier ce salon (vérifie le rôle du bot et sa permission Gérer les salons)")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ échec du unlock: {e}")
    embed = discord.Embed(title="🔓 Salon déverrouillé", description=f"déverrouillé par {ctx.author.mention}", color=0x2ECC71)
    await channel.send(embed=embed)
    if channel != ctx.channel:
        await ctx.send(f"🔓 {channel.mention} déverrouillé")
    await log(ctx.guild, f"🔓 #{channel.name} déverrouillé par {ctx.author}")


@bot.command(name="vc")
async def vc_stats(ctx: commands.Context):
    """Statistiques du serveur."""
    guild = await bot.fetch_guild(ctx.guild.id, with_counts=True)
    voice_total = sum(len(vc.members) for vc in ctx.guild.voice_channels)
    embed = discord.Embed(title=f"Statistiques {ctx.guild.name}", color=0xE75480)
    embed.add_field(name="👥 Membres", value=str(ctx.guild.member_count), inline=True)
    embed.add_field(name="🟢 En ligne", value=str(guild.approximate_presence_count or 0), inline=True)
    embed.add_field(name="🔊 En vocal", value=str(voice_total), inline=True)
    embed.add_field(name="💎 Boosts", value=str(ctx.guild.premium_subscription_count or 0), inline=True)
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(text=BOT_NAME)
    await ctx.send(embed=embed)


@bot.command(name="vocal")
async def voice_list(ctx: commands.Context):
    """Liste les membres connectés en vocal."""
    embed = discord.Embed(title=f"{BOT_NAME} — vocaux", color=0x2B2D31)
    found = False
    for vc in ctx.guild.voice_channels:
        if vc.members:
            found = True
            names = "\n".join(f"• {m.display_name}" for m in vc.members)
            embed.add_field(name=f"{vc.name} ({len(vc.members)})", value=names, inline=False)
    if not found:
        embed.description = "personne en vocal"
    await ctx.send(embed=embed)


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "aucune raison"):
    err = can_act_on(ctx, member)
    if err:
        return await ctx.send(f"❌ {err}")
    await member.kick(reason=f"{ctx.author}: {reason}")
    await ctx.send(f"👢 {member} kické. raison: {reason}")
    await log(ctx.guild, f"👢 {member} kické par {ctx.author} ({reason})")
    record_infraction(ctx.guild.id, member.id, f"kick: {reason}")


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx: commands.Context, target: discord.User, *, reason: str = "aucune raison"):
    """!ban @user ou !ban <id> [raison]"""
    member = ctx.guild.get_member(target.id)
    if member:
        err = can_act_on(ctx, member)
        if err:
            return await ctx.send(f"❌ {err}")
    try:
        await ctx.guild.ban(discord.Object(id=target.id), reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
    except discord.HTTPException as e:
        return await ctx.send(f"❌ échec du ban: {e}")
    await ctx.send(f"🔨 {target} banni. raison: {reason}")
    await log(ctx.guild, f"🔨 {target} ({target.id}) banni par {ctx.author} ({reason})")
    record_infraction(ctx.guild.id, target.id, f"ban: {reason}")


@bot.command(name="tempmute")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def tempmute(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "aucune raison"):
    """Ex: !tempmute @user 10m spam  (s/m/h/d)"""
    err = can_act_on(ctx, member)
    if err:
        return await ctx.send(f"❌ {err}")
    delta = parse_duration(duration)
    if delta is None:
        return await ctx.send("❌ durée invalide. format: 30s, 10m, 2h, 1d")
    if delta > MAX_TIMEOUT:
        return await ctx.send("❌ max 28 jours")
    await member.timeout(delta, reason=f"{ctx.author}: {reason}")
    await ctx.send(f"🔇 {member} muté {duration}. raison: {reason}")
    await log(ctx.guild, f"🔇 {member} muté {duration} par {ctx.author} ({reason})")
    record_infraction(ctx.guild.id, member.id, f"mute {duration}: {reason}")


GIVEAWAY_EMOJI = "🎉"


async def end_giveaway(channel: discord.TextChannel, msg_id: int, prize: str, seconds: int):
    await asyncio.sleep(seconds)
    try:
        msg = await channel.fetch_message(msg_id)
    except discord.HTTPException:
        return
    reaction = discord.utils.get(msg.reactions, emoji=GIVEAWAY_EMOJI)
    users = [u async for u in reaction.users() if not u.bot] if reaction else []
    if not users:
        await channel.send(f"😢 personne n'a participé à **{prize}**")
    else:
        winner = random.choice(users)
        await channel.send(f"🎉 félicitations {winner.mention}, tu gagnes **{prize}** !")
    await log(channel.guild, f"🎉 giveaway {prize} terminé dans #{channel.name}")


@bot.command(name="gw")
@commands.has_permissions(manage_guild=True)
@commands.bot_has_permissions(send_messages=True, add_reactions=True, read_message_history=True)
async def gw(ctx: commands.Context, duration: str, channel: discord.TextChannel, *, prize: str):
    """!gw 10m #salon <cadeau> — lance un giveaway"""
    delta = parse_duration(duration)
    if delta is None:
        return await ctx.send("❌ durée invalide. format: 30s, 10m, 2h, 1d")
    seconds = int(delta.total_seconds())
    if seconds < 10 or seconds > 7 * 86400:
        return await ctx.send("❌ durée entre 10 secondes et 7 jours")
    end_ts = int(time.time()) + seconds
    embed = discord.Embed(
        title=f"🎉 Giveaway : {prize}",
        description=f"Réagis avec 🎉 pour participer.\nFin : <t:{end_ts}:R>\nOrganisé par {ctx.author.mention}",
        color=0xF1C40F,
    )
    msg = await channel.send(embed=embed)
    await msg.add_reaction(GIVEAWAY_EMOJI)
    await ctx.send(f"✅ giveaway lancé dans {channel.mention}")
    await log(ctx.guild, f"🎉 giveaway « {prize} » lancé par {ctx.author} dans #{channel.name} ({duration})")
    asyncio.create_task(end_giveaway(channel, msg.id, prize, seconds))


NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ouvrir un ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="osaka_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        name = "ticket-" + (re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower()) or "user")
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing:
            return await interaction.response.send_message(f"tu as déjà un ticket : {existing.mention}", ephemeral=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        try:
            ch = await guild.create_text_channel(name, overwrites=overwrites, reason=f"ticket de {interaction.user}")
        except discord.HTTPException:
            return await interaction.response.send_message("❌ impossible de créer le ticket", ephemeral=True)
        await ch.send(f"{interaction.user.mention} ton ticket est ouvert. explique ta demande. tape `{PREFIX}close` pour le fermer.")
        await interaction.response.send_message(f"🎫 ticket ouvert : {ch.mention}", ephemeral=True)


@bot.command(name="ticketpanel")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(send_messages=True, embed_links=True)
async def ticketpanel(ctx: commands.Context):
    """Poste le panneau avec le bouton d'ouverture de ticket."""
    embed = discord.Embed(
        title="🎫 Support",
        description="clique sur le bouton pour ouvrir un ticket privé avec le staff.",
        color=0x5865F2,
    )
    await ctx.send(embed=embed, view=TicketView())


@bot.command(name="close")
async def close(ctx: commands.Context):
    """Ferme un ticket (à lancer dans le salon du ticket)."""
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send("❌ ce n'est pas un salon de ticket")
    await ctx.send("🔒 fermeture dans 5 secondes")
    await asyncio.sleep(5)
    try:
        await ctx.channel.delete(reason=f"ticket fermé par {ctx.author}")
    except discord.HTTPException:
        pass
    await log(ctx.guild, f"🎫 ticket #{ctx.channel.name} fermé par {ctx.author}")


@bot.command(name="poll")
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(send_messages=True, add_reactions=True)
async def poll(ctx: commands.Context, *, content: str):
    """!poll Question | option 1 | option 2 ..."""
    parts = [p.strip() for p in content.split("|") if p.strip()]
    if len(parts) < 3 or len(parts) > 11:
        return await ctx.send("❌ usage: `!poll Question | option 1 | option 2` (2 à 10 options)")
    question, options = parts[0], parts[1:]
    desc = "\n".join(f"{NUMBER_EMOJIS[i]} {opt}" for i, opt in enumerate(options))
    msg = await ctx.send(embed=discord.Embed(title=f"📊 {question}", description=desc, color=0x5865F2))
    for i in range(len(options)):
        await msg.add_reaction(NUMBER_EMOJIS[i])


@bot.command(name="infractions")
@commands.has_permissions(kick_members=True)
async def infractions(ctx: commands.Context, target: discord.User):
    """!infractions @user ou <id> — historique des sanctions"""
    items = DATA["infractions"].get(str(ctx.guild.id), {}).get(str(target.id), [])
    if not items:
        return await ctx.send(f"✅ aucune infraction pour {target}")
    lines = [f"<t:{i['ts']}:d> — {i['reason']}" for i in items[-10:]]
    embed = discord.Embed(title=f"Infractions de {target}", description="\n".join(lines), color=0xE74C3C)
    embed.set_footer(text=f"total : {len(items)} (10 dernières affichées)")
    await ctx.send(embed=embed)


# emoji -> nom exact du rôle (noms de rôles sensibles aux majuscules)
ROLE_REACTIONS = {
    "🌸": "foid",
    "🔵": "moid",
    "🩷": "rose",
    "⚪": "blanc",
    "🔴": "rouge",
    "🟠": "orange",
    "💙": "bleu",
    "🟢": "vert",
    "🟡": "jaune",
    "🟣": "violet",
}
COLOR_ROLES = {"rose", "blanc", "rouge", "orange", "bleu", "vert", "jaune", "violet"}  # une seule couleur à la fois


@bot.command(name="rolepanel")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(send_messages=True, add_reactions=True, manage_roles=True)
async def rolepanel(ctx: commands.Context):
    """Poste le panneau de rôles par réaction."""
    desc = "\n".join(f"{emoji} → rôle **{role}**" for emoji, role in ROLE_REACTIONS.items())
    embed = discord.Embed(
        title="🎭 Choisis tes rôles",
        description=desc + "\n\nréagis pour recevoir le rôle, retire ta réaction pour l'enlever.",
        color=0x5865F2,
    )
    msg = await ctx.send(embed=embed)
    for emoji in ROLE_REACTIONS:
        await msg.add_reaction(emoji)
    DATA["rolepanel"] = {"channel_id": ctx.channel.id, "message_id": msg.id}
    save_data()
    await log(ctx.guild, f"🎭 panneau de rôles posté par {ctx.author} dans #{ctx.channel.name}")


async def _toggle_role(payload: discord.RawReactionActionEvent, add: bool):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    panel = DATA.get("rolepanel")
    if not panel or payload.message_id != panel["message_id"]:
        return
    role_name = ROLE_REACTIONS.get(str(payload.emoji))
    if not role_name:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = guild.get_member(payload.user_id)
    if member is None:
        return
    role = discord.utils.get(guild.roles, name=role_name)
    if role is None:
        return await log(guild, f"⚠️ rôle « {role_name} » introuvable sur le serveur")
    if add and role_name in COLOR_ROLES:
        # une seule couleur : retire les autres rôles couleur et leurs réactions
        channel = guild.get_channel(payload.channel_id)
        for other_emoji, other_name in ROLE_REACTIONS.items():
            if other_name in COLOR_ROLES and other_name != role_name:
                other = discord.utils.get(guild.roles, name=other_name)
                if other and other in member.roles:
                    try:
                        await member.remove_roles(other, reason="une seule couleur")
                    except discord.HTTPException:
                        pass
                if channel:
                    try:
                        msg = await channel.fetch_message(payload.message_id)
                        await msg.remove_reaction(other_emoji, member)
                    except discord.HTTPException:
                        pass
    try:
        if add:
            await member.add_roles(role, reason="rôle par réaction")
        else:
            await member.remove_roles(role, reason="rôle par réaction retiré")
    except discord.HTTPException as e:
        await log(guild, f"⚠️ impossible de modifier {role_name} pour {member}: {e}")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await _toggle_role(payload, add=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await _toggle_role(payload, add=False)


@bot.command(name="say")
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, send_messages=True)
async def say(ctx: commands.Context, *, text: str):
    """!say <texte> — le bot écrit le texte à ta place"""
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    # allowed_mentions none : le texte ne peut pas ping @everyone ni des rôles
    await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
    await log(ctx.guild, f"🗣️ say par {ctx.author} dans #{ctx.channel.name}: {text[:200]}")


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: int, *, reason: str = "aucune raison"):
    """!unban <id> [raison] — débannit un utilisateur par son ID"""
    try:
        await ctx.guild.unban(discord.Object(id=user_id), reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        return await ctx.send("❌ cet utilisateur n'est pas banni")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ échec du unban: {e}")
    await ctx.send(f"✅ `{user_id}` débanni. raison: {reason}")
    await log(ctx.guild, f"✅ `{user_id}` débanni par {ctx.author} ({reason})")


@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    await member.timeout(None, reason=f"unmute par {ctx.author}")
    await ctx.send(f"🔊 {member} démuté")


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ tu n'as pas la permission")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ il me manque une permission (Gérer les salons, Gérer les messages, etc.)")
    elif isinstance(error, (commands.MemberNotFound, commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.send(f"❌ usage: `{PREFIX}{ctx.command.qualified_name if ctx.command else '...'}`")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        raise error


# ---------- mini serveur web (healthcheck pour Render / Web Service) ----------
class HealthHandler(BaseHTTPRequestHandler):
    def _respond(self, send_body: bool):
        if self.path.split("?", 1)[0] == "/":
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        else:
            body = b"Not Found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_GET(self):
        self._respond(send_body=True)

    def do_HEAD(self):
        self._respond(send_body=False)

    def log_message(self, format, *args):  # pas de spam dans les logs à chaque ping
        pass


def start_web_server() -> None:
    try:
        port = int(os.environ.get("PORT", "10000"))
    except ValueError:
        port = 10000
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, name="web-server", daemon=True).start()
    print(f"Serveur web sur le port {port}", flush=True)


start_web_server()
bot.run(os.environ["DISCORD_TOKEN"])
