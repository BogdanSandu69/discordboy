import asyncio
import io
import logging
import os
import struct
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

# Recording settings
RECORDING_DURATION = 10      # default recording duration in seconds
MAX_RECORDING_DURATION = 30  # maximum allowed recording duration
MIN_RECORDING_DURATION = 3   # minimum allowed recording duration
SPEECH_THRESHOLD = 200.0     # average PCM amplitude required to bother calling Whisper
SPEECH_SAMPLE_INTERVAL_MS = 10  # sample every N ms for energy detection

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

# guild_id -> whether a !record session is currently in progress
recording_in_progress: set[int] = set()

# user_id -> list of {"role": ..., "content": ...}  (conversation history)
conversation_history: dict[int, list[dict]] = defaultdict(list)

# ---------------------------------------------------------------------------
# Audio sink
# ---------------------------------------------------------------------------


class VoiceBufferSink(voice_recv.AudioSink):
    """Simple audio sink that accumulates PCM data per user during recording.

    Audio delivered by discord-ext-voice-recv is already decoded to
    48 kHz / stereo / 16-bit signed PCM.
    """

    CHANNELS = 2
    SAMPLE_WIDTH = 2        # 16-bit → 2 bytes per sample
    SAMPLING_RATE = 48000

    def __init__(self) -> None:
        super().__init__()
        self._record_data: dict[int, bytearray] = {}
        self._lock = threading.Lock()

    # -- AudioSink interface -----------------------------------------------

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.User | None, data: voice_recv.VoiceData) -> None:
        if user is None:
            return
        with self._lock:
            self._record_data.setdefault(user.id, bytearray()).extend(data.pcm)

    def cleanup(self) -> None:
        pass

    # -- Recording helpers -------------------------------------------------

    def get_recording(self) -> dict[int, bytes]:
        """Return WAV bytes keyed by user_id for all users who spoke."""
        with self._lock:
            result: dict[int, bytes] = {}
            for uid, pcm in self._record_data.items():
                if pcm:
                    result[uid] = self._pcm_to_wav(bytes(pcm))
            return result

    # -- PCM → WAV ---------------------------------------------------------

    def _pcm_to_wav(self, pcm: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(self.SAMPLE_WIDTH)
            wf.setframerate(self.SAMPLING_RATE)
            wf.writeframes(pcm)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Helpers: energy detection
# ---------------------------------------------------------------------------


def _has_speech(pcm: bytes, threshold: float = SPEECH_THRESHOLD) -> bool:
    """Return True when the PCM buffer has enough energy to contain speech.

    Samples every ~10 ms worth of data to compute mean absolute amplitude.
    Avoids calling Whisper on silent / near-silent buffers.
    """
    if len(pcm) < 400:
        return False
    # Each sample is a 16-bit signed int (2 bytes); step ≈ SPEECH_SAMPLE_INTERVAL_MS of audio
    step = max(
        2,
        (VoiceBufferSink.SAMPLING_RATE * VoiceBufferSink.CHANNELS * VoiceBufferSink.SAMPLE_WIDTH
         * SPEECH_SAMPLE_INTERVAL_MS) // 1000,
    )
    # Align step to 2-byte boundary
    step = step if step % 2 == 0 else step + 1
    total = 0
    count = 0
    for i in range(0, len(pcm) - 1, step):
        (val,) = struct.unpack_from("<h", pcm, i)
        total += abs(val)
        count += 1
    if count == 0:
        return False
    return (total / count) > threshold


# ---------------------------------------------------------------------------
# Helpers: OpenAI (run in executor to avoid blocking the event loop)
# ---------------------------------------------------------------------------


def _transcribe_sync(wav_bytes: bytes) -> str:
    """Blocking Whisper transcription – call via run_in_executor."""
    buf = io.BytesIO(wav_bytes)
    buf.name = "audio.wav"
    result = openai_client.audio.transcriptions.create(
        model="whisper-1", file=buf
    )
    return result.text.strip()


def _chat_sync(messages: list[dict]) -> str:
    """Blocking ChatGPT completion – call via run_in_executor."""
    completion = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
    )
    return completion.choices[0].message.content.strip()


async def _transcribe(wav_bytes: bytes) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_sync, wav_bytes)


async def _chat(messages: list[dict]) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _chat_sync, messages)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a helpful voice assistant with the personality of a wise, \
experienced 50-year-old man with a deep, commanding voice.

Your responses should be:
- Thoughtful and measured, not rushed
- Confident and authoritative, but friendly
- Use mature vocabulary and complete sentences
- Occasionally use phrases like "Well," "You see," "In my experience"
- Keep responses concise but impactful
- Sound like someone who has lived life and knows what they're talking about

Don't mention your age or voice explicitly - just embody this personality naturally."""


# ---------------------------------------------------------------------------
# Audio processing: transcribe and reply
# ---------------------------------------------------------------------------


async def process_audio(
    sink: VoiceBufferSink,
    channel: discord.TextChannel,
    requesting_user_id: int,
) -> None:
    """Transcribe recorded audio and post ChatGPT replies.

    Processes audio from all users who spoke during the recording window.
    Falls back to a single combined response attributed to the requesting user
    if no audio is found for other individual users.
    """
    recording = sink.get_recording()

    if not recording:
        await channel.send("🔇 No audio detected. Make sure you spoke during the recording!")
        return

    for user_id, wav_bytes in recording.items():
        try:
            # Energy-gate: skip silent buffers to avoid unnecessary Whisper calls
            pcm_buf = io.BytesIO(wav_bytes)
            with wave.open(pcm_buf, "rb") as wf:
                raw_pcm = wf.readframes(wf.getnframes())
            if not _has_speech(raw_pcm):
                log.info("No speech energy detected for user %s – skipping.", user_id)
                continue

            log.info("Transcribing recording for user %s…", user_id)
            text = await _transcribe(wav_bytes)
            if not text:
                log.info("Empty transcription for user %s – skipping.", user_id)
                continue

            log.info("Transcribed (user %s): %s", user_id, text)

            # Build conversation context
            history = conversation_history[user_id]
            history.append({"role": "user", "content": text})
            if len(history) > MAX_HISTORY * 2:
                history = history[-(MAX_HISTORY * 2):]
                conversation_history[user_id] = history

            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

            log.info("Requesting ChatGPT response for user %s…", user_id)
            reply = await _chat(messages)
            history.append({"role": "assistant", "content": reply})

            await channel.send(
                f"**<@{user_id}> said:** {text}\n**Bot:** {reply}"
            )

        except Exception as exc:
            log.exception("Error processing recording for user %s: %s", user_id, exc)
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
            name="!record",
        )
    )


@bot.command(name="join")
async def join(ctx: commands.Context):
    """Join the caller's voice channel."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ You must be in a voice channel first!")
        return

    voice_channel = ctx.author.voice.channel

    if ctx.guild.id in active_voice_clients:
        vc = active_voice_clients[ctx.guild.id]
        if vc.channel == voice_channel:
            await ctx.send(
                "✅ I'm already in your voice channel! Use `!record` to start recording."
            )
            return
        # Move to a different channel
        if vc.is_listening():
            vc.stop_listening()
        await vc.move_to(voice_channel)
    else:
        try:
            vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        except discord.ClientException as exc:
            await ctx.send(f"❌ Could not connect to the voice channel: {exc}")
            return

    active_voice_clients[ctx.guild.id] = vc

    await ctx.send(
        f"✅ Joined **{voice_channel.name}**! Use `!record` to start recording.\n"
        "Use `!leave` to disconnect."
    )
    log.info(
        "Joined voice channel '%s' in guild '%s'.", voice_channel.name, ctx.guild.name
    )


@bot.command(name="leave")
async def leave(ctx: commands.Context):
    """Leave the voice channel."""
    if ctx.guild.id not in active_voice_clients:
        await ctx.send("❌ I'm not in a voice channel right now.")
        return

    # Cancel any in-progress recording
    recording_in_progress.discard(ctx.guild.id)

    vc = active_voice_clients.pop(ctx.guild.id)

    if vc.is_connected():
        if vc.is_listening():
            vc.stop_listening()
        await vc.disconnect()

    await ctx.send("👋 Left the voice channel. See you next time!")
    log.info("Left voice channel in guild '%s'.", ctx.guild.name)


@bot.command(name="record")
async def record(ctx: commands.Context, duration: int = RECORDING_DURATION):
    """Record audio for the specified duration (default 10s), transcribe, and respond."""
    if ctx.guild.id not in active_voice_clients:
        await ctx.send("❌ I'm not in a voice channel! Use `!join` first.")
        return

    vc = active_voice_clients[ctx.guild.id]

    if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
        await ctx.send("❌ You must be in the same voice channel as me!")
        return

    if ctx.guild.id in recording_in_progress:
        await ctx.send("⏳ Already recording! Please wait for the current recording to finish.")
        return

    # Clamp duration to allowed range
    duration = max(MIN_RECORDING_DURATION, min(duration, MAX_RECORDING_DURATION))

    recording_in_progress.add(ctx.guild.id)
    sink = VoiceBufferSink()

    try:
        # Stop any previous listening before starting fresh
        if vc.is_listening():
            vc.stop_listening()

        vc.listen(sink)
        await ctx.send(f"🎤 Recording for {duration} seconds… speak now!")
        log.info("Recording started in guild '%s' for %s seconds.", ctx.guild.name, duration)

        await asyncio.sleep(duration)

        vc.stop_listening()
        await ctx.send("🔄 Processing your audio…")

        await process_audio(sink, ctx.channel, ctx.author.id)
        await ctx.send("✅ Ready! Use `!record` again to ask another question.")

    except Exception as exc:
        log.exception("Error during recording in guild '%s': %s", ctx.guild.name, exc)
        await ctx.send("⚠️ An error occurred during recording. Please try again.")
    finally:
        recording_in_progress.discard(ctx.guild.id)
        # Ensure we stop listening if something went wrong mid-session
        if vc.is_listening():
            vc.stop_listening()


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Display available commands."""
    embed = discord.Embed(
        title="🤖 DiscordBoy – Voice AI Bot",
        description=(
            "Use `!record` to ask me anything from your voice channel. "
            "I'll transcribe your speech and reply with ChatGPT!"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="`!join`",
        value="Join your current voice channel.",
        inline=False,
    )
    embed.add_field(
        name="`!record [seconds]`",
        value=(
            f"Record audio for the specified duration (default: {RECORDING_DURATION}s, "
            f"max: {MAX_RECORDING_DURATION}s), then transcribe and respond."
        ),
        inline=False,
    )
    embed.add_field(
        name="`!leave`",
        value="Leave the voice channel.",
        inline=False,
    )
    embed.add_field(
        name="`!help`",
        value="Show this help message.",
        inline=False,
    )
    embed.add_field(
        name="💡 Usage",
        value=(
            "1. Join a voice channel and type `!join`\n"
            "2. Type `!record` to start a 10-second recording\n"
            "3. Speak your message during the recording window\n"
            "4. The bot will transcribe your words and reply!\n"
            "5. Use `!record` again to ask another question"
        ),
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
