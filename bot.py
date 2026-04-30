import asyncio
import io
import logging
import os
import struct
import threading
import time
import wave
from collections import defaultdict
from enum import Enum, auto

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

# Wake word settings
WAKE_WORD = "hello logitrix"
WAKE_CHECK_INTERVAL = 3.0    # seconds between wake-word checks
WAKE_BUFFER_SECONDS = 3.0    # rolling audio window used for wake-word detection
RECORD_DURATION = 10.0       # seconds to record after the wake word
MIN_ACTIVATION_GAP = 2.0     # minimum seconds between activations
SPEECH_THRESHOLD = 200.0     # average PCM amplitude required to bother calling Whisper
SPEECH_SAMPLE_INTERVAL_MS = 10  # sample every N ms for energy detection
WAKE_WORD_STRIP_CHARS = " ,!?"  # characters to strip after removing leading wake word

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

# guild_id -> WakeWordSink currently in use
active_sinks: dict[int, "WakeWordSink"] = {}

# guild_id -> background asyncio Task running the wake-word loop
active_tasks: dict[int, asyncio.Task] = {}

# guild_id -> monotonic time of last successful wake-word activation
last_activation: dict[int, float] = {}

# user_id -> list of {"role": ..., "content": ...}  (conversation history)
conversation_history: dict[int, list[dict]] = defaultdict(list)

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class State(Enum):
    WAITING = auto()    # passively listening for the wake word
    RECORDING = auto()  # recording speech after wake word was detected


# ---------------------------------------------------------------------------
# Audio sink
# ---------------------------------------------------------------------------


class WakeWordSink(voice_recv.AudioSink):
    """Dual-mode audio sink.

    * WAITING mode  – maintains a rolling PCM buffer (last *wake_buffer_seconds*
      of audio) per user for cheap energy-gated wake-word detection.
    * RECORDING mode – accumulates all incoming PCM per user so the full
      utterance can be sent to Whisper.

    Audio delivered by discord-ext-voice-recv is already decoded to
    48 kHz / stereo / 16-bit signed PCM.
    """

    CHANNELS = 2
    SAMPLE_WIDTH = 2        # 16-bit → 2 bytes per sample
    SAMPLING_RATE = 48000
    BYTES_PER_SECOND = CHANNELS * SAMPLE_WIDTH * SAMPLING_RATE  # 192 000

    def __init__(self, wake_buffer_seconds: float = WAKE_BUFFER_SECONDS) -> None:
        super().__init__()
        self._max_wake_bytes = int(wake_buffer_seconds * self.BYTES_PER_SECOND)
        self._wake_data: dict[int, bytearray] = {}    # rolling per-user buffer
        self._record_data: dict[int, bytearray] = {}  # accumulation buffer
        self._recording = False
        self._lock = threading.Lock()

    # -- AudioSink interface -----------------------------------------------

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.User | None, data: voice_recv.VoiceData) -> None:
        if user is None:
            return
        pcm = data.pcm
        with self._lock:
            if self._recording:
                self._record_data.setdefault(user.id, bytearray()).extend(pcm)
            else:
                buf = self._wake_data.setdefault(user.id, bytearray())
                buf.extend(pcm)
                excess = len(buf) - self._max_wake_bytes
                if excess > 0:
                    del buf[:excess]

    def cleanup(self) -> None:
        pass

    # -- Wake-word helpers -------------------------------------------------

    def get_wake_wav(self, user_id: int) -> bytes:
        """Return WAV-encoded rolling buffer for *user_id*, or b'' if empty."""
        with self._lock:
            pcm = bytes(self._wake_data.get(user_id, b""))
        return self._pcm_to_wav(pcm) if pcm else b""

    def get_wake_pcm(self, user_id: int) -> bytes:
        """Return raw PCM bytes from wake buffer for *user_id*."""
        with self._lock:
            return bytes(self._wake_data.get(user_id, b""))

    @property
    def wake_user_ids(self) -> list[int]:
        with self._lock:
            return list(self._wake_data.keys())

    # -- Recording helpers -------------------------------------------------

    def start_recording(self) -> None:
        """Switch to RECORDING mode and clear any previous recording."""
        with self._lock:
            self._recording = True
            self._record_data.clear()

    def stop_and_get_recording(self) -> dict[int, bytes]:
        """Switch back to WAITING mode; return WAV bytes keyed by user_id."""
        with self._lock:
            self._recording = False
            result: dict[int, bytes] = {}
            for uid, pcm in self._record_data.items():
                if pcm:
                    result[uid] = self._pcm_to_wav(bytes(pcm))
            self._record_data.clear()
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
        (WakeWordSink.SAMPLING_RATE * WakeWordSink.CHANNELS * WakeWordSink.SAMPLE_WIDTH
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


def _transcribe_sync(wav_bytes: bytes, prompt: str = "") -> str:
    """Blocking Whisper transcription – call via run_in_executor."""
    buf = io.BytesIO(wav_bytes)
    buf.name = "audio.wav"
    if prompt:
        result = openai_client.audio.transcriptions.create(
            model="whisper-1", file=buf, prompt=prompt
        )
    else:
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


async def _transcribe(wav_bytes: bytes, prompt: str = "") -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_sync, wav_bytes, prompt)


