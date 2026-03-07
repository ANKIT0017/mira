/**
 * MIRA WhatsApp Bridge
 * Uses Baileys to connect to WhatsApp Web as a companion device.
 *
 * Endpoints:
 *   GET  /status          → { status: 'qr'|'connected'|'disconnected', jid }
 *   GET  /qr              → { status, qr: <base64 data URL> | null }
 *   POST /send            → { to, message }  → sends text message
 *   POST /send-media      → { to, url, caption, type }  → sends image/video/doc
 *
 * Incoming messages are forwarded to MIRA at:
 *   POST http://localhost:5000/api/whatsapp/incoming
 *   Body: { from, jid, message, timestamp, is_group, group_name }
 */

import makeWASocket, {
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
    isJidBroadcast
} from '@whiskeysockets/baileys'
import { Boom } from '@hapi/boom'
import express from 'express'
import axios from 'axios'
import QRCode from 'qrcode'
import pino from 'pino'
import { existsSync, mkdirSync, readFileSync } from 'fs'
import { resolve, isAbsolute, extname, dirname, sep } from 'path'
import { fileURLToPath } from 'url'

// ── Config ────────────────────────────────────────────────────────────
const BRIDGE_PORT  = parseInt(process.env.BRIDGE_PORT  || '9001')
const MIRA_URL     = process.env.MIRA_URL     || 'http://localhost:5000'
const AUTH_DIR     = './auth_whatsapp'

// MIRA project root is one level up from whatsapp_bridge/
const __dirname = dirname(fileURLToPath(import.meta.url))
const MIRA_ROOT = resolve(__dirname, '..')
// Only files inside SANDBOX may be sent via WhatsApp (prevents file exfiltration)
const SANDBOX_DIR = resolve(MIRA_ROOT, 'SANDBOX')

if (!existsSync(AUTH_DIR)) mkdirSync(AUTH_DIR)

// ── Mime-type helper for local files ─────────────────────────────────
function _mimeForFile (filePath) {
    const ext = extname(filePath).toLowerCase()
    const map = {
        '.pdf':  'application/pdf',
        '.doc':  'application/msword',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.xls':  'application/vnd.ms-excel',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.ppt':  'application/vnd.ms-powerpoint',
        '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        '.txt':  'text/plain',
        '.zip':  'application/zip',
        '.mp4':  'video/mp4',
        '.mp3':  'audio/mpeg',
        '.jpg':  'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png':  'image/png',
        '.gif':  'image/gif',
        '.webp': 'image/webp',
    }
    return map[ext] || 'application/octet-stream'
}

// ── State ─────────────────────────────────────────────────────────────
let sock             = null
let currentQR        = null          // raw QR string from Baileys
let connectionStatus = 'disconnected' // 'qr' | 'connecting' | 'connected' | 'disconnected'
let connectedJID     = null

const logger = pino({ level: 'silent' })  // set 'debug' to see raw frames

// ── Baileys ───────────────────────────────────────────────────────────
async function startBaileys () {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
    const { version } = await fetchLatestBaileysVersion()

    console.log(`[WA] Using Baileys v${version.join('.')}`)
    connectionStatus = 'connecting'

    sock = makeWASocket({
        version,
        auth: {
            creds: state.creds,
            keys:  makeCacheableSignalKeyStore(state.keys, logger)
        },
        printQRInTerminal: true,   // also prints in terminal for convenience
        logger
    })

    // ── Persist credentials whenever they change ──────────────────────
    sock.ev.on('creds.update', saveCreds)

    // ── Connection lifecycle ──────────────────────────────────────────
    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update

        if (qr) {
            // New QR code arrived (rotates every ~20 s)
            currentQR        = qr
            connectionStatus = 'qr'
            console.log('[WA] QR code ready — open http://localhost:' + BRIDGE_PORT + '/qr in MIRA UI')
        }

        if (connection === 'open') {
            connectionStatus = 'connected'
            connectedJID     = sock.user?.id || null
            currentQR        = null
            console.log('[WA] ✓ Connected as', connectedJID)

            // Notify MIRA that we are connected
            _notifyMira('whatsapp_connected', { jid: connectedJID }).catch(() => {})
        }

        if (connection === 'close') {
            connectionStatus = 'disconnected'
            connectedJID     = null

            const statusCode = (lastDisconnect?.error instanceof Boom)
                ? lastDisconnect.error.output.statusCode
                : null

            const loggedOut = statusCode === DisconnectReason.loggedOut

            console.log('[WA] Connection closed. Logged out:', loggedOut, '| Code:', statusCode)

            if (!loggedOut) {
                console.log('[WA] Reconnecting in 4 s...')
                setTimeout(startBaileys, 4000)
            } else {
                console.log('[WA] Logged out. Delete auth_whatsapp/ to re-pair.')
                _notifyMira('whatsapp_disconnected', { reason: 'logged_out' }).catch(() => {})
            }
        }
    })

