# discordboy 🎤🤖

A Discord bot that wakes up when it hears **"Hello Logitrix"**, records the next 10 seconds of speech, transcribes it with **OpenAI Whisper**, and replies with **ChatGPT** — all in text chat.

---

## Features

- 🎤 Joins Discord voice channels on demand
- 👂 Passively listens for the wake word **"Hello Logitrix"**
- 🗣️ Transcribes speech to text using OpenAI Whisper
- 🤖 Generates conversational replies via ChatGPT (GPT-3.5-turbo)
- 💬 Posts both the transcription and the AI reply in the text channel
- 🧠 Maintains per-user conversation history
- 🔄 Automatically returns to wake-word listening after each activation
- 🛡️ Handles corrupted audio packets gracefully (auto-restarts receiver)
- 🌐 Supports all languages that Whisper understands

---

## Prerequisites

| Tool | Notes |
|------|-------|
| Python 3.10+ | [python.org](https://www.python.org/downloads/) |
| ffmpeg | Required for audio processing (see below) |
| Discord Bot Token | [discord.com/developers](https://discord.com/developers/applications) |
| OpenAI API Key | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |

### Installing ffmpeg

- **macOS**: `brew install ffmpeg`
- **Ubuntu/Debian**: `sudo apt install ffmpeg`
- **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/BogdanSandu69/discordboy.git
cd discordboy
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```env
DISCORD_TOKEN=your_discord_bot_token_here
OPENAI_API_KEY=your_openai_api_key_here
```

### 4. Run the bot

```bash
python bot.py
```

---

## Getting a Discord Bot Token

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**.
2. Give it a name (e.g. "discordboy") and confirm.
3. In the left sidebar click **Bot**, then **Add Bot**.
4. Under **Token**, click **Reset Token** and copy the token into your `.env`.
5. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent**
   - **Message Content Intent**
   - **Voice State Intent** (enabled by default)
6. Go to **OAuth2 → URL Generator**, select the `bot` scope, then choose at minimum:
   - **Send Messages**
   - **Read Message History**
   - **Connect** (voice)
   - **Speak** (voice)
7. Copy the generated URL and open it to invite the bot to your server.

---

## Getting an OpenAI API Key

1. Sign up or log in at [platform.openai.com](https://platform.openai.com).
2. Go to **API Keys** and click **Create new secret key**.
3. Copy the key into your `.env`.

> **Note**: Using Whisper and GPT-3.5-turbo incurs costs. Check [openai.com/pricing](https://openai.com/pricing) for details.

---

## Available Commands

| Command | Description |
|---------|-------------|
| `!join` | Bot joins your current voice channel and starts listening for **"Hello Logitrix"** |
| `!leave` | Bot stops listening and leaves the voice channel |
| `!help` | Display this help information |

---

## How It Works

1. Use `!join` while you're in a voice channel.
2. The bot joins and passively listens for the wake word **"Hello Logitrix"**.
3. Say **"Hello Logitrix"** — the bot detects it and starts recording:

```
User: "Hello Logitrix, what's the weather like on Mars?"
Bot: 🎤 Wake word detected! Recording for 10 seconds… speak now!

Bot: **@YourName said:** What's the weather like on Mars?
     **Bot:** Mars has an extremely thin atmosphere…

Bot: 👂 Back to listening — say "Hello Logitrix" to activate.
```

4. Conversation history is maintained per user so the bot remembers context.
5. Use `!leave` to disconnect the bot from the voice channel.

---

## Configuration

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Your Discord bot token |
| `OPENAI_API_KEY` | Your OpenAI API key |

---

## Dependencies

```
discord.py[voice]>=2.3.0        # Discord API with voice support
discord-ext-voice-recv==0.5.2a179  # Voice receiving extension
openai>=1.0.0                   # Whisper STT + ChatGPT
PyNaCl>=1.5.0                   # Voice encryption
python-dotenv>=1.0.0            # .env file loading
```

---

## License

MIT
