# role_bot_fixed_complete.py
"""
Discord self-role bot with persistent select menus (slash commands).
Requirements:
  - Python 3.11+
  - discord.py 2.3+
Env:
  - DISCORD_BOT_TOKEN (required)
  - GUILD_ID (optional, for fast guild command sync during testing)
"""
import os
import json
import logging
from pathlib import Path
from typing import Optional, List
from uuid import uuid4

import discord
from discord import app_commands

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("role_bot")

# ---------- Config ----------
DATA_FILE = Path("config.json")


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Не удалось прочитать config.json — загружаю пустой конфиг.")
    return {"join_role_id": None, "role_messages": []}


def save_data(d: dict) -> None:
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


data = load_data()

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

    async def setup_hook(self):
        # Restore persistent views from saved config
        for item in data.get("role_messages", []):
            try:
                guild = self.get_guild(item["guild_id"])
                if guild is None:
                    log.warning("Guild %s not in client cache; skipping restore for message %s", item["guild_id"], item["message_id"])
                    continue
                channel = guild.get_channel(item["channel_id"])
                if channel is None:
                    log.warning("Channel %s not in cache; skipping restore for message %s", item["channel_id"], item["message_id"])
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
        lines.append(f"- `{it['message_id']}` в {ch.mention if ch else 'канале(удален)'} — {it.get('title','(без названия)')} — роли: {len(it['role_ids'])}")
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
        await interaction.response.send_message("Сообщение не найдено в конфиgе.", ephemeral=True)
    else:
        await interaction.response.send_message("Удалено из конфиgа. (Сообщение в Discord остаётся; если нужно — удалите вручную.)", ephemeral=True)

@bot.tree.command(name="reattachview", description="Попытаться вручную восстановить view для message_id из конфиgа")
@app_commands.describe(message_id="ID сообщения из конфиgа")
async def reattachview(interaction: discord.Interaction, message_id: int):
    if not await require_manage_or_reply(interaction):
        return
    items = [it for it in data.get("role_messages", []) if it["guild_id"] == interaction.guild.id and it["message_id"] == message_id]
    if not items:
        await interaction.response.send_message("Не найдено в конфиgе (проверь /listrolemessages).", ephemeral=True)
        return
    it = items[0]
    try:
        guild = interaction.guild
        channel = guild.get_channel(it["channel_id"])
        if channel is None:
            await interaction.response.send_message("Канал не найден в кеше бота.", ephemeral=True)
            return
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

# ---------- Run ----------
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if not TOKEN:
        log.error("Установите переменную окружения DISCORD_BOT_TOKEN с токеном бота.")
        raise SystemExit(1)
    bot.run(TOKEN)
