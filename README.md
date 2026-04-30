# discordboy 🎤🤖

A Discord bot that records speech on demand, transcribes it with **OpenAI Whisper**, and replies with **ChatGPT** — all in text chat.

---

## Features

- 🎤 Joins Discord voice channels on demand
- 🔴 Records audio on command with `!record`
- 🗣️ Transcribes speech to text using OpenAI Whisper
- 🤖 Generates conversational replies via ChatGPT (GPT-3.5-turbo) with a deep-voice personality
- 💬 Posts both the transcription and the AI reply in the text channel
- 🧠 Maintains per-user conversation history
- 🌐 Supports all languages that Whisper understands

---

## Prerequisites

| Tool | Notes |
|------|-------|
| Node.js 18+ | [nodejs.org](https://nodejs.org/) |
| Discord Bot Token | [discord.com/developers](https://discord.com/developers/applications) |
| OpenAI API Key | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/BogdanSandu69/discordboy.git
cd discordboy
```

### 2. Install dependencies

```bash
npm install
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
npm start
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
| `!join` | Bot joins your current voice channel |
| `!record [seconds]` | Record audio for the specified duration (default: 10s, max: 30s), then transcribe and respond |
| `!leave` | Bot leaves the voice channel |
| `!help` | Display this help information |

---

## How It Works

1. Use `!join` while you're in a voice channel.
2. The bot joins passively — no continuous listening.
3. Type `!record` to start a recording window:

```
User: !join
Bot: ✅ Joined #voice-channel! Use !record to start recording.

User: !record
Bot: 🎤 Recording for 10 seconds… speak now!
[10 seconds pass]
Bot: 🔄 Processing your audio…

Bot: **@YourName said:** What's the weather like on Mars?
     **Bot:** Well, Mars has an extremely thin atmosphere…

Bot: ✅ Ready! Use !record again to ask another question.
```

4. Conversation history is maintained per user so the bot remembers context.
5. Use `!leave` to disconnect the bot from the voice channel.
6. Optionally specify a custom duration: `!record 15` for a 15-second window.

---

## Configuration

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Your Discord bot token |
| `OPENAI_API_KEY` | Your OpenAI API key |

---

## Dependencies

```
discord.js ^14.14.1          Discord API client
@discordjs/voice ^0.16.1     Voice connection and audio receiving
opusscript ^0.1.1            Opus codec (pure JS, no native bindings needed)
prism-media ^1.3.5           Audio processing (Opus decoding)
libsodium-wrappers ^0.7.13   Voice encryption
openai ^4.28.0               Whisper STT + ChatGPT
dotenv ^16.4.1               .env file loading
```

---

## License

MIT
