# role_bot_slash_persistent.py
import os
import json
import logging
from pathlib import Path
from typing import Optional, List
from uuid import uuid4

import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("role-bot")

# --------- CONFIG & HELPERS ----------
DATA_FILE = Path("config.json")


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"join_role_id": None, "role_messages": []}


def save_data(d):
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


data = load_data()

# optional quick guild sync - set GUILD_ID env to test fast
GUILD_ID = os.environ.get("GUILD_ID")
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

# ---------- INTENTS & CLIENT ----------
intents = discord.Intents.default()
intents.members = True  # needed for on_member_join and roles
intents.guilds = True

class RoleClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Restore persistent views for saved messages
        for item in data.get("role_messages", []):
            try:
                guild = self.get_guild(item["guild_id"])
                if guild is None:
                    log.warning("Guild %s not in cache; skipping view restore.", item["guild_id"])
                    continue
                channel = guild.get_channel(item["channel_id"])
                if channel is None:
                    log.warning("Channel %s not in cache; skipping.", item["channel_id"])
                    continue
                # fetch message to ensure it exists
                message = await channel.fetch_message(item["message_id"])
                # build view and register
                roles = [guild.get_role(rid) for rid in item["role_ids"]]
                roles = [r for r in roles if r is not None]
                if not roles:
                    log.warning("No roles found for saved message %s; skipping.", item["message_id"])
                    continue
                view = SelectView(uid=item["uid"], role_ids=[r.id for r in roles])
                self.add_view(view, message_id=message.id)
                log.info("Restored view uid=%s for message %s (guild %s)", item["uid"], message.id, guild.id)
            except Exception as e:
                log.exception("Error restoring view for item %s: %s", item, e)

        # sync commands (guild if provided for fast testing)
        try:
            if GUILD:
                self.tree.copy_global_to(guild=GUILD)
                await self.tree.sync(guild=GUILD)
                log.info("Synced commands to guild %s", GUILD_ID)
            else:
                await self.tree.sync()
                log.info("Synced global commands")
        except Exception as e:
            log.exception("Failed to sync commands: %s", e)


bot = RoleClient()