async def _chat(messages: list[dict]) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _chat_sync, messages)


# ---------------------------------------------------------------------------
# Wake-word check
# ---------------------------------------------------------------------------


async def _contains_wake_word(wav_bytes: bytes) -> bool:
    """Return True when Whisper detects the wake word in *wav_bytes*."""
    try:
        text = await _transcribe(wav_bytes, prompt="hello logitrix")
        return WAKE_WORD in text.lower()
    except Exception as exc:
        log.warning("Wake-word transcription error (skipping): %s", exc)
        return False


# ---------------------------------------------------------------------------
# Post-wake-word audio processing
# ---------------------------------------------------------------------------


async def process_recording(
    recording: dict[int, bytes], channel: discord.TextChannel
) -> None:
    """Transcribe a wake-word-triggered recording and post ChatGPT replies."""
    for user_id, wav_bytes in recording.items():
        try:
            log.info("Transcribing post-wake recording for user %s…", user_id)
            text = await _transcribe(wav_bytes)
            if not text:
                log.info("Empty transcription for user %s – skipping.", user_id)
                continue

            # Strip the wake word from the beginning of the transcript
            cleaned = text.strip()
            lower = cleaned.lower()
            if lower.startswith(WAKE_WORD):
                cleaned = cleaned[len(WAKE_WORD):].lstrip(WAKE_WORD_STRIP_CHARS)
            if not cleaned:
                log.info("Only wake word spoken by user %s – skipping.", user_id)
                continue

            log.info("Transcribed (user %s): %s", user_id, cleaned)

            # Build conversation context
            history = conversation_history[user_id]
            history.append({"role": "user", "content": cleaned})
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

            log.info("Requesting ChatGPT response for user %s…", user_id)
            reply = await _chat(messages)
            history.append({"role": "assistant", "content": reply})

            await channel.send(
                f"**<@{user_id}> said:** {cleaned}\n**Bot:** {reply}"
            )

        except Exception as exc:
            log.exception("Error processing recording for user %s: %s", user_id, exc)
            await channel.send(
                "⚠️ Sorry, I ran into an error processing your audio. Please try again."
            )


# ---------------------------------------------------------------------------
# Background wake-word loop
# ---------------------------------------------------------------------------


async def wake_word_loop(
    guild_id: int,
    vc: voice_recv.VoiceRecvClient,
    sink: WakeWordSink,
    channel: discord.TextChannel,
) -> None:
    """Background task: listen for wake word, record, transcribe, reply."""
    state = State.WAITING
    log.info("Wake-word loop started for guild %s.", guild_id)

    try:
        while guild_id in active_voice_clients:
            if state == State.WAITING:
                await asyncio.sleep(WAKE_CHECK_INTERVAL)

                # Restart the voice receiver if the router crashed (e.g. corrupted
                # Opus packets) – this is the main resilience mechanism.
                if not vc.is_listening():
                    log.warning(
                        "Voice receiver stopped unexpectedly in guild %s; restarting.",
                        guild_id,
                    )
                    try:
                        vc.listen(sink)
                    except Exception as exc:
                        log.error("Failed to restart voice receiver: %s", exc)
                    continue

                # Rate-limit activations
                now = time.monotonic()
                if guild_id in last_activation:
                    if now - last_activation[guild_id] < MIN_ACTIVATION_GAP:
                        continue

                # Check each user's rolling buffer for the wake word.
                # Energy-gate first to avoid unnecessary Whisper calls.
                for user_id in sink.wake_user_ids:
                    pcm = sink.get_wake_pcm(user_id)
                    if not _has_speech(pcm):
                        continue
                    wav = sink.get_wake_wav(user_id)
                    if not wav:
                        continue
                    if await _contains_wake_word(wav):
                        log.info(
                            "Wake word detected from user %s in guild %s.",
                            user_id,
                            guild_id,
                        )
                        last_activation[guild_id] = time.monotonic()
                        state = State.RECORDING
                        sink.start_recording()
                        await channel.send(
                            f"🎤 Wake word detected! Recording for "
                            f"{int(RECORD_DURATION)} seconds… speak now!"
                        )
                        break  # one activation per check cycle

            elif state == State.RECORDING:
                await asyncio.sleep(RECORD_DURATION)
                recording = sink.stop_and_get_recording()
                state = State.WAITING

                if not recording:
                    await channel.send(
                        "🔇 No audio detected after wake word. "
                        'Say **"Hello Logitrix"** to try again.'
                    )
                else:
                    await process_recording(recording, channel)
                    await channel.send(
                        '👂 Back to listening — say **"Hello Logitrix"** to activate.'
                    )

    except asyncio.CancelledError:
        log.info("Wake-word loop cancelled for guild %s.", guild_id)
    except Exception as exc:
        log.exception(
            "Unexpected error in wake-word loop for guild %s: %s", guild_id, exc
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
            name='"Hello Logitrix"',
        )
    )