// ── Deduplication (Baileys can fire upsert multiple times for the same msg)
const _seenIds = new Set()
setTimeout(() => _seenIds.clear(), 60_000 * 10)  // clear every 10 min

// ── Trigger rules for group messages ─────────────────────────────────
// Bot responds in groups/self-chat ONLY when explicitly addressed.
// Supported triggers (case-insensitive):
//   @mira   /mira   /ai   hey mira
const GROUP_TRIGGERS = [/\@mira\b/i, /^\/mira\b/i, /^\/ai\b/i, /\bhey mira\b/i]

function _shouldRespond (text, needsTrigger, mentionedJids, myJid) {
    if (!needsTrigger) return true  // Always respond in private 1:1 chats
    // Groups / self-chat: only when bot is mentioned by JID or by text trigger
    const myNumber = (myJid || '').split(':')[0].split('@')[0]
    if (mentionedJids.some(j => j.includes(myNumber))) return true
    return GROUP_TRIGGERS.some(re => re.test(text))
}

function _stripTrigger (text) {
    // Remove the trigger prefix so the agent gets clean input
    return text
        .replace(/\@mira\b/gi, '')
        .replace(/^\/mira\b\s*/i, '')
        .replace(/^\/ai\b\s*/i, '')
        .replace(/\bhey mira[,!]?\s*/gi, '')
        .trim()
}

// ── Incoming messages ─────────────────────────────────────────────────
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return

        for (const msg of messages) {
            if (!msg.message)              continue
            if (isJidBroadcast(msg.key.remoteJid)) continue

            const jid      = msg.key.remoteJid
            const isGroup  = jid.endsWith('@g.us')

            // Self-chat: user messages their own number (saved messages / MIRA trigger)
            // fromMe=true for all self-chat messages — must allow them through.
            const myBareNum = (sock.user?.id || '').split(':')[0]
            const isSelfChat = !!myBareNum && jid === `${myBareNum}@s.whatsapp.net`

            // Skip own messages in regular chats/groups (not self-chat)
            if (msg.key.fromMe && !isSelfChat) continue

            // ── Deduplication ─────────────────────────────────────────
            const msgId = msg.key.id
            if (_seenIds.has(msgId)) continue
            _seenIds.add(msgId)

            const sender   = isGroup
                ? (msg.key.participant || '').split('@')[0]
                : jid.split('@')[0]

            // Extract text from common message types
            const rawText =
                msg.message?.conversation                                      ||
                msg.message?.extendedTextMessage?.text                         ||
                msg.message?.imageMessage?.caption                             ||
                msg.message?.videoMessage?.caption                             ||
                msg.message?.documentMessage?.caption                         ||
                msg.message?.buttonsResponseMessage?.selectedDisplayText       ||
                msg.message?.listResponseMessage?.title                        ||
                ''

            if (!rawText.trim()) continue

            // ── Trigger filter ────────────────────────────────────────
            // ALL chats (DMs, groups, self-chat) require @mira / /mira / hey mira.
            const needsTrigger = true
            const mentionedJids = msg.message?.extendedTextMessage?.contextInfo?.mentionedJid || []
            if (!_shouldRespond(rawText, needsTrigger, mentionedJids, sock.user?.id)) {
                console.log(`[WA] ⋯ skipped (no trigger) from ${sender}: ${rawText.substring(0, 60)}`)
                continue
            }

            // Strip the trigger prefix before sending to agent
            const text = _stripTrigger(rawText)
            if (!text) continue

            // Group name (best-effort)
            let group_name = null
            if (isGroup) {
                try {
                    const meta = await sock.groupMetadata(jid)
                    group_name = meta.subject
                } catch (_) {}
            }

            const chatLabel = isSelfChat ? 'self-chat' : isGroup ? (group_name || jid) : sender
            console.log(`[WA] ← ${chatLabel}: ${text.substring(0, 120)}`)

            // Forward to MIRA agent
            _notifyMira('whatsapp_message', {
                from:       sender,
                jid,
                message:    text,
                timestamp:  msg.messageTimestamp,
                is_group:   isGroup,
                is_self_chat: isSelfChat,
                group_name
            }).catch(err => console.error('[WA] Failed to forward to MIRA:', err.message))
        }
    })
}

