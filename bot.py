import os
import sqlite3
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import tasks

# ===================== Journalisation =====================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s",
)
logger = logging.getLogger("reminder-bot")

# ===================== Constantes =========================
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

def get_tz() -> ZoneInfo | None:
    try:
        return ZoneInfo("Europe/Paris")
    except Exception:
        logger.warning("ZoneInfo Europe/Paris indisponible, datation sans fuseau.")
        return None

TZ = get_tz()

# ===================== Base de données ====================
def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Crée les tables (si besoin)."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            arrivants_channel_id INTEGER,
            condamnes_channel_id INTEGER,
            gerants_role_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS arrivals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            pseudo TEXT NOT NULL,
            date_iso TEXT NOT NULL,
            profile TEXT,
            reminder_sent INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS condemns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            pseudo TEXT NOT NULL,
            date_iso TEXT NOT NULL,
            restore_role_id INTEGER,
            restore_role_name TEXT,
            reminder_sent INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()

def upsert_setting(guild_id: int, field: str, value: Optional[int]):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,))
    cur.execute(f"UPDATE settings SET {field}=? WHERE guild_id=?", (value, guild_id))
    conn.commit()
    conn.close()

def fetch_settings(guild_id: int) -> Optional[sqlite3.Row]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    conn.close()
    return row

# ===================== Client & Intents ===================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def now_paris() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()

# ===================== Ready / Sync =======================
@client.event
async def on_ready():
    init_db()
    try:
        # Sync globale (pour avoir les commandes partout)
        await tree.sync()
        logger.info("Commandes slash synchronisées globalement.")
        # Sync immédiate par serveur (apparition instantanée)
        for g in client.guilds:
            await tree.sync(guild=g)
            logger.info(f"Commandes slash synchronisées IMMÉDIATEMENT pour le serveur: {g.name} (id={g.id})")
    except Exception as e:
        logger.exception(f"Erreur de synchronisation des commandes : {e}")

    check_reminders.change_interval(seconds=30)  # 30s pour tester facilement
    check_reminders.start()
    logger.info(f"Connecté en tant que {client.user} (id={client.user.id})")

# ===================== Utilitaires ========================
def parse_fr_date_or_fail(date_str: str) -> Optional[datetime]:
    try:
        dt = datetime.strptime(date_str.strip(), "%d/%m/%Y")
        return dt.replace(tzinfo=TZ) if TZ else dt
    except ValueError:
        return None

def iso_to_fr(date_iso: str) -> str:
    try:
        d = datetime.fromisoformat(date_iso)
        return d.strftime("%d/%m/%Y")
    except Exception:
        return date_iso

