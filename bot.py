import os
import sqlite3
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import tasks

# ---------- Journalisation ----------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s",
)
logger = logging.getLogger("reminder-bot")

# ---------- Constantes ----------
TZ = ZoneInfo("Europe/Paris")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__),"data.db"))

ALLOWED_MENTIONS = discord.AllowedMentions(roles=True, users=False, everyone=False)

# ---------- Base de données ----------
def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Crée les tables si besoin + colonnes nécessaires."""
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

def fetch_settings(guild_id: int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    conn.close()
    return row

# ---------- Bot & commandes ----------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    init_db()
    try:
        for g in client.guilds:
            await tree.sync(guild=g)
            logger.info(f"Commandes slash synchronisées IMMÉDIATEMENT pour le serveur: {g.name} (id={g.id})")
    except Exception as e:
        logger.exception(f"Erreur de synchronisation des commandes : {e}")

    # Démarre la boucle de rappels
    check_reminders.start()
    logger.info(f"Connecté en tant que {client.user} (id={client.user.id})")


# ---------- Commandes de configuration ----------
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

# ---------- Commandes principales ----------
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
    try:
        dt = datetime.strptime(date.strip(), "%d/%m/%Y").replace(tzinfo=TZ)
    except ValueError:
        await interaction.response.send_message(
            "❌ Date invalide. Format attendu : JJ/MM/AAAA (ex: 21/10/2025).", ephemeral=True
        )
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
    try:
        dt = datetime.strptime(date.strip(), "%d/%m/%Y").replace(tzinfo=TZ)
    except ValueError:
        await interaction.response.send_message(
            "❌ Date invalide. Format attendu : JJ/MM/AAAA (ex: 21/10/2025).",
            ephemeral=True
        )
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

    msg = f"✅ Condamnation enregistrée pour **{pseudo}** (le {dt.strftime('%d/%m/%Y')}). Rappel dans 7 jours."
    if restore_role_id or restore_role_name:
        display = role_a_restituer.mention if role_a_restituer else restore_role_name
        msg += f" Rôle à restituer : {display}"
    await interaction.response.send_message(msg, ephemeral=True)

# ---------- Diagnostic : /config ----------
@tree.command(name="config", description="Voir la configuration enregistrée pour ce serveur")
async def config(interaction: discord.Interaction):
    s = fetch_settings(interaction.guild_id)
    if not s:
        await interaction.response.send_message("Aucune configuration enregistrée pour ce serveur.", ephemeral=True)
        return
    arrivants = f"<#{s['arrivants_channel_id']}>" if s['arrivants_channel_id'] else "—"
    condamnes = f"<#{s['condamnes_channel_id']}>" if s['condamnes_channel_id'] else "—"
    role = f"<@&{s['gerants_role_id']}>" if s['gerants_role_id'] else "—"

    embed = discord.Embed(title=f"Configuration • {interaction.guild.name}", color=discord.Color.green())
    embed.add_field(name="Salon arrivants", value=arrivants, inline=False)
    embed.add_field(name="Salon condamnés", value=condamnes, inline=False)
    embed.add_field(name="Rôle gérants", value=role, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- Diagnostic : /due (liste ce qui est dû dans CE serveur) ----------
@tree.command(name="due", description="Lister ce qui est dû (arrivées/condamnations) pour ce serveur")
async def due(interaction: discord.Interaction):
    gid = interaction.guild_id
    today_iso = datetime.now(TZ).date().isoformat()

    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT a.id, a.pseudo, a.date_iso, a.profile
        FROM arrivals a
        JOIN settings s ON s.guild_id = a.guild_id
        WHERE a.guild_id=?
          AND a.reminder_sent=0
          AND s.arrivants_channel_id IS NOT NULL
          AND date(a.date_iso, '+7 days') <= date(?)
        """,
        (gid, today_iso),
    )
    arr = cur.fetchall()

    cur.execute(
        """
        SELECT c.id, c.pseudo, c.date_iso, c.restore_role_id, c.restore_role_name
        FROM condemns c
        JOIN settings s ON s.guild_id = c.guild_id
        WHERE c.guild_id=?
          AND c.reminder_sent=0
          AND s.condamnes_channel_id IS NOT NULL
          AND date(c.date_iso, '+7 days') <= date(?)
        """,
        (gid, today_iso),
    )
    con = cur.fetchall()

    conn.close()

    if not arr and not con:
        await interaction.response.send_message("Rien n'est dû pour ce serveur ✅", ephemeral=True)
        return

    embed = discord.Embed(title="Éléments DÛS", color=discord.Color.orange())
    if arr:
        txt = "\n".join([f"• #{r['id']} — {r['pseudo']} (arrivé le {datetime.fromisoformat(r['date_iso']).strftime('%d/%m/%Y')})"
                         + (f" — profil: {r['profile']}" if r['profile'] else "")
                         for r in arr])
        embed.add_field(name=f"Arrivées ({len(arr)})", value=txt[:1024], inline=False)
    if con:
        def role_disp(r):
            if r["restore_role_id"]:
                return f"<@&{r['restore_role_id']}>"
            return r["restore_role_name"] or "—"
        txt = "\n".join([f"• #{r['id']} — {r['pseudo']} (condamné le {datetime.fromisoformat(r['date_iso']).strftime('%d/%m/%Y')}) — rôle: {role_disp(r)}"
                         for r in con])
        embed.add_field(name=f"Condamnations ({len(con)})", value=txt[:1024], inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- Boucle de rappels automatiques (30s pour test) ----------
@tasks.loop(seconds=30)
async def check_reminders():
    now = datetime.now(TZ)
    today_iso = now.date().isoformat()
    logger.info(f"[loop] Tick… today={today_iso}")

    conn = get_db_conn()
    cur = conn.cursor()

    # Arrivées dues (tous serveurs où config ok)
    cur.execute(
        """
        SELECT a.id, a.guild_id, a.pseudo, a.date_iso, a.profile,
               s.arrivants_channel_id, s.gerants_role_id
        FROM arrivals a
        JOIN settings s ON s.guild_id = a.guild_id
        WHERE a.reminder_sent=0
          AND s.arrivants_channel_id IS NOT NULL
          AND date(a.date_iso, '+7 days') <= date(?)
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
          AND s.condamnes_channel_id IS NOT NULL
          AND date(c.date_iso, '+7 days') <= date(?)
        """,
        (today_iso,),
    )
    condemns_due = cur.fetchall()

    logger.info(f"[loop] Arrivées dues={len(arrivals_due)} | Condamnations dues={len(condemns_due)}")

    # ----- Envoi Arrivées -----
    for row in arrivals_due:
        try:
            channel = await client.fetch_channel(row["arrivants_channel_id"])
            ping = f"<@&{row['gerants_role_id']}>" if row["gerants_role_id"] else ""
            pseudo = row["pseudo"]
            profile = row["profile"] or "—"

            embed = discord.Embed(
                title="🎉 Un nouveau membre a fait son entrée !",
                description=f"**{pseudo}** a rejoint l’alliance il y a **7 jours** 🎂",
                color=discord.Color.blurple(),
                timestamp=datetime.now(TZ),
            )
            embed.add_field(name="Profil", value=profile, inline=True)
            embed.add_field(name="Décision ⚖️", value="On garde ou on kick ?", inline=False)
            embed.set_footer(text="Rappel arrivants • RevoBot")

            await channel.send(content=ping, embed=embed, allowed_mentions=ALLOWED_MENTIONS)
            cur.execute("UPDATE arrivals SET reminder_sent=1 WHERE id=?", (row["id"],))
            conn.commit()
            logger.info(f"[loop] Rappel ARRIVEE envoyé • guild={row['guild_id']} pseudo={pseudo}")
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

            fr_date = datetime.fromisoformat(row["date_iso"]).strftime("%d/%m/%Y")
            ping = f"<@&{row['gerants_role_id']}>" if row["gerants_role_id"] else ""

            if row["restore_role_id"]:
                role_text = f"<@&{row['restore_role_id']}>"
            elif row["restore_role_name"]:
                role_text = row["restore_role_name"]
            else:
                role_text = "—"

            embed = discord.Embed(
                title="⚖️ Jugement rendu",
                description=f"**{row['pseudo']}** a été condamné le **{fr_date}**.\nLa sentence est désormais levée ⛓️",
                color=discord.Color.dark_red(),
                timestamp=datetime.now(TZ),
            )
            embed.add_field(name="Il récupère son rôle de", value=role_text, inline=True)
            embed.add_field(name="Antécédents 📜", value=f"**{count}** condamnation(s) au total.", inline=False)
            embed.set_footer(text="Rappel condamnés • RevoBot")

            await channel.send(content=ping, embed=embed, allowed_mentions=ALLOWED_MENTIONS)
            cur.execute("UPDATE condemns SET reminder_sent=1 WHERE id=?", (row["id"],))
            conn.commit()
            logger.info(f"[loop] Rappel CONDAMNE envoyé • guild={row['guild_id']} pseudo={row['pseudo']}")
        except Exception as e:
            logger.exception(f"Erreur rappel condamnation : {e}")

    conn.close()

@check_reminders.before_loop
async def before_check_reminders():
    await client.wait_until_ready()

# ---------- Lancement ----------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("La variable d'environnement DISCORD_TOKEN est manquante.")
        raise SystemExit(1)
    client.run(token)

if __name__ == "__main__":
    main()