// ── Helper: fire-and-forget POST to MIRA ─────────────────────────────
async function _notifyMira (event, data) {
    await axios.post(`${MIRA_URL}/api/whatsapp/incoming`, { event, ...data }, {
        timeout: 8000,
        headers: { 'Content-Type': 'application/json' }
    })
}

// ── Express REST API ──────────────────────────────────────────────────
const app = express()
app.use(express.json())

// GET /status
app.get('/status', (_req, res) => {
    res.json({ status: connectionStatus, jid: connectedJID })
})

// GET /qr  →  { status, qr: "data:image/png;base64,..." | null }
app.get('/qr', async (_req, res) => {
    if (connectionStatus === 'connected') {
        return res.json({ status: 'connected', qr: null, jid: connectedJID })
    }
    if (!currentQR) {
        return res.json({ status: connectionStatus, qr: null })
    }
    try {
        const png = await QRCode.toDataURL(currentQR, { scale: 6 })
        res.json({ status: 'qr', qr: png })
    } catch (e) {
        res.status(500).json({ error: e.message })
    }
})

// POST /send  →  { to: "919876543210", message: "Hello" }
app.post('/send', async (req, res) => {
    const { to, message } = req.body
    if (!to || !message)
        return res.status(400).json({ error: 'Missing to or message' })
    if (connectionStatus !== 'connected')
        return res.status(503).json({ error: 'Not connected to WhatsApp' })

    try {
        const jid = to.includes('@') ? to : `${to.replace(/[^0-9]/g, '')}@s.whatsapp.net`
        await sock.sendMessage(jid, { text: message })
        console.log(`[WA] → ${to}: ${message.substring(0, 80)}`)
        res.json({ status: 'sent', to })
    } catch (e) {
        res.status(500).json({ error: e.message })
    }
})

// POST /send-media  →  { to, url, caption, type: "image"|"video"|"document"|"audio" }
// url can be a public https:// URL  OR  a local path like "SANDBOX/file.pdf"
app.post('/send-media', async (req, res) => {
    const { to, url, caption = '', type = 'image' } = req.body
    if (!to || !url)
        return res.status(400).json({ error: 'Missing to or url' })
    if (connectionStatus !== 'connected')
        return res.status(503).json({ error: 'Not connected to WhatsApp' })

    try {
        const jid = to.includes('@') ? to : `${to.replace(/[^0-9]/g, '')}@s.whatsapp.net`
        const allowed = ['image', 'video', 'document', 'audio']
        if (!allowed.includes(type))
            return res.status(400).json({ error: `type must be one of: ${allowed.join(', ')}` })

        let msgPayload
        const isRemote = url.startsWith('http://') || url.startsWith('https://')

        if (isRemote) {
            // Remote URL — let Baileys download it
            msgPayload = { [type]: { url }, caption }
        } else {
            // Local file path — resolve relative to SANDBOX (never arbitrary FS paths)
            const absPath = isAbsolute(url) ? url : resolve(MIRA_ROOT, url)

            // Security: enforce that the resolved path is within SANDBOX
            if (!absPath.startsWith(SANDBOX_DIR + sep) && absPath !== SANDBOX_DIR) {
                return res.status(403).json({
                    error: `Security: file path must be inside SANDBOX. Got: ${absPath}`
                })
            }

            if (!existsSync(absPath))
                return res.status(404).json({ error: `File not found: ${absPath}` })

            const fileBuffer = readFileSync(absPath)
            const fileName   = absPath.split(/[\\/]/).pop()
            const mimetype   = _mimeForFile(absPath)

            if (type === 'document') {
                msgPayload = { document: fileBuffer, caption, fileName, mimetype }
            } else {
                msgPayload = { [type]: fileBuffer, caption, mimetype }
            }
        }

        await sock.sendMessage(jid, msgPayload)
        console.log(`[WA] → ${to} [${type}]: ${url.substring(0, 60)}`)
        res.json({ status: 'sent', to, type })
    } catch (e) {
        res.status(500).json({ error: e.message })
    }
})

// POST /logout  →  clears session
app.post('/logout', async (_req, res) => {
    try {
        if (sock) await sock.logout()
        res.json({ status: 'logged_out' })
    } catch (e) {
        res.status(500).json({ error: e.message })
    }
})

// ── Boot ──────────────────────────────────────────────────────────────
app.listen(BRIDGE_PORT, () => {
    console.log(`[WA BRIDGE] HTTP server listening on port ${BRIDGE_PORT}`)
    console.log(`[WA BRIDGE] Forwarding messages to MIRA at ${MIRA_URL}`)
    startBaileys()
})