# ---------- Utility checks ----------
def has_manage_roles(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        return False
    return member.guild_permissions.manage_roles or member.guild_permissions.administrator

async def ensure_role_assignable(guild: discord.Guild, role: discord.Role):
    me = guild.me or await guild.fetch_member(bot.user.id)
    if not me.guild_permissions.manage_roles:
        raise RuntimeError("У бота нет права Manage Roles.")
    if role.position >= me.top_role.position:
        raise RuntimeError("Роль выше роли бота — поставьте роль бота повыше.")
    return True

# ---------- Persistent Select & View ----------
class RoleSelect(discord.ui.Select):
    def __init__(self, uid: str, role_ids: List[int]):
        self.uid = uid
        self.role_ids = role_ids
        options = []
        # labels will be updated per-guild when callback runs (roles resolved)
        for rid in role_ids:
            options.append(discord.SelectOption(label=str(rid), value=str(rid)))
        super().__init__(
            placeholder="Выберите роли (подтвердите выбор)",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"role_select_{uid}"
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        member = interaction.user
        # Resolve roles from guild
        roles = [guild.get_role(int(rid)) for rid in self.role_ids]
        roles = [r for r in roles if r is not None]
        if not roles:
            await interaction.response.send_message("Роли для этого меню не найдены (возможно удалены).", ephemeral=True)
            return

        # Update options labels to actual role names (in case they were numeric)
        for opt in self.options:
            try:
                r = guild.get_role(int(opt.value))
                opt.label = r.name if r else opt.label
            except Exception:
                pass

        # Check assignability
        for r in roles:
            try:
                await ensure_role_assignable(guild, r)
            except RuntimeError as e:
                await interaction.response.send_message(f"Невозможно изменить роль **{r.name}**: {e}", ephemeral=True)
                return

        selected_ids = set(int(v) for v in self.values)
        to_add = [r for r in roles if r.id in selected_ids and r not in member.roles]
        to_remove = [r for r in roles if r.id not in selected_ids and r in member.roles]

        msg_parts = []
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Self-select via select menu")
                msg_parts.append(f"Выданы: {', '.join(r.name for r in to_add)}")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Self-select via select menu")
                msg_parts.append(f"Сняты: {', '.join(r.name for r in to_remove)}")
            if not msg_parts:
                await interaction.response.send_message("Нет изменений в ролях.", ephemeral=True)
            else:
                await interaction.response.send_message("; ".join(msg_parts), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("У меня нет прав менять эти роли.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Ошибка: {e}", ephemeral=True)

class SelectView(discord.ui.View):
    def __init__(self, uid: str, role_ids: List[int]):
        super().__init__(timeout=None)
        self.uid = uid
        self.role_ids = role_ids
        self.add_item(RoleSelect(uid=uid, role_ids=role_ids))

# ---------- Events ----------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")

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
@bot.tree.command(name="setjoinrole", description="Установить роль, выдаваемую при заходе")
@app_commands.describe(role="Роль для авто-выдачи")
async def setjoinrole(interaction: discord.Interaction, role: discord.Role):
    if not has_manage_roles(interaction):
        await interaction.response.send_message("Нужны права Manage Roles/Administrator.", ephemeral=True)
        return
    try:
        await ensure_role_assignable(interaction.guild, role)
    except Exception as e:
        await interaction.response.send_message(f"Невозможно установить эту роль: {e}", ephemeral=True)
        return
    data["join_role_id"] = role.id
    save_data(data)
    await interaction.response.send_message(f"Роль при заходе установлена: **{role.name}**", ephemeral=True)

@bot.tree.command(name="clearjoinrole", description="Сбросить роль при заходе")
async def clearjoinrole(interaction: discord.Interaction):
    if not has_manage_roles(interaction):
        await interaction.response.send_message("Нужны права Manage Roles/Administrator.", ephemeral=True)
        return
    data["join_role_id"] = None
    save_data(data)
    await interaction.response.send_message("Роль при заходе очищена.", ephemeral=True)

# /createrolemsg channel title role1..role10
@bot.tree.command(name="createrolemsg", description="Создать сообщение с select-menu для самоназначения ролей (до 10 ролей)")
@app_commands.describe(channel="Канал для сообщения", title="Заголовок embed (кратко)", 
                       role1="Роль 1 (обязательно)", role2="Роль 2 (опционально)", role3="Роль 3",
                       role4="Роль 4", role5="Роль 5", role6="Роль 6", role7="Роль 7", role8="Роль 8", role9="Роль 9", role10="Роль 10")
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
    if not has_manage_roles(interaction):
        await interaction.response.send_message("Нужны права Manage Roles/Administrator.", ephemeral=True)
        return

    roles = [r for r in [role1, role2, role3, role4, role5, role6, role7, role8, role9, role10] if r is not None]
    if not roles:
        await interaction.response.send_message("Нужно указать хотя бы одну роль.", ephemeral=True)
        return

    # check assignable for each
    for r in roles:
        try:
            await ensure_role_assignable(interaction.guild, r)
        except Exception as e:
            await interaction.response.send_message(f"Невозможно использовать роль **{r.name}**: {e}", ephemeral=True)
            return

    # Generate unique uid for this menu (used for custom_id)
    uid = uuid4().hex
    role_ids = [r.id for r in roles]

    embed = discord.Embed(title=title, description="Выберите роли из меню ниже. Отметьте все, которые хотите оставить.", color=discord.Color.blurple())
    view = SelectView(uid=uid, role_ids=role_ids)
    message = await channel.send(embed=embed, view=view)

    # persist: save guild_id, channel_id, message_id, role_ids, uid
    data.setdefault("role_messages", []).append({
        "guild_id": interaction.guild.id,
        "channel_id": channel.id,
        "message_id": message.id,
        "role_ids": role_ids,
        "uid": uid,
        "title": title
    })
    save_data(data)

    # register persistent view for runtime (also necessary if bot is not restarted)
    bot.add_view(view, message_id=message.id)

    await interaction.response.send_message(f"Создано сообщение с меню в {channel.mention} (id={message.id}).", ephemeral=True)

@bot.tree.command(name="listrolemessages", description="Показать сохранённые сообщения self-role на сервере")
async def listrolemessages(interaction: discord.Interaction):
    if not has_manage_roles(interaction):
        await interaction.response.send_message("Нужны права Manage Roles/Administrator.", ephemeral=True)
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
    if not has_manage_roles(interaction):
        await interaction.response.send_message("Нужны права Manage Roles/Administrator.", ephemeral=True)
        return
    before = len(data.get("role_messages", []))
    data["role_messages"] = [it for it in data.get("role_messages", []) if not (it["guild_id"] == interaction.guild.id and it["message_id"] == message_id)]
    after = len(data.get("role_messages", []))
    save_data(data)
    if before == after:
        await interaction.response.send_message("Сообщение не найдено в конфиге.", ephemeral=True)
    else:
        await interaction.response.send_message("Удалено из конфига. (Сообщение в Discord остаётся; если нужно — удалите вручную.)", ephemeral=True)

@bot.tree.command(name="reattachview", description="Попытаться вручную восстановить view для message_id из конфига")
@app_commands.describe(message_id="ID сообщения из конфига")
async def reattachview(interaction: discord.Interaction, message_id: int):
    if not has_manage_roles(interaction):
        await interaction.response.send_message("Нужны права Manage Roles/Administrator.", ephemeral=True)
        return
    items = [it for it in data.get("role_messages", []) if it["guild_id"] == interaction.guild.id and it["message_id"] == message_id]
    if not items:
        await interaction.response.send_message("Не найдено в конфиге (проверь /listrolemessages).", ephemeral=True)
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
        view = SelectView(uid=it["uid"], role_ids=[r.id for r in roles])
        bot.add_view(view, message_id=message.id)
        await interaction.response.send_message("View успешно добавлен к сообщению.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Ошибка при попытке восстановить view: {e}", ephemeral=True)


# ---------- Run ----------
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if not TOKEN:
        print("Установи переменную окружения DISCORD_BOT_TOKEN с токеном бота.")
        raise SystemExit(1)
    bot.run(TOKEN)
