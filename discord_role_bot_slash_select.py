# discord_role_bot_slash_select.py
"""
Discord role bot with slash-commands and select-menu role self-assign.
Features:
- /setjoinrole role
- /clearjoinrole
- /createrolemsg channel title role1 role2 ... role10  (creates a message with a Select menu)
- /listrolemessages
- /removerolemsg message_id
- persistent views restored on startup (select menus remain functional after restart)

Usage:
- Set environment variables: DISCORD_BOT_TOKEN (required), optionally GUILD_ID for quick guild-only slash registration
- Install: pip install -U discord.py
- Run: python discord_role_bot_slash_select.py

Notes:
- The command /createrolemsg supports up to 10 roles (role1..role10). Extend if you need more.
- Select menu behavior: user selects which roles (from the menu) they WANT to have; the bot will add chosen roles and remove the other roles from that menu.
- Bot needs Manage Roles permission and its top role must be above roles it assigns.
"""

import os
import json
import logging
from pathlib import Path
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("role-bot")

DATA_FILE = Path("config.json")

def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"join_role_id": None, "role_messages": []}

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

config = load_data()

# ---------- Intents & Bot ----------
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_ID = os.environ.get("GUILD_ID")  # set to a guild id string for fast testing
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

# ---------- Utilities ----------

def is_admin_check(interaction: discord.Interaction) -> bool:
    if interaction.user is None or interaction.guild is None:
        return False
    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        return False
    return member.guild_permissions.manage_roles or member.guild_permissions.administrator

def admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        ok = is_admin_check(interaction)
        if not ok:
            await interaction.response.send_message("Нужны права Manage Roles или Administrator.", ephemeral=True)
        return ok
    return app_commands.check(predicate)

async def ensure_role_assignable(guild: discord.Guild, role: discord.Role):
    me = guild.me
    if not me:
        me = await guild.fetch_member(bot.user.id)
    if not me.guild_permissions.manage_roles:
        raise RuntimeError("У бота нет права `Manage Roles` в этой гильдии.")
    if role.position >= me.top_role.position:
        raise RuntimeError("Роль выше роли бота. Поставь роль бота повыше.")
    return True

