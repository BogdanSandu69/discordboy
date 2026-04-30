const { Client, GatewayIntentBits, EmbedBuilder } = require('discord.js');
const {
  joinVoiceChannel,
  EndBehaviorType,
  VoiceConnectionStatus,
  getVoiceConnection,
} = require('@discordjs/voice');
const prism = require('prism-media');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { OpenAI } = require('openai');
require('dotenv').config();

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const RECORDING_DURATION = 10;     // default recording duration in seconds
const MAX_RECORDING_DURATION = 30; // maximum allowed recording duration
const MIN_RECORDING_DURATION = 3;  // minimum allowed recording duration
const MAX_HISTORY = 10;

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

function log(level, ...args) {
  const ts = new Date().toISOString();
  console[level](`${ts} [${level.toUpperCase()}]`, ...args);
}

// ---------------------------------------------------------------------------
// OpenAI client
// ---------------------------------------------------------------------------

const openai = new OpenAI({ apiKey: OPENAI_API_KEY });

// ---------------------------------------------------------------------------
// Discord client
// ---------------------------------------------------------------------------

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.GuildVoiceStates,
  ],
});

// ---------------------------------------------------------------------------
// State management
// ---------------------------------------------------------------------------

// guildId -> Set of guild IDs currently recording
const recordingInProgress = new Set();

// userId -> Array of { role, content }
const conversationHistory = new Map();

// ---------------------------------------------------------------------------
// System prompt
// ---------------------------------------------------------------------------

const SYSTEM_PROMPT = `You are a helpful voice assistant with the personality of a wise, \
experienced 50-year-old man with a deep, commanding voice.\n\nYour responses should be:\n- Thoughtful and measured, not rushed\n- Confident and authoritative, but friendly\n- Use mature vocabulary and complete sentences\n- Occasionally use phrases like "Well," "You see," "In my experience"\n- Keep responses concise but impactful\n- Sound like someone who has lived life and knows what they're talking about\n\nDon't mention your age or voice explicitly - just embody this personality naturally.`;

// ---------------------------------------------------------------------------
// Bot events
// ---------------------------------------------------------------------------

client.once('ready', () => {
  log('info', `Logged in as ${client.user.tag} (id: ${client.user.id})`);
  client.user.setActivity('!record', { type: 2 }); // 2 = LISTENING
});

// ---------------------------------------------------------------------------
// Message handler
// ---------------------------------------------------------------------------

client.on('messageCreate', async (message) => {
  if (message.author.bot) return;
  if (!message.content.startsWith('!')) return;

  const args = message.content.slice(1).trim().split(/ +/);
  const command = args.shift().toLowerCase();

  try {
    switch (command) {
      case 'join':
        await handleJoin(message);
        break;
      case 'record':
        await handleRecord(message, args);
        break;
      case 'leave':
        await handleLeave(message);
        break;
      case 'help':
        await handleHelp(message);
        break;
    }
  } catch (error) {
    log('error', `Error handling command \