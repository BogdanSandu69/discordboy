import asyncio
import io
import logging
import os
import threading
import wave
from collections import defaultdict

import discord
from discord.ext import commands, voice_recv
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

# guild_id -> voice_recv.VoiceRecvClient
active_voice_clients: dict[int, voice_recv.VoiceRecvClient] = {}

# guild_id -> text channel where responses are posted
response_channels: dict[int, discord.TextChannel] = {}

# guild_id -> VoiceBufferSink currently recording
active_sinks: dict[int, "VoiceBufferSink"] = {}

# user_id -> list of {"role": ..., "content": ...}  (conversation history)
conversation_history: dict[int, list[dict]] = defaultdict(list)

# ---------------------------------------------------------------------------
# Audio sink
# ---------------------------------------------------------------------------


class VoiceBufferSink(voice_recv.AudioSink):
    """Accumulates raw PCM audio per user in memory.

    Audio is decoded to 48 kHz, stereo, 16-bit signed PCM by the library.
    """

    CHANNELS = 2
    SAMPLE_WIDTH = 2       # 16-bit → 2 bytes per sample
    SAMPLING_RATE = 48000

    def __init__(self) -> None:
        super().__init__()
        self._audio_data: dict[int, bytearray] = {}
        self._lock = threading.Lock()

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.User | None, data: voice_recv.VoiceData) -> None:
        if user is None:
            return
        with self._lock:
            buf = self._audio_data.setdefault(user.id, bytearray())
            buf.extend(data.pcm)

    def cleanup(self) -> None:
        pass

    def get_wav_bytes(self, user_id: int) -> bytes:
        """Return WAV-encoded audio for *user_id*, or empty bytes if none."""
        with self._lock:
            pcm = bytes(self._audio_data.get(user_id, b""))
        if not pcm:
            return b""
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(self.SAMPLE_WIDTH)
            wf.setframerate(self.SAMPLING_RATE)
            wf.writeframes(pcm)
        return wav_buf.getvalue()

    @property
    def user_ids(self) -> list[int]:
        with self._lock:
            return list(self._audio_data.keys())


# ---------------------------------------------------------------------------
# Audio processing
# ---------------------------------------------------------------------------


async def process_audio(sink: VoiceBufferSink, channel: discord.TextChannel) -> None:
    """Transcribe buffered audio for each user and post ChatGPT replies."""
    for user_id in sink.user_ids:
        try:
            wav_bytes = sink.get_wav_bytes(user_id)
            if not wav_bytes:
                continue

            audio_buf = io.BytesIO(wav_bytes)
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
        # Move to the new channel – stop current sink and process its audio first
        if vc.is_listening():
            vc.stop_listening()
        old_sink = active_sinks.pop(ctx.guild.id, None)
        old_channel = response_channels.get(ctx.guild.id)
        await vc.move_to(voice_channel)
        if old_sink and old_channel:
            await process_audio(old_sink, old_channel)
    else:
        try:
            vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        except discord.ClientException as exc:
            await ctx.send(f"❌ Could not connect to the voice channel: {exc}")
            return

    active_voice_clients[ctx.guild.id] = vc
    response_channels[ctx.guild.id] = ctx.channel

    # Begin capturing audio from all users in the channel
    sink = VoiceBufferSink()
    active_sinks[ctx.guild.id] = sink
    vc.listen(sink)

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
    sink = active_sinks.pop(ctx.guild.id, None)
    channel = response_channels.pop(ctx.guild.id, None)

    if vc.is_connected():
        if vc.is_listening():
            vc.stop_listening()
        await vc.disconnect()

    await ctx.send("👋 Left the voice channel. Processing your audio now…")
    log.info("Left voice channel in guild '%s'.", ctx.guild.name)

    if sink and channel:
        await process_audio(sink, channel)


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