# ---------- Select View ----------
class RoleSelect(Select):
    def __init__(self, roles: List[discord.Role]):
        # options correspond to roles
        options = [discord.SelectOption(label=r.name, value=str(r.id), description=f"Роль: {r.name}") for r in roles]
        # allow multiple selection up to number of roles
        super().__init__(placeholder="Выберите роли (несколько) — подтвердите выбор",
                         min_values=0, max_values=len(options), options=options, custom_id=f"role_select_{roles[0].guild.id}_{roles[0].id if roles else 0}")
        self.role_ids = [r.id for r in roles]

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        member = interaction.user
        # selected values are role IDs as strings
        selected_ids = set(int(v) for v in self.values)
        # ensure roles still exist and are assignable
        roles = [guild.get_role(rid) for rid in self.role_ids]
        roles = [r for r in roles if r is not None]
        # check assignable per role
        for r in roles:
            try:
                await ensure_role_assignable(guild, r)
            except RuntimeError as e:
                await interaction.response.send_message(f"Невозможно изменить роль **{r.name}**: {e}", ephemeral=True)
                return
        # Now apply: add selected roles that member doesn't have, remove unselected roles that member has
        to_add = [r for r in roles if r.id in selected_ids and r not in member.roles]
        to_remove = [r for r in roles if r.id not in selected_ids and r in member.roles]
        results = []
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Self-select via select menu")
                results.append(f"Выданы: {', '.join(r.name for r in to_add)}")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Self-select via select menu")
                results.append(f"Сняты: {', '.join(r.name for r in to_remove)}")
            if not results:
                results_text = "Нет изменений в ролях."
            else:
                results_text = "; ".join(results)
            await interaction.response.send_message(results_text, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("У меня нет прав менять эти роли.", ephemeral=True)

class SelectView(View):
    def __init__(self, roles: List[discord.Role], message_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.roles = roles
        self.message_id = message_id
        self.add_item(RoleSelect(roles))

# ---------- Events ----------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")
    # restore persistent views
    for item in config.get("role_messages", []):
        try:
            guild = bot.get_guild(item["guild_id"])
            if guild is None:
                log.warning(f"Guild {item['guild_id']} not in bot cache; skipping view restore.")
                continue
            channel = guild.get_channel(item["channel_id"])
            if channel is None:
                log.warning(f"Channel {item['channel_id']} not in cache; skipping.")
                continue
            message = await channel.fetch_message(item["message_id"])
            roles = [guild.get_role(rid) for rid in item["role_ids"]]
            roles = [r for r in roles if r is not None]
            if not roles:
                log.warning("No roles found for saved role message; skipping.")
                continue
            view = SelectView(roles, message_id=message.id)
            bot.add_view(view, message_id=message.id)
            log.info(f"Restored select view for message {message.id} in guild {guild.id}")
        except Exception as e:
            log.exception("Error restoring role select view: %s", e)
    # sync commands (guild or global)
    try:
        if GUILD:
            bot.tree.copy_global_to(guild=GUILD)
            await bot.tree.sync(guild=GUILD)
            log.info("Slash commands synced to GUILD_ID")
        else:
            await bot.tree.sync()
            log.info("Global slash commands synced")
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)

@bot.event
async def on_member_join(member: discord.Member):
    role_id = config.get("join_role_id")
    if role_id:
        role = member.guild.get_role(role_id)
        if role:
            try:
                await ensure_role_assignable(member.guild, role)
                await member.add_roles(role, reason="Auto-join role")
                log.info(f"Assigned join role {role.name} to {member} in {member.guild.name}")
            except Exception as e:
                log.warning(f"Failed to assign join role: {e}")

# ---------- Slash commands ----------

@app_commands.command(name="setjoinrole", description="Установить роль, выдаваемую при заходе")
@app_commands.describe(role="Роль для авто-выдачи")
@admin_check()
async def slash_setjoinrole(interaction: discord.Interaction, role: discord.Role):
    try:
        await ensure_role_assignable(interaction.guild, role)
    except RuntimeError as e:
        await interaction.response.send_message(f"Невозможно установить эту роль: {e}", ephemeral=True)
        return
    config["join_role_id"] = role.id
    save_data(config)
    await interaction.response.send_message(f"Роль при заходе установлена: **{role.name}**", ephemeral=True)

@app_commands.command(name="clearjoinrole", description="Сбросить роль при заходе")
@admin_check()
async def slash_clearjoinrole(interaction: discord.Interaction):
    config["join_role_id"] = None
    save_data(config)
    await interaction.response.send_message("Роль при заходе очищена.", ephemeral=True)

# createrolemsg with up to 10 roles
@app_commands.command(name="createrolemsg", description="Создать сообщение с select-menu для самоназначения ролей")
@app_commands.describe(channel="Канал для сообщения", title="Заголовок embed", role1="Роль 1 (обязательно)", role2="Роль 2 (опционально)", role3="Роль 3",
                       role4="Роль 4", role5="Роль 5", role6="Роль 6", role7="Роль 7", role8="Роль 8", role9="Роль 9", role10="Роль 10")
@admin_check()
async def slash_createrolemsg(interaction: discord.Interaction, channel: discord.TextChannel, title: str, role1: discord.Role,
                              role2: Optional[discord.Role]=None, role3: Optional[discord.Role]=None, role4: Optional[discord.Role]=None,
                              role5: Optional[discord.Role]=None, role6: Optional[discord.Role]=None, role7: Optional[discord.Role]=None,
                              role8: Optional[discord.Role]=None, role9: Optional[discord.Role]=None, role10: Optional[discord.Role]=None):

    roles = [r for r in [role1, role2, role3, role4, role5, role6, role7, role8, role9, role10] if r is not None]
    if not roles:
        await interaction.response.send_message("Нужно указать хотя бы одну роль.", ephemeral=True)
        return
    # check assignable
    for r in roles:
        try:
            await ensure_role_assignable(interaction.guild, r)
        except RuntimeError as e:
            await interaction.response.send_message(f"Невозможно использовать роль **{r.name}**: {e}", ephemeral=True)
            return
    embed = discord.Embed(title=title, description="Выберите роли из меню ниже. Выберите все, которые хотите оставить.", color=discord.Color.blurple())
    view = SelectView(roles)
    message = await channel.send(embed=embed, view=view)
    # persist
    config.setdefault("role_messages", []).append({
        "guild_id": interaction.guild.id,
        "channel_id": channel.id,
        "message_id": message.id,
        "role_ids": [r.id for r in roles],
        "title": title
    })
    save_data(config)
    # register view for persistence
    bot.add_view(view, message_id=message.id)
    await interaction.response.send_message(f"Создано сообщение с меню в {channel.mention} (id={message.id}).", ephemeral=True)

@app_commands.command(name="listrolemessages", description="Показать сохранённые сообщения self-role на сервере")
@admin_check()
async def slash_listrolemessages(interaction: discord.Interaction):
    items = [it for it in config.get("role_messages", []) if it["guild_id"] == interaction.guild.id]
    if not items:
        await interaction.response.send_message("Нет сохранённых сообщений выдачи ролей на этом сервере.", ephemeral=True)
        return
    lines = []
    for it in items:
        ch = interaction.guild.get_channel(it["channel_id"])
        lines.append(f"- `{it['message_id']}` в {ch.mention if ch else 'канале(удален)'} — {it['title']} — роли: {len(it['role_ids'])}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@app_commands.command(name="removerolemsg", description="Удалить запись о сообщении с меню (не удаляет само сообщение в Discord)")
@app_commands.describe(message_id="ID сообщения для удаления из конфигурации")
@admin_check()
async def slash_removerolemsg(interaction: discord.Interaction, message_id: int):
    before = len(config.get("role_messages", []))
    config["role_messages"] = [it for it in config.get("role_messages", []) if not (it["guild_id"] == interaction.guild.id and it["message_id"] == message_id)]
    after = len(config.get("role_messages", []))
    save_data(config)
    if before == after:
        await interaction.response.send_message("Сообщение не найдено в конфиге.", ephemeral=True)
    else:
        await interaction.response.send_message("Удалено из конфига.", ephemeral=True)

# register commands onto the tree
bot.tree.add_command(slash_setjoinrole, guild=GUILD)
bot.tree.add_command(slash_clearjoinrole, guild=GUILD)
bot.tree.add_command(slash_createrolemsg, guild=GUILD)
bot.tree.add_command(slash_listrolemessages, guild=GUILD)
bot.tree.add_command(slash_removerolemsg, guild=GUILD)

# ---------- Run ----------
if __name__ == '__main__':
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if not TOKEN:
        print("Установи переменную окружения DISCORD_BOT_TOKEN с токеном бота.")
        raise SystemExit(1)
    bot.run(TOKEN)