# ===================== Commandes de réglage ==============
@tree.command(name="set_salon_arrivants", description="Définir le salon pour les rappels d'arrivées")
async def set_salon_arrivants(interaction: discord.Interaction, salon: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Permission requise : Gérer le serveur.", ephemeral=True)
        return
    upsert_setting(interaction.guild_id, "arrivants_channel_id", salon.id)
    await interaction.response.send_message(f"✅ Salon Arrivants : {salon.mention}", ephemeral=True)

@tree.command(name="set_salon_condamnes", description="Définir le salon pour les rappels de condamnations")
async def set_salon_condamnes(interaction: discord.Interaction, salon: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Permission requise : Gérer le serveur.", ephemeral=True)
        return
    upsert_setting(interaction.guild_id, "condamnes_channel_id", salon.id)
    await interaction.response.send_message(f"✅ Salon Condamnés : {salon.mention}", ephemeral=True)

@tree.command(name="set_role_gerants", description="Définir le rôle à mentionner lors des rappels")
async def set_role_gerants(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Permission requise : Gérer le serveur.", ephemeral=True)
        return
    upsert_setting(interaction.guild_id, "gerants_role_id", role.id)
    await interaction.response.send_message(f"✅ Rôle des gérants défini : {role.mention}", ephemeral=True)

@tree.command(name="config", description="Afficher la configuration du serveur")
async def config(interaction: discord.Interaction):
    s = fetch_settings(interaction.guild_id)
    if not s:
        await interaction.response.send_message("Aucune config encore enregistrée.", ephemeral=True)
        return
    msg = (
        f"**Config actuelle**\n"
        f"- Salon arrivants: <#{s['arrivants_channel_id']}>\n"
        f"- Salon condamnés: <#{s['condamnes_channel_id']}>\n"
        f"- Rôle gérants: <@&{s['gerants_role_id']}>"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# ===================== Commandes principales ==============
@tree.command(name="arrivee", description="Enregistrer l'arrivée d'un membre (date JJ/MM/AAAA)")
@app_commands.describe(
    pseudo="Pseudo libre (pas forcément un membre Discord)",
    date="Date d'arrivée JJ/MM/AAAA",
    profil="Profil du joueur"
)
@app_commands.choices(
    profil=[
        app_commands.Choice(name="PVM OPTI", value="PVM OPTI"),
        app_commands.Choice(name="PVM BL", value="PVM BL"),
        app_commands.Choice(name="PVP OPTI", value="PVP OPTI"),
        app_commands.Choice(name="PVP PAS OPTI", value="PVP PAS OPTI"),
    ]
)
async def arrivee(interaction: discord.Interaction, pseudo: str, date: str, profil: app_commands.Choice[str]):
    dt = parse_fr_date_or_fail(date)
    if not dt:
        await interaction.response.send_message("❌ Date invalide. Format attendu : JJ/MM/AAAA.", ephemeral=True)
        return

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO arrivals (guild_id, pseudo, date_iso, profile) VALUES (?, ?, ?, ?)",
        (interaction.guild_id, pseudo.strip(), dt.date().isoformat(), profil.value),
    )
    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"✅ Arrivée enregistrée pour **{pseudo}** ({profil.value}) le {dt.strftime('%d/%m/%Y')}. Rappel dans 7 jours.",
        ephemeral=True,
    )

@tree.command(name="condamne", description="Enregistrer une condamnation (date JJ/MM/AAAA)")
@app_commands.describe(
    pseudo="Pseudo libre",
    date="Date JJ/MM/AAAA",
    role_a_restituer="(Optionnel) Rôle à lui rendre (sélecteur Discord)",
    role_nom="(Optionnel) Nom du rôle en texte si le rôle n'apparaît pas"
)
async def condamne(
    interaction: discord.Interaction,
    pseudo: str,
    date: str,
    role_a_restituer: Optional[discord.Role] = None,
    role_nom: Optional[str] = None
):
    dt = parse_fr_date_or_fail(date)
    if not dt:
        await interaction.response.send_message("❌ Date invalide. Format attendu : JJ/MM/AAAA.", ephemeral=True)
        return

    restore_role_id = role_a_restituer.id if role_a_restituer else None
    restore_role_name = role_a_restituer.name if role_a_restituer else (role_nom.strip() if role_nom else None)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO condemns (guild_id, pseudo, date_iso, restore_role_id, restore_role_name) VALUES (?, ?, ?, ?, ?)",
        (interaction.guild_id, pseudo.strip(), dt.date().isoformat(), restore_role_id, restore_role_name),
    )
    conn.commit()
    conn.close()

    details = f" Rôle à restituer : {role_a_restituer.mention}" if role_a_restituer else (f" Rôle à restituer : {restore_role_name}" if restore_role_name else "")
    await interaction.response.send_message(
        f"✅ Condamnation enregistrée pour **{pseudo}** (le {dt.strftime('%d/%m/%Y')}). Rappel dans 7 jours.{details}",
        ephemeral=True
    )

@tree.command(name="stats_condamnations", description="Afficher le nombre de condamnations d'un pseudo")
async def stats_condamnations(interaction: discord.Interaction, pseudo: str):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM condemns WHERE guild_id=? AND LOWER(pseudo)=LOWER(?)",
                (interaction.guild_id, pseudo.strip()))
    (count,) = cur.fetchone()
    conn.close()
    await interaction.response.send_message(f"📊 **{pseudo}** a **{count}** condamnation(s).", ephemeral=True)

