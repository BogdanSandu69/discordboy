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
    log('error', `Error handling command "${command}":`, error);
    await message.reply('❌ An error occurred. Please try again.');
  }
});

// ---------------------------------------------------------------------------
// Command: !join
// ---------------------------------------------------------------------------

async function handleJoin(message) {
  const voiceChannel = message.member?.voice?.channel;

  if (!voiceChannel) {
    return message.reply('❌ You must be in a voice channel first!');
  }

  const existing = getVoiceConnection(message.guildId);
  if (existing) {
    if (existing.joinConfig.channelId === voiceChannel.id) {
      return message.reply('✅ I\'m already in your voice channel! Use `!record` to start recording.');
    }
    // Move to new channel by destroying and reconnecting
    existing.destroy();
  }

  const connection = joinVoiceChannel({
    channelId: voiceChannel.id,
    guildId: message.guildId,
    adapterCreator: message.guild.voiceAdapterCreator,
    selfDeaf: false,
    selfMute: false,
  });

  connection.on(VoiceConnectionStatus.Ready, () => {
    log('info', `Connected to voice channel "${voiceChannel.name}" in guild "${message.guild.name}".`);
  });

  connection.on(VoiceConnectionStatus.Disconnected, () => {
    log('info', `Disconnected from voice channel in guild "${message.guild.name}".`);
  });

  await message.reply(
    `✅ Joined **${voiceChannel.name}**! Use \`!record\` to start recording.\nUse \`!leave\` to disconnect.`
  );

  // Greeting so you know the bot is alive and can speak
  await message.channel.send(
    `👋 Hey everyone! DiscordBoy here — your AI voice assistant. I'm all ears. Use \`!record\` whenever you're ready to talk to me!`
  );

  log('info', `Joined voice channel "${voiceChannel.name}" in guild "${message.guild.name}".`);
}

// ---------------------------------------------------------------------------
// Command: !record [seconds]
// ---------------------------------------------------------------------------

