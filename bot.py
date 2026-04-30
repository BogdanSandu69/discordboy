import asyncio
import io
import logging
import os
from collections import defaultdict

import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Maximum number of conversation turns kept in memory per user
MAX_HISTORY = 10

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# guild_id -> discord.VoiceClient
active_voice_clients: dict[int, discord.VoiceClient] = {}

# guild_id -> text channel where responses are posted
response_channels: dict[int, discord.TextChannel] = {}

# user_id -> list of {"role": ..., "content": ...}  (conversation history)
conversation_history: dict[int, list[dict]] = defaultdict(list)

# ---------------------------------------------------------------------------
# Audio callback
# ---------------------------------------------------------------------------


async def finished_recording(
    sink: discord.sinks.WaveSink,
    channel: discord.TextChannel,
    *args,
):
    """Called by discord.py after stop_recording(); processes every user's audio."""
    for user_id, audio in sink.audio_data.items():
        try:
            raw_bytes = audio.file.read()
            if not raw_bytes:
                continue

            audio_buf = io.BytesIO(raw_bytes)
            audio_buf.name = "audio.wav"

            log.info("Transcribing audio for user %s ...", user_id)

            # --- Speech-to-Text via Whisper ---
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_buf,
            )
            text = transcript.text.strip()

            if not text:
                log.info("Empty transcription for user %s – skipping.", user_id)
                continue

            log.info("Transcribed (user %s): %s", user_id, text)

            # --- Build conversation context ---
            history = conversation_history[user_id]
            history.append({"role": "user", "content": text})

            # Keep history within limit; each turn = 1 user msg + 1 assistant msg (2 entries)
            if len(history) > MAX_HISTORY * 2:
                history = history[-(MAX_HISTORY * 2):]
                conversation_history[user_id] = history

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a helpful, friendly voice assistant on Discord. "
                        "Keep responses concise and conversational."
                    ),
                }
            ] + history

            # --- AI response via ChatGPT ---
            log.info("Requesting ChatGPT response for user %s ...", user_id)
            completion = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
            )
            reply = completion.choices[0].message.content.strip()
            history.append({"role": "assistant", "content": reply})

            # --- Post result to text channel ---
            user_mention = f"<@{user_id}>"
            await channel.send(
                f"**{user_mention} said:** {text}\n**Bot:** {reply}"
            )

        except Exception as exc:
            log.exception("Error processing audio for user %s: %s", user_id, exc)
            await channel.send(
                "⚠️ Sorry, I ran into an error processing your audio. Please try again."
            )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="!join to start",
        )
    )


@bot.command(name="join")
async def join(ctx: commands.Context):
    """Join the caller's voice channel and start listening."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ You must be in a voice channel first!")
        return

    voice_channel = ctx.author.voice.channel

    # Already connected to this guild?
    if ctx.guild.id in active_voice_clients:
        vc = active_voice_clients[ctx.guild.id]
        if vc.channel == voice_channel:
            await ctx.send("✅ I'm already in your voice channel and listening!")
            return
        # Move to the new channel
        await vc.move_to(voice_channel)
    else:
        try:
            vc = await voice_channel.connect()
        except discord.ClientException as exc:
            await ctx.send(f"❌ Could not connect to the voice channel: {exc}")
            return

    active_voice_clients[ctx.guild.id] = vc
    response_channels[ctx.guild.id] = ctx.channel

    # Begin recording all users in the channel
    vc.start_recording(
        discord.sinks.WaveSink(),
        finished_recording,
        ctx.channel,
    )

    await ctx.send(
        f"🎤 Joined **{voice_channel.name}** and started listening!\n"
        "Use `!leave` when you're done."
    )
    log.info("Joined voice channel '%s' in guild '%s'.", voice_channel.name, ctx.guild.name)


@bot.command(name="leave")
async def leave(ctx: commands.Context):
    """Stop listening and leave the voice channel."""
    if ctx.guild.id not in active_voice_clients:
        await ctx.send("❌ I'm not in a voice channel right now.")
        return

    vc = active_voice_clients.pop(ctx.guild.id)
    response_channels.pop(ctx.guild.id, None)

    if vc.is_connected():
        # stop_recording triggers finished_recording callback
        try:
            vc.stop_recording()
        except Exception:
            pass
        # Give the finished_recording callback a moment to fire before disconnecting
        await asyncio.sleep(1)
        await vc.disconnect()

    await ctx.send("👋 Left the voice channel. Goodbye!")
    log.info("Left voice channel in guild '%s'.", ctx.guild.name)


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Display available commands."""
    embed = discord.Embed(
        title="🤖 DiscordBoy – Voice AI Bot",
        description="I listen to your voice, transcribe it, and reply via ChatGPT!",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="`!join`",
        value="Join your current voice channel and start listening.",
        inline=False,
    )
    embed.add_field(
        name="`!leave`",
        value="Stop listening and leave the voice channel.",
        inline=False,
    )
    embed.add_field(
        name="`!help`",
        value="Show this help message.",
        inline=False,
    )
    embed.set_footer(text="Powered by OpenAI Whisper & ChatGPT")
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. "
            "Copy .env.example to .env and fill in your token."
        )
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. "
            "Copy .env.example to .env and fill in your API key."
        )

    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
