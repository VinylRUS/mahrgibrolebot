# discord_role_bot_slash_select_v2.py
"""
Discord self-role bot with persistent select menus (slash commands) + Twitch live notifications.
Requirements:
  - Python 3.11+
  - discord.py 2.3+
Env:
  - DISCORD_BOT_TOKEN (required)
  - GUILD_ID (optional, for fast guild command sync during testing)
  - TWITCH_CLIENT_ID (required for Twitch notifications)
  - TWITCH_CLIENT_SECRET (required for Twitch notifications)
"""
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List
from uuid import uuid4

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("role_bot")

# ---------- Config ----------
DATA_FILE = Path("config.json")


def default_data() -> dict:
    return {
        "join_role_id": None,
        "role_messages": [],
        "twitch_notifications": [],
        "twitch_state": {}
    }


def load_data() -> dict:
    defaults = default_data()
    if DATA_FILE.exists():
        try:
            loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
                return defaults
        except Exception:
            log.exception("Не удалось прочитать config.json — загружаю пустой конфиг.")
    return defaults


def save_data(d: dict) -> None:
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


data = load_data()

# ---------- Twitch ----------
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")


class TwitchAPI:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.access_token: Optional[str] = None
        self.expires_at: datetime = datetime.now(timezone.utc)

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_app_token(self) -> Optional[str]:
        if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
            return None
        now = datetime.now(timezone.utc)
        if self.access_token and now < self.expires_at:
            return self.access_token

        await self.ensure_session()
        assert self.session is not None

        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials"
        }
        try:
            async with self.session.post(url, params=params, timeout=15) as resp:
                payload = await resp.json()
                if resp.status != 200:
                    log.warning("Twitch token error %s: %s", resp.status, payload)
                    return None
                token = payload.get("access_token")
                expires_in = int(payload.get("expires_in", 0))
                if not token:
                    return None
                self.access_token = token
                self.expires_at = now + timedelta(seconds=max(60, expires_in - 60))
                return token
        except Exception as e:
            log.warning("Не удалось получить токен Twitch: %s", e)
            return None

    async def get_user(self, login: str) -> Optional[dict]:
        token = await self.get_app_token()
        if not token:
            return None

        await self.ensure_session()
        assert self.session is not None

        headers = {
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}"
        }
        url = "https://api.twitch.tv/helix/users"
        params = {"login": login}
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=15) as resp:
                payload = await resp.json()
                if resp.status != 200:
                    log.warning("Twitch users error %s: %s", resp.status, payload)
                    return None
                users = payload.get("data", [])
                return users[0] if users else None
        except Exception as e:
            log.warning("Ошибка запроса к Twitch users для %s: %s", login, e)
            return None

    async def get_live_stream(self, user_id: str) -> Optional[dict]:
        token = await self.get_app_token()
        if not token:
            return None

        await self.ensure_session()
        assert self.session is not None

        headers = {
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}"
        }
        url = "https://api.twitch.tv/helix/streams"
        params = {"user_id": user_id}
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=15) as resp:
                payload = await resp.json()
                if resp.status != 200:
                    log.warning("Twitch streams error %s: %s", resp.status, payload)
                    return None
                streams = payload.get("data", [])
                return streams[0] if streams else None
        except Exception as e:
            log.warning("Ошибка запроса к Twitch streams для %s: %s", user_id, e)
            return None


# ---------- Intents & Client ----------
GUILD_ID = os.environ.get("GUILD_ID")
TEST_GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

intents = discord.Intents.default()
intents.members = True
intents.guilds = True


class RoleClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.twitch = TwitchAPI()

    async def resolve_guild(self, guild_id: int) -> Optional[discord.Guild]:
        guild = self.get_guild(guild_id)
        if guild is not None:
            return guild
        try:
            return await self.fetch_guild(guild_id)
        except discord.DiscordException:
            log.exception("Failed to fetch guild %s while restoring persistent views", guild_id)
            return None

    async def resolve_channel(self, guild: discord.Guild, channel_id: int) -> Optional[discord.abc.GuildChannel]:
        channel = guild.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            fetched_channel = await self.fetch_channel(channel_id)
        except discord.DiscordException:
            log.exception("Failed to fetch channel %s while restoring persistent views", channel_id)
            return None
        if isinstance(fetched_channel, discord.abc.GuildChannel):
            return fetched_channel
        log.warning("Channel %s is not a guild channel; skipping persistent view restore", channel_id)
        return None

    async def setup_hook(self):
        # Restore persistent views from saved config
        for item in data.get("role_messages", []):
            try:
                guild = await self.resolve_guild(item["guild_id"])
                if guild is None:
                    log.warning("Guild %s unavailable; skipping restore for message %s", item["guild_id"], item["message_id"])
                    continue
                channel = await self.resolve_channel(guild, item["channel_id"])
                if channel is None:
                    log.warning("Channel %s unavailable; skipping restore for message %s", item["channel_id"], item["message_id"])
                    continue
                # Ensure message exists
                message = await channel.fetch_message(item["message_id"])
                # Resolve roles that still exist in the guild
                roles = [guild.get_role(rid) for rid in item["role_ids"]]
                roles = [r for r in roles if r is not None]
                if not roles:
                    log.warning("No roles found in guild for saved message %s; skipping.", item["message_id"])
                    continue
                view = SelectView(uid=item["uid"], guild=guild, role_ids=[r.id for r in roles])
                self.add_view(view, message_id=message.id)
                log.info("Restored view uid=%s for message %s in guild %s", item["uid"], message.id, guild.id)
            except Exception as e:
                log.exception("Error restoring view for item %s: %s", item, e)

        if not twitch_poller.is_running():
            twitch_poller.start()

        # Sync commands (to test guild if provided, otherwise global)
        try:
            if TEST_GUILD:
                self.tree.copy_global_to(guild=TEST_GUILD)
                await self.tree.sync(guild=TEST_GUILD)
                log.info("Synced commands to test guild %s", GUILD_ID)
            else:
                await self.tree.sync()
                log.info("Synced global commands")
        except Exception as e:
            log.exception("Failed to sync commands: %s", e)

    async def close(self):
        if twitch_poller.is_running():
            twitch_poller.cancel()
        await self.twitch.close()
        await super().close()


bot = RoleClient()

# ---------- Helpers ----------
async def require_manage(interaction: discord.Interaction) -> bool:
    """Return True if the invoking user has manage_roles or admin, otherwise respond and return False."""
    if not interaction.guild:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return False
    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            await interaction.response.send_message("Не удалось определить ваши права.", ephemeral=True)
            return False
    if not (member.guild_permissions.manage_roles or member.guild_permissions.administrator):
        await interaction.response.send_message("Нужны права Manage Roles или Administrator.", ephemeral=True)
        return False
    return True


async def ensure_role_assignable(guild: discord.Guild, role: discord.Role):
    """Raise RuntimeError if the bot cannot assign the role (permissions/position)."""
    me = guild.me or await guild.fetch_member(bot.user.id)
    if not me.guild_permissions.manage_roles:
        raise RuntimeError("У бота нет права Manage Roles в этой гильдии.")
    if role.position >= me.top_role.position:
        raise RuntimeError("Роль выше роли бота — передвиньте роль бота выше.")
    return True


def normalize_login(login: str) -> str:
    return login.strip().lower().lstrip("@")


def get_twitch_items_for_guild(guild_id: int) -> List[dict]:
    return [it for it in data.get("twitch_notifications", []) if it.get("guild_id") == guild_id]