async function handleRecord(message, args) {
  const connection = getVoiceConnection(message.guildId);

  if (!connection) {
    return message.reply('❌ I\'m not in a voice channel! Use `!join` first.');
  }

  const voiceChannel = message.member?.voice?.channel;
  if (!voiceChannel) {
    return message.reply('❌ You must be in a voice channel!');
  }

  if (connection.joinConfig.channelId !== voiceChannel.id) {
    return message.reply('❌ You must be in the same voice channel as me!');
  }

  if (recordingInProgress.has(message.guildId)) {
    return message.reply('⏳ Already recording! Please wait for the current recording to finish.');
  }

  // Parse optional duration argument
  let duration = RECORDING_DURATION;
  if (args.length > 0) {
    const parsed = parseInt(args[0], 10);
    if (!isNaN(parsed)) {
      duration = Math.max(MIN_RECORDING_DURATION, Math.min(parsed, MAX_RECORDING_DURATION));
    }
  }

  // Gather all non-bot members currently in the voice channel
  const nonBotMembers = [...voiceChannel.members.values()].filter(m => !m.user.bot);
  if (nonBotMembers.length === 0) {
    return message.reply('❌ No non-bot users are in the voice channel!');
  }

  const memberIds = nonBotMembers.map(m => m.id);
  log('info', `[RECORD] Starting ${duration}s recording in guild "${message.guild.name}" ` +
    `(channel: "${voiceChannel.name}"). Subscribing to ${nonBotMembers.length} user(s): [${memberIds.join(', ')}]`);

  recordingInProgress.add(message.guildId);

  // Per-user PCM buffer: userId -> Buffer[]
  const userPcmChunks = new Map();
  const activeStreams = [];
  const receiver = connection.receiver;

  for (const member of nonBotMembers) {
    const userId = member.id;
    userPcmChunks.set(userId, []);

    try {
      const audioStream = receiver.subscribe(userId, {
        end: { behavior: EndBehaviorType.AfterSilence, duration: 500 },
      });

      // Decode Opus → raw PCM (48 kHz, 2-channel, 16-bit signed)
      const decoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
      const decodeStream = audioStream.pipe(decoder);

      decodeStream.on('data', (chunk) => {
        userPcmChunks.get(userId).push(chunk);
      });

      decodeStream.on('end', () => {
        const totalBytes = userPcmChunks.get(userId).reduce((s, c) => s + c.length, 0);
        log('info', `[RECORD] Stream ended for user ${userId} (${member.user.tag}) — buffered: ${totalBytes} bytes`);
      });

      decodeStream.on('error', (err) => {
        log('error', `[RECORD] Decoder error for user ${userId} (${member.user.tag}): ${err.message}`);
      });

      audioStream.on('error', (err) => {
        log('error', `[RECORD] Audio stream error for user ${userId} (${member.user.tag}): ${err.message}`);
      });

      activeStreams.push({ audioStream, decodeStream, userId });
      log('info', `[RECORD] Subscribed to user ${userId} (${member.user.tag})`);
    } catch (err) {
      log('error', `[RECORD] Failed to subscribe to user ${userId} (${member.user.tag}): ${err.message}`);
    }
  }

  try {
    await message.reply(
      `🎤 Recording for ${duration} seconds… speak now! (listening to ${nonBotMembers.length} user(s))`
    );

    // Wait for the full recording window — collect audio from all users simultaneously
    await new Promise((resolve) => setTimeout(resolve, duration * 1000));

    // Destroy all active streams after the window closes
    for (const { audioStream, decodeStream, userId } of activeStreams) {
      try { decodeStream.destroy(); } catch (err) {
        log('warn', `[RECORD] Error destroying decode stream for user ${userId}: ${err.message}`);
      }
      try { audioStream.destroy(); } catch (err) {
        log('warn', `[RECORD] Error destroying audio stream for user ${userId}: ${err.message}`);
      }
    }

    await message.channel.send('🔄 Processing audio…');

    // Log buffer stats for every subscribed user
    let anyAudio = false;
    for (const [userId, chunks] of userPcmChunks) {
      const totalBytes = chunks.reduce((s, c) => s + c.length, 0);
      log('info', `[RECORD] Buffer stats — user ${userId}: ${chunks.length} chunk(s), ${totalBytes} bytes`);
      if (totalBytes > 0) anyAudio = true;
    }

    if (!anyAudio) {
      log('info', `[RECORD] No audio data received from any user in guild "${message.guild.name}".`);
      await message.channel.send(
        '🔇 No audio detected from anyone. Make sure your mic isn\'t muted and you\'re speaking!'
      );
      return;
    }

    // Process each user who produced audio
    for (const [userId, chunks] of userPcmChunks) {
      const pcmData = Buffer.concat(chunks);
      if (pcmData.length === 0) {
        log('info', `[RECORD] Skipping user ${userId} — no audio buffered.`);
        continue;
      }

      const wavFile = path.join(os.tmpdir(), `audio_${Date.now()}_${userId}.wav`);
      try {
        const wavHeader = createWavHeader(pcmData.length);
        fs.writeFileSync(wavFile, Buffer.concat([wavHeader, pcmData]));
        log('info', `[RECORD] WAV written for user ${userId}: ${wavFile} (${pcmData.length} PCM bytes)`);

        // Transcribe with Whisper
        const transcription = await openai.audio.transcriptions.create({
          file: fs.createReadStream(wavFile),
          model: 'whisper-1',
        });

        const text = transcription.text.trim();

        if (!text) {
          log('info', `[RECORD] Whisper returned empty transcription for user ${userId}.`);
          await message.channel.send(`<@${userId}>: 🔇 No speech detected in your audio.`);
          continue;
        }

        log('info', `[RECORD] Transcription for user ${userId}: ${text}`);

        // Build conversation history
        let history = conversationHistory.get(userId) || [];
        history.push({ role: 'user', content: text });
        if (history.length > MAX_HISTORY * 2) {
          history = history.slice(-(MAX_HISTORY * 2));
        }

        const messages = [{ role: 'system', content: SYSTEM_PROMPT }, ...history];

        // Get ChatGPT response
        log('info', `[RECORD] Requesting ChatGPT response for user ${userId}…`);
        const completion = await openai.chat.completions.create({
          model: 'gpt-3.5-turbo',
          messages,
        });

        const reply = completion.choices[0].message.content.trim();
        history.push({ role: 'assistant', content: reply });
        conversationHistory.set(userId, history);

        await message.channel.send(`**<@${userId}> said:** ${text}\n**Bot:** ${reply}`);

      } catch (err) {
        log('error', `[RECORD] Error processing audio for user ${userId}: ${err.message}`);
        await message.channel.send(`<@${userId}>: ⚠️ Failed to process your audio. (${err.message})`);
      } finally {
        try { fs.unlinkSync(wavFile); } catch (e) {
          if (e.code !== 'ENOENT') log('error', `[RECORD] Failed to clean up temp file for user ${userId}: ${e.message}`);
        }
      }
    }

    await message.channel.send('✅ Done! Use `!record` again to ask another question.');

  } catch (error) {
    log('error', `[RECORD] Unexpected error in guild "${message.guild.name}":`, error);
    await message.channel.send('⚠️ An error occurred during recording. Please try again.');
  } finally {
    recordingInProgress.delete(message.guildId);
  }
}