# -------- Test immédiat (envoi d'embed) --------
@tree.command(name="test_ping", description="Envoyer un embed de test immédiatement")
@app_commands.describe(
    salon="Salon où envoyer le test",
    pseudo="Nom/pseudo à afficher",
    type="Type de message : arrivee ou condamne",
    profil="(Arrivée) Profil à afficher",
    role_a_restituer="(Condamné) Rôle à afficher (sélecteur Discord)",
    role_nom="(Condamné) Nom du rôle en texte si le rôle n'apparaît pas"
)
@app_commands.choices(
    profil=[
        app_commands.Choice(name="PVM OPTI", value="PVM OPTI"),
        app_commands.Choice(name="PVM BL", value="PVM BL"),
        app_commands.Choice(name="PVP OPTI", value="PVP OPTI"),
        app_commands.Choice(name="PVP PAS OPTI", value="PVP PAS OPTI"),
    ]
)
async def test_ping(
    interaction: discord.Interaction,
    salon: discord.TextChannel,
    pseudo: str,
    type: Literal["arrivee", "condamne"],
    profil: Optional[app_commands.Choice[str]] = None,
    role_a_restituer: Optional[discord.Role] = None,
    role_nom: Optional[str] = None
):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Permission requise : Gérer le serveur.", ephemeral=True)
        return

    settings = fetch_settings(interaction.guild_id)
    role_id_gerants = settings["gerants_role_id"] if settings else None
    ping_gerants = f"<@&{role_id_gerants}>" if role_id_gerants else ""

    if type == "arrivee":
        ptext = profil.value if profil else "—"
        embed = discord.Embed(
            title="🎉 Un nouveau membre a fait son entrée !",
            description=f"**{pseudo}** a rejoint l’alliance il y a **7 jours** 🎂",
            color=discord.Color.blurple(),
            timestamp=now_paris(),
        )
        embed.add_field(name="Profil", value=ptext, inline=True)
        embed.add_field(name="Décision ⚖️", value="On garde ou on kick ?", inline=False)
        embed.set_footer(text="Rappel arrivants • RevoBot")
    else:
        if role_a_restituer:
            role_display = role_a_restituer.mention
        elif role_nom and role_nom.strip():
            role_display = role_nom.strip()
        else:
            role_display = "—"

        embed = discord.Embed(
            title="⚖️ Jugement rendu",
            description=f"**{pseudo}** a été condamné le **{now_paris().strftime('%d/%m/%Y')}**.\nLa sentence est désormais levée ⛓️",
            color=discord.Color.dark_red(),
            timestamp=now_paris(),
        )
        embed.add_field(name="Il récupère son rôle de", value=role_display, inline=True)
        embed.add_field(name="Antécédents 📜", value="**(test)** condamnation(s) au total.", inline=False)
        embed.set_footer(text="Rappel condamnés • RevoBot")

    await salon.send(
        content=ping_gerants,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )
    await interaction.response.send_message("✅ Test envoyé.", ephemeral=True)

# -------- Outils admin: due & forcer l’envoi --------
@tree.command(name="due", description="Voir combien de rappels sont dus aujourd’hui")
async def due(interaction: discord.Interaction):
    today_iso = now_paris().date().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM arrivals a JOIN settings s ON s.guild_id=a.guild_id "
        "WHERE a.reminder_sent=0 AND s.arrivants_channel_id IS NOT NULL "
        "AND date(a.date_iso, '+7 days') <= date(?) AND a.guild_id=?",
        (today_iso, interaction.guild_id),
    )
    (arr_cnt,) = cur.fetchone()
    cur.execute(
        "SELECT COUNT(*) FROM condemns c JOIN settings s ON s.guild_id=c.guild_id "
        "WHERE c.reminder_sent=0 AND s.condamnes_channel_id IS NOT NULL "
        "AND date(c.date_iso, '+7 days') <= date(?) AND c.guild_id=?",
        (today_iso, interaction.guild_id),
    )
    (con_cnt,) = cur.fetchone()
    conn.close()
    if arr_cnt == 0 and con_cnt == 0:
        await interaction.response.send_message("Rien n'est dû pour ce serveur ✅", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"📌 Dû aujourd'hui : **{arr_cnt}** arrivée(s) • **{con_cnt}** condamnation(s).",
            ephemeral=True
        )

@tree.command(name="forcerappel", description="FORCE l'envoi de tous les rappels dus (admins seulement)")
async def forcerappel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Permission requise : Gérer le serveur.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await check_reminders_once()
    await interaction.followup.send("✅ Rappels forcés envoyés (si dûs).", ephemeral=True)

# ===================== Boucle de rappels ==================
@tasks.loop(seconds=30)  # 30s pour tester. Mets minutes=5 en prod si tu préfères.
async def check_reminders():
    await check_reminders_once()