# ---------- Persistent Select & View ----------
class RoleSelect(discord.ui.Select):
    def __init__(self, uid: str, guild: discord.Guild, role_ids: List[int]):
        self.uid = uid
        self.role_ids = role_ids

        options: List[discord.SelectOption] = []
        for rid in role_ids:
            role = guild.get_role(rid)
            if role:
                label = role.name
                if len(label) > 80:
                    label = label[:77] + "..."
                options.append(discord.SelectOption(label=label, value=str(role.id)))
        # safety: if options empty (shouldn't happen for created menus), add placeholder option
        if not options:
            options = [discord.SelectOption(label="(нет доступных ролей)", value="0")]

        super().__init__(
            placeholder="Выберите роли (отметьте все, которые хотите оставить)",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"role_select_{uid}"
        )

    async def callback(self, interaction: discord.Interaction):
        # Acknowledge quickly to avoid "interaction failed"
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Команда доступна только на сервере.", ephemeral=True)
            return
        member = interaction.user  # type: ignore

        # Resolve roles from options (filter deleted)
        roles = [guild.get_role(int(opt.value)) for opt in self.options if opt.value.isdigit()]
        roles = [r for r in roles if r is not None]
        if not roles:
            await interaction.followup.send("Роли для этого меню не найдены (возможно удалены). Обратитесь к админам.", ephemeral=True)
            return

        # Check assignable
        for r in roles:
            try:
                await ensure_role_assignable(guild, r)
            except RuntimeError as e:
                await interaction.followup.send(f"Невозможно изменить роль **{r.name}**: {e}", ephemeral=True)
                return

        selected_ids = set(int(v) for v in self.values if v.isdigit())

        to_add = [r for r in roles if r.id in selected_ids and r not in member.roles]
        to_remove = [r for r in roles if r.id not in selected_ids and r in member.roles]

        parts: List[str] = []
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Self-select via select menu")
                parts.append("Выданы: " + ", ".join(r.name for r in to_add))
            if to_remove:
                await member.remove_roles(*to_remove, reason="Self-select via select menu")
                parts.append("Сняты: " + ", ".join(r.name for r in to_remove))
            if not parts:
                await interaction.followup.send("Нет изменений в ролях.", ephemeral=True)
            else:
                await interaction.followup.send("; ".join(parts), ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("У меня нет прав менять эти роли (Forbidden).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Ошибка при изменении ролей: {e}", ephemeral=True)


class SelectView(discord.ui.View):
    def __init__(self, uid: str, guild: discord.Guild, role_ids: List[int]):
        super().__init__(timeout=None)
        self.uid = uid
        self.role_ids = role_ids
        self.add_item(RoleSelect(uid=uid, guild=guild, role_ids=role_ids))


# ---------- Twitch Poller ----------
@tasks.loop(seconds=120)
async def twitch_poller():
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return

    notifications = data.get("twitch_notifications", [])
    if not notifications:
        return

    for item in notifications:
        try:
            guild_id = item.get("guild_id")
            channel_id = item.get("channel_id")
            streamer_login = item.get("streamer_login")

            if not guild_id or not channel_id or not streamer_login:
                continue

            guild = bot.get_guild(guild_id)
            if guild is None:
                continue

            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            user = await bot.twitch.get_user(streamer_login)
            if not user:
                continue

            stream = await bot.twitch.get_live_stream(user_id=user["id"])
            state_key = f"{guild_id}:{streamer_login}"
            last_announced = data.setdefault("twitch_state", {}).get(state_key)

            if stream:
                stream_id = stream.get("id")
                if stream_id and stream_id != last_announced:
                    stream_url = f"https://www.twitch.tv/{streamer_login}"
                    title = stream.get("title") or "Стрим начался"
                    game_name = stream.get("game_name") or "Без категории"
                    thumb = (stream.get("thumbnail_url") or "").replace("{width}", "1280").replace("{height}", "720")
                    msg = item.get("message") or "🔴 **{streamer}** запустил(а) стрим: {url}"
                    rendered = msg.format(streamer=streamer_login, url=stream_url, title=title, game=game_name)

                    embed = discord.Embed(
                        title=title,
                        description=f"[{streamer_login}]({stream_url}) сейчас в эфире",
                        color=discord.Color.purple(),
                    )
                    embed.add_field(name="Категория", value=game_name, inline=True)
                    started_at = stream.get("started_at")
                    if started_at:
                        embed.add_field(name="Старт", value=started_at.replace("T", " ").replace("Z", " UTC"), inline=True)
                    if thumb:
                        embed.set_image(url=thumb)
                    await channel.send(rendered, embed=embed)

                    data["twitch_state"][state_key] = stream_id
                    save_data(data)
                    log.info("Twitch notify sent for %s in guild %s", streamer_login, guild_id)
            else:
                if last_announced:
                    data["twitch_state"][state_key] = None
                    save_data(data)
        except Exception as e:
            log.warning("Ошибка twitch_poller item=%s: %s", item, e)


@twitch_poller.before_loop
async def before_twitch_poller():
    await bot.wait_until_ready()


# ---------- Events ----------
@bot.event
async def on_ready():
    log.info("Bot ready: %s (id=%s)", bot.user, bot.user.id)


@bot.event
async def on_member_join(member: discord.Member):
    role_id = data.get("join_role_id")
    if role_id:
        role = member.guild.get_role(role_id)
        if role:
            try:
                await ensure_role_assignable(member.guild, role)
                await member.add_roles(role, reason="Auto-join role")
                log.info("Assigned join role %s to %s in %s", role.name, member, member.guild.name)
            except Exception as e:
                log.warning("Failed to assign join role: %s", e)


# ---------- Slash commands ----------
async def require_manage_or_reply(interaction: discord.Interaction) -> bool:
    """Helper wrapper to check and reply; returns True if allowed."""
    return await require_manage(interaction)


@bot.tree.command(name="setjoinrole", description="Установить роль, выдаваемую при заходе")
@app_commands.describe(role="Роль, которая будет выдаваться при заходе")
async def setjoinrole(interaction: discord.Interaction, role: discord.Role):
    if not await require_manage_or_reply(interaction):
        return
    try:
        await ensure_role_assignable(interaction.guild, role)
    except Exception as e:
        await interaction.response.send_message(f"Невозможно установить роль: {e}", ephemeral=True)
        return
    data["join_role_id"] = role.id
    save_data(data)
    await interaction.response.send_message(f"Роль при заходе установлена: **{role.name}**", ephemeral=True)


@bot.tree.command(name="clearjoinrole", description="Очистить роль при заходе")
async def clearjoinrole(interaction: discord.Interaction):
    if not await require_manage_or_reply(interaction):
        return
    data["join_role_id"] = None
    save_data(data)
    await interaction.response.send_message("Роль при заходе очищена.", ephemeral=True)


@bot.tree.command(name="createrolemsg", description="Создать сообщение с select-menu для самоназначения ролей (до 10 ролей)")
@app_commands.describe(
    channel="Канал для сообщения",
    title="Заголовок embed",
    role1="Роль 1 (обязательно)",
    role2="Роль 2 (опционально)",
    role3="Роль 3",
    role4="Роль 4",
    role5="Роль 5",
    role6="Роль 6",
    role7="Роль 7",
    role8="Роль 8",
    role9="Роль 9",
    role10="Роль 10"
)
async def createrolemsg(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    role1: discord.Role,
    role2: Optional[discord.Role] = None,
    role3: Optional[discord.Role] = None,
    role4: Optional[discord.Role] = None,
    role5: Optional[discord.Role] = None,
    role6: Optional[discord.Role] = None,
    role7: Optional[discord.Role] = None,
    role8: Optional[discord.Role] = None,
    role9: Optional[discord.Role] = None,
    role10: Optional[discord.Role] = None
):
    if not await require_manage_or_reply(interaction):
        return

    roles = [r for r in (role1, role2, role3, role4, role5, role6, role7, role8, role9, role10) if r is not None]
    if not roles:
        await interaction.response.send_message("Нужно указать хотя бы одну роль.", ephemeral=True)
        return

    # Check assignable for each role
    for r in roles:
        try:
            await ensure_role_assignable(interaction.guild, r)
        except Exception as e:
            await interaction.response.send_message(f"Невозможно использовать роль **{r.name}**: {e}", ephemeral=True)
            return

    role_ids = [r.id for r in roles]
    uid = uuid4().hex

    embed = discord.Embed(title=title, description="Выберите роли из меню ниже. Отметьте все, которые хотите оставить.", color=discord.Color.blurple())
    view = SelectView(uid=uid, guild=interaction.guild, role_ids=role_ids)
    message = await channel.send(embed=embed, view=view)

    # persist
    data.setdefault("role_messages", []).append({
        "guild_id": interaction.guild.id,
        "channel_id": channel.id,
        "message_id": message.id,
        "role_ids": role_ids,
        "uid": uid,
        "title": title
    })
    save_data(data)

    # register view at runtime
    bot.add_view(view, message_id=message.id)

    await interaction.response.send_message(f"Создано сообщение с меню в {channel.mention} (id={message.id}).", ephemeral=True)


@bot.tree.command(name="listrolemessages", description="Показать сохранённые сообщения self-role на сервере")
async def listrolemessages(interaction: discord.Interaction):
    if not await require_manage_or_reply(interaction):
        return
    items = [it for it in data.get("role_messages", []) if it["guild_id"] == interaction.guild.id]
    if not items:
        await interaction.response.send_message("Нет сохранённых сообщений выдачи ролей на этом сервере.", ephemeral=True)
        return
    lines = []
    for it in items:
        ch = interaction.guild.get_channel(it["channel_id"])
        lines.append(f"- `{it['message_id']}` в {ch.mention if ch else 'канале(удален)'} — {it.get('title', '(без названия)')} — роли: {len(it['role_ids'])}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="removerolemsg", description="Удалить запись о сообщении с меню (не удаляет само сообщение в Discord)")
@app_commands.describe(message_id="ID сообщения для удаления из конфигурации")
async def removerolemsg(interaction: discord.Interaction, message_id: int):
    if not await require_manage_or_reply(interaction):
        return
    before = len(data.get("role_messages", []))
    data["role_messages"] = [it for it in data.get("role_messages", []) if not (it["guild_id"] == interaction.guild.id and it["message_id"] == message_id)]
    after = len(data.get("role_messages", []))
    save_data(data)
    if before == after:
        await interaction.response.send_message("Сообщение не найдено в config.", ephemeral=True)
    else:
        await interaction.response.send_message("Удалено из config. (Сообщение в Discord остаётся; если нужно — удалите вручную.)", ephemeral=True)


@bot.tree.command(name="reattachview", description="Попытаться вручную восстановить view для message_id из config")
@app_commands.describe(message_id="ID сообщения из config")
async def reattachview(interaction: discord.Interaction, message_id: int):
    if not await require_manage_or_reply(interaction):
        return
    items = [it for it in data.get("role_messages", []) if it["guild_id"] == interaction.guild.id and it["message_id"] == message_id]
    if not items:
        await interaction.response.send_message("Не найдено в config (проверь /listrolemessages).", ephemeral=True)
        return
    it = items[0]
    try:
        guild = interaction.guild
        channel = guild.get_channel(it["channel_id"])
        if channel is None:
            try:
                fetched_channel = await bot.fetch_channel(it["channel_id"])
            except discord.DiscordException as e:
                await interaction.response.send_message(f"Не удалось получить канал: {e}", ephemeral=True)
                return
            if not isinstance(fetched_channel, discord.abc.GuildChannel):
                await interaction.response.send_message("Указанный канал не относится к серверу.", ephemeral=True)
                return
            channel = fetched_channel
        message = await channel.fetch_message(it["message_id"])
        roles = [guild.get_role(rid) for rid in it["role_ids"]]
        roles = [r for r in roles if r is not None]
        if not roles:
            await interaction.response.send_message("Роли не найдены в гильдии — восстановление невозможно.", ephemeral=True)
            return
        view = SelectView(uid=it["uid"], guild=guild, role_ids=[r.id for r in roles])
        bot.add_view(view, message_id=message.id)
        await interaction.response.send_message("View успешно добавлен к сообщению.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Ошибка при попытке восстановить view: {e}", ephemeral=True)


@bot.tree.command(name="twitch_add", description="Добавить Twitch-оповещения о начале стрима в канал")
@app_commands.describe(streamer_login="Логин Twitch без https://twitch.tv/", channel="Канал для оповещений", message="Кастомный шаблон")
async def twitch_add(interaction: discord.Interaction, streamer_login: str, channel: discord.TextChannel, message: Optional[str] = None):
    if not await require_manage_or_reply(interaction):
        return

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        await interaction.response.send_message(
            "Нужно задать TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET в окружении бота.",
            ephemeral=True,
        )
        return

    streamer_login = normalize_login(streamer_login)
    if not streamer_login:
        await interaction.response.send_message("Укажите корректный логин Twitch.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    user = await bot.twitch.get_user(streamer_login)
    if not user:
        await interaction.followup.send("Twitch-канал не найден или Twitch API недоступен.", ephemeral=True)
        return

    items = data.setdefault("twitch_notifications", [])
    existing = None
    for it in items:
        if it.get("guild_id") == interaction.guild.id and it.get("streamer_login") == streamer_login:
            existing = it
            break

    if existing:
        existing["channel_id"] = channel.id
        if message is not None:
            existing["message"] = message
        text = f"Обновил Twitch-оповещения для **{streamer_login}** в {channel.mention}."
    else:
        items.append({
            "guild_id": interaction.guild.id,
            "streamer_login": streamer_login,
            "channel_id": channel.id,
            "message": message or "🔴 **{streamer}** запустил(а) стрим: {url}",
        })
        text = f"Добавил Twitch-оповещения для **{streamer_login}** в {channel.mention}."

    save_data(data)
    await interaction.followup.send(
        text + " Шаблон поддерживает: {streamer}, {url}, {title}, {game}.",
        ephemeral=True,
    )


@bot.tree.command(name="twitch_remove", description="Удалить Twitch-оповещения для стримера")
@app_commands.describe(streamer_login="Логин Twitch")
async def twitch_remove(interaction: discord.Interaction, streamer_login: str):
    if not await require_manage_or_reply(interaction):
        return

    streamer_login = normalize_login(streamer_login)
    before = len(data.get("twitch_notifications", []))
    data["twitch_notifications"] = [
        it for it in data.get("twitch_notifications", [])
        if not (it.get("guild_id") == interaction.guild.id and it.get("streamer_login") == streamer_login)
    ]
    after = len(data.get("twitch_notifications", []))
    save_data(data)

    if before == after:
        await interaction.response.send_message("Запись не найдена.", ephemeral=True)
    else:
        state_key = f"{interaction.guild.id}:{streamer_login}"
        if state_key in data.setdefault("twitch_state", {}):
            data["twitch_state"].pop(state_key, None)
            save_data(data)
        await interaction.response.send_message(f"Удалил Twitch-оповещения для **{streamer_login}**.", ephemeral=True)


@bot.tree.command(name="twitch_list", description="Список настроенных Twitch-оповещений на сервере")
async def twitch_list(interaction: discord.Interaction):
    if not await require_manage_or_reply(interaction):
        return

    items = get_twitch_items_for_guild(interaction.guild.id)
    if not items:
        await interaction.response.send_message("На этом сервере нет Twitch-оповещений.", ephemeral=True)
        return

    lines = []
    for it in items:
        ch = interaction.guild.get_channel(it.get("channel_id"))
        state = data.get("twitch_state", {}).get(f"{interaction.guild.id}:{it.get('streamer_login')}")
        lines.append(
            f"- **{it.get('streamer_login')}** → {ch.mention if ch else '#удалён-канал'} | активный stream_id: `{state or 'нет'}`"
        )

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------- Run ----------
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if not TOKEN:
        log.error("Установите переменную окружения DISCORD_BOT_TOKEN с токеном бота.")
        raise SystemExit(1)
    bot.run(TOKEN)