// ---------------------------------------------------------------------------
// Command: !leave
// ---------------------------------------------------------------------------

async function handleLeave(message) {
  const connection = getVoiceConnection(message.guildId);

  if (!connection) {
    return message.reply('❌ I\'m not in a voice channel right now.');
  }

  recordingInProgress.delete(message.guildId);
  connection.destroy();

  await message.reply('👋 Left the voice channel. See you next time!');
  log('info', `Left voice channel in guild "${message.guild.name}".`);
}

// ---------------------------------------------------------------------------
// Command: !help
// ---------------------------------------------------------------------------

async function handleHelp(message) {
  const embed = new EmbedBuilder()
    .setColor('#5865F2')
    .setTitle('🤖 DiscordBoy – Voice AI Bot')
    .setDescription(
      'Use `!record` to ask me anything from your voice channel. ' +
      "I'll transcribe your speech and reply with ChatGPT!"
    )
    .addFields(
      { name: '`!join`', value: 'Join your current voice channel.', inline: false },
      {
        name: `\`!record [seconds]\``,
        value: `Record audio for the specified duration (default: ${RECORDING_DURATION}s, max: ${MAX_RECORDING_DURATION}s), then transcribe and respond.`,
        inline: false,
      },
      { name: '`!leave`', value: 'Leave the voice channel.', inline: false },
      { name: '`!help`', value: 'Show this help message.', inline: false },
      {
        name: '💡 Usage',
        value:
          '1. Join a voice channel and type `!join`\n' +
          '2. Type `!record` to start a 10-second recording\n' +
          '3. Speak your message during the recording window\n' +
          '4. The bot will transcribe your words and reply!\n' +
          '5. Use `!record` again to ask another question',
        inline: false,
      }
    )
    .setFooter({ text: 'Powered by OpenAI Whisper & ChatGPT' });

  await message.reply({ embeds: [embed] });
}

// ---------------------------------------------------------------------------
// Audio helpers
// ---------------------------------------------------------------------------

/**
 * Build a 44-byte WAV header for raw 48 kHz / stereo / 16-bit PCM data.
 * @param {number} dataLength  Length of the raw PCM data in bytes
 * @returns {Buffer}
 */
function createWavHeader(dataLength) {
  const header = Buffer.alloc(44);

  // RIFF chunk descriptor
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + dataLength, 4);
  header.write('WAVE', 8);

  // fmt sub-chunk
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);     // Subchunk1Size (16 for PCM)
  header.writeUInt16LE(1, 20);      // AudioFormat (1 = PCM)
  header.writeUInt16LE(2, 22);      // NumChannels (stereo)
  header.writeUInt32LE(48000, 24);  // SampleRate
  header.writeUInt32LE(48000 * 2 * 2, 28); // ByteRate
  header.writeUInt16LE(4, 32);      // BlockAlign (channels * bitsPerSample / 8)
  header.writeUInt16LE(16, 34);     // BitsPerSample

  // data sub-chunk
  header.write('data', 36);
  header.writeUInt32LE(dataLength, 40);

  return header;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

if (!DISCORD_TOKEN) {
  console.error('DISCORD_TOKEN is not set. Copy .env.example to .env and fill in your token.');
  process.exit(1);
}
if (!OPENAI_API_KEY) {
  console.error('OPENAI_API_KEY is not set. Copy .env.example to .env and fill in your API key.');
  process.exit(1);
}

client.login(DISCORD_TOKEN);