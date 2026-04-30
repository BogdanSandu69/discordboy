# Deploying Discord Bot to Render.com

This guide will help you deploy your Discord voice-to-text AI bot to Render.com.

## Prerequisites

Before deploying, you need:
1. ✅ A Render.com account (free tier works!)
2. ✅ Discord Bot Token
3. ✅ OpenAI API Key

---

## 🔑 Step 1: Get Your API Keys

### Discord Bot Token

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click your application (or create a new one)
3. Go to **Bot** section in the left sidebar
4. Click **Reset Token** and copy it (keep it secret!)
5. Enable these **Privileged Gateway Intents**:
   - ☑️ Presence Intent
   - ☑️ Server Members Intent
   - ☑️ Message Content Intent

### Bot Invite Link

1. In Discord Developer Portal, go to **OAuth2** → **URL Generator**
2. Select scopes:
   - ☑️ `bot`
   - ☑️ `applications.commands`
3. Select bot permissions:
   - ☑️ Send Messages
   - ☑️ Read Message History
   - ☑️ Connect (voice)
   - ☑️ Speak (voice)
   - ☑️ Use Voice Activity
4. Copy the generated URL and open it to invite the bot to your server

### OpenAI API Key

1. Go to [OpenAI Platform](https://platform.openai.com/api-keys)
2. Click **+ Create new secret key**
3. Copy the key (you won't see it again!)
4. Make sure you have credits/billing set up

---

## 🚀 Step 2: Deploy to Render

### Option A: One-Click Deploy (Easiest!)

1. Go to [Render.com](https://render.com)
2. Click **New +** → **Web Service**
3. Connect your GitHub and select `BogdanSandu69/discordboy`
4. Render auto-detects `render.yaml` configuration
5. Add environment variables:
   - `DISCORD_TOKEN`: Your Discord bot token
   - `OPENAI_API_KEY`: Your OpenAI API key
6. Click **Create Web Service** and wait 2-5 minutes

### Option B: Manual Setup

1. **Login to Render**
   - Go to [Render.com](https://render.com)
   - Sign in or create an account

2. **Create New Web Service**
   - Click **New +** → **Web Service**
   - Connect your GitHub account
   - Select the `BogdanSandu69/discordboy` repository

3. **Configure Service**
   ```
   Name: discordboy
   Runtime: Python 3
   Branch: main
   Build Command: pip install -r requirements.txt
   Start Command: python bot.py
   ```

4. **Set Environment Variables**
   - Click **Advanced** → **Add Environment Variable**
   - Add:
     ```
     DISCORD_TOKEN = your_discord_bot_token_here
     OPENAI_API_KEY = your_openai_api_key_here
     ```

5. **Choose Plan**
   - **Free Plan**: Bot may sleep after 15 min inactivity
   - **Starter Plan ($7/month)**: Always on

6. **Deploy!**
   - Click **Create Web Service**
   - Check logs for "Bot connected!" message

---

## 🔄 Step 3: Keep Free Tier Awake (Optional)

If using the **free plan**, keep your bot awake using UptimeRobot:

1. Go to [UptimeRobot.com](https://uptimerobot.com) and sign up (free)
2. Click **Add New Monitor**
3. Configure:
   ```
   Monitor Type: HTTP(s)
   Friendly Name: Discord Bot
   URL: Your Render service URL
   Monitoring Interval: 5 minutes
   ```
4. Save and your bot stays awake! 🎉

---

## 📊 Step 4: Monitor Your Bot

### View Logs
- Render dashboard → Your service → **Logs** tab
- See bot connection status, transcriptions, AI responses

### Check Status
Look for: `Bot connected as YourBotName#1234`

---

## 🎮 Step 5: Use Your Bot!

In Discord:
1. Join a voice channel
2. Type: `!join`
3. Start speaking!
4. Type: `!leave` when done

---

## 💰 Cost Estimates

### Render Hosting
- **Free**: $0 (with UptimeRobot)
- **Starter**: $7/month (always on)

### OpenAI API
- **Whisper**: $0.006/minute of audio
- **GPT-3.5**: ~$0.002/conversation

**Monthly estimate:**
- Light usage: $5-10/month
- Heavy usage: $38-45/month

---

## 🛠️ Troubleshooting

**Bot Not Responding:**
- Check Render logs
- Verify environment variables
- Check Discord permissions

**Voice Not Working:**
- Ensure Connect/Speak permissions
- Verify PyNaCl and ffmpeg installed

**OpenAI Errors:**
- Check API key
- Verify billing/credits

---

## 🔒 Security

- ✅ Never commit API keys
- ✅ Use environment variables
- ✅ Regenerate if exposed
- ✅ Monitor usage

---

**Your bot is now live! 🎉**

Join voice, type `!join`, and talk to your AI bot!