async def check_reminders_once():
    now = now_paris()
    today_iso = now.date().isoformat()

    conn = get_db_conn()
    cur = conn.cursor()

    # Arrivées dues
    cur.execute(
        """
        SELECT a.id, a.guild_id, a.pseudo, a.date_iso, a.profile,
               s.arrivants_channel_id, s.gerants_role_id
        FROM arrivals a
        JOIN settings s ON s.guild_id = a.guild_id
        WHERE a.reminder_sent=0
          AND date(a.date_iso, '+7 days') <= date(?)
          AND s.arrivants_channel_id IS NOT NULL
        """,
        (today_iso,),
    )
    arrivals_due = cur.fetchall()

    # Condamnations dues
    cur.execute(
        """
        SELECT c.id, c.guild_id, c.pseudo, c.date_iso, c.restore_role_id, c.restore_role_name,
               s.condamnes_channel_id, s.gerants_role_id
        FROM condemns c
        JOIN settings s ON s.guild_id = c.guild_id
        WHERE c.reminder_sent=0
          AND date(c.date_iso, '+7 days') <= date(?)
          AND s.condamnes_channel_id IS NOT NULL
        """,
        (today_iso,),
    )
    condemns_due = cur.fetchall()

    logger.info(f"[loop] Tick… today={today_iso}")
    logger.info(f"[loop] Arrivées dues={len(arrivals_due)} | Condamnations dues={len(condemns_due)}")

    # ----- Envoi Arrivées -----
    for row in arrivals_due:
        try:
            channel = await client.fetch_channel(row["arrivants_channel_id"])
            role_tag = f"<@&{row['gerants_role_id']}>" if row["gerants_role_id"] else ""
            pseudo = row["pseudo"]
            profile = row["profile"] or "—"

            embed = discord.Embed(
                title="🎉 Un nouveau membre a fait son entrée !",
                description=f"**{pseudo}** a rejoint l’alliance il y a **7 jours** 🎂",
                color=discord.Color.blurple(),
                timestamp=now_paris(),
            )
            embed.add_field(name="Profil", value=profile, inline=True)
            embed.add_field(name="Décision ⚖️", value="On garde ou on kick ?", inline=False)
            embed.set_footer(text="Rappel arrivants • RevoBot")

            await channel.send(
                content=role_tag,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
            cur.execute("UPDATE arrivals SET reminder_sent=1 WHERE id=?", (row["id"],))
            conn.commit()
        except Exception as e:
            logger.exception(f"Erreur rappel arrivée : {e}")

    # ----- Envoi Condamnations -----
    for row in condemns_due:
        try:
            channel = await client.fetch_channel(row["condamnes_channel_id"])
            cur.execute(
                "SELECT COUNT(*) FROM condemns WHERE guild_id=? AND LOWER(pseudo)=LOWER(?)",
                (row["guild_id"], row["pseudo"]),
            )
            (count,) = cur.fetchone()

            fr_date = iso_to_fr(row["date_iso"])
            role_tag = f"<@&{row['gerants_role_id']}>" if row["gerants_role_id"] else ""
            pseudo = row["pseudo"]

            if row["restore_role_id"]:
                role_text = f"<@&{row['restore_role_id']}>"
            elif row["restore_role_name"]:
                role_text = row["restore_role_name"]
            else:
                role_text = "—"

            embed = discord.Embed(
                title="⚖️ Jugement rendu",
                description=f"**{pseudo}** a été condamné le **{fr_date}**.\nLa sentence est désormais levée ⛓️",
                color=discord.Color.dark_red(),
                timestamp=now_paris(),
            )
            embed.add_field(name="Il récupère son rôle de", value=role_text, inline=True)
            embed.add_field(name="Antécédents 📜", value=f"**{count}** condamnation(s) au total.", inline=False)
            embed.set_footer(text="Rappel condamnés • RevoBot")

            await channel.send(
                content=role_tag,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
            cur.execute("UPDATE condemns SET reminder_sent=1 WHERE id=?", (row["id"],))
            conn.commit()
        except Exception as e:
            logger.exception(f"Erreur rappel condamnation : {e}")

    conn.close()

@check_reminders.before_loop
async def before_check_reminders():
    await client.wait_until_ready()

# ===================== Lancement ==========================
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("La variable d'environnement DISCORD_TOKEN est manquante.")
        raise SystemExit(1)
    client.run(token)

if __name__ == "__main__":
    main()