def _cancel_guild_task(guild_id: int) -> None:
    """Cancel and remove the background task for *guild_id* if it exists."""
    task = active_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()


@bot.command(name="join")
async def join(ctx: commands.Context):
    """Join the caller's voice channel and listen for the wake word."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ You must be in a voice channel first!")
        return

    voice_channel = ctx.author.voice.channel

    if ctx.guild.id in active_voice_clients:
        vc = active_voice_clients[ctx.guild.id]
        if vc.channel == voice_channel:
            await ctx.send(
                "✅ I'm already listening in your voice channel! "
                'Say **"Hello Logitrix"** to activate.'
            )
            return
        # Move to a different channel: tear down existing state first
        _cancel_guild_task(ctx.guild.id)
        if vc.is_listening():
            vc.stop_listening()
        active_sinks.pop(ctx.guild.id, None)
        response_channels.pop(ctx.guild.id, None)
        await vc.move_to(voice_channel)
    else:
        try:
            vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        except discord.ClientException as exc:
            await ctx.send(f"❌ Could not connect to the voice channel: {exc}")
            return

    active_voice_clients[ctx.guild.id] = vc
    response_channels[ctx.guild.id] = ctx.channel

    sink = WakeWordSink()
    active_sinks[ctx.guild.id] = sink
    vc.listen(sink)

    task = asyncio.create_task(
        wake_word_loop(ctx.guild.id, vc, sink, ctx.channel)
    )
    active_tasks[ctx.guild.id] = task

    await ctx.send(
        f'🎤 Joined **{voice_channel.name}**! Say **"Hello Logitrix"** to activate.\n'
        "Use `!leave` to disconnect."
    )
    log.info(
        "Joined voice channel '%s' in guild '%s'.", voice_channel.name, ctx.guild.name
    )


@bot.command(name="leave")
async def leave(ctx: commands.Context):
    """Stop listening and leave the voice channel."""
    if ctx.guild.id not in active_voice_clients:
        await ctx.send("❌ I'm not in a voice channel right now.")
        return

    _cancel_guild_task(ctx.guild.id)
    vc = active_voice_clients.pop(ctx.guild.id)
    active_sinks.pop(ctx.guild.id, None)
    response_channels.pop(ctx.guild.id, None)
    last_activation.pop(ctx.guild.id, None)

    if vc.is_connected():
        if vc.is_listening():
            vc.stop_listening()
        await vc.disconnect()

    await ctx.send("👋 Left the voice channel. See you next time!")
    log.info("Left voice channel in guild '%s'.", ctx.guild.name)


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Display available commands."""
    embed = discord.Embed(
        title="🤖 DiscordBoy – Voice AI Bot",
        description=(
            'Say **"Hello Logitrix"** to wake me up, then speak your message. '
            "I'll transcribe it and reply via ChatGPT!"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="`!join`",
        value=(
            'Join your current voice channel and start listening for '
            '**"Hello Logitrix"**.'
        ),
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
    embed.add_field(
        name="💡 Usage",
        value=(
            "1. Join a voice channel and type `!join`\n"
            '2. Say **"Hello Logitrix"** to activate the bot\n'
            "3. Speak your message within the next 10 seconds\n"
            "4. The bot will transcribe your words and reply!"
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
