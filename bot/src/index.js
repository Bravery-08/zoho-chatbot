// bot/src/index.js
import makeWASocket, {
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
} from '@whiskeysockets/baileys'

import { Boom } from '@hapi/boom'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import * as dotenv from 'dotenv'
import { handleMessage } from './handler.js'

dotenv.config()

// Suppress verbose Baileys internal logs
const logger = pino({ level: 'silent' })

async function connectToWhatsApp() {
    // Load or create session
    const { state, saveCreds } = await useMultiFileAuthState('./auth_info')

    // Always use the latest Baileys protocol version
    const { version } = await fetchLatestBaileysVersion()
    console.log(`Using Baileys version: ${version.join('.')}`)

    const sock = makeWASocket({
        version,
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        logger,
        printQRInTerminal: false,   // we handle QR ourselves below
        generateHighQualityLinkPreview: false,
        syncFullHistory: false,     // faster startup
    })

    // ── QR Code ─────────────────────────────────────────────────
    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update

        if (qr) {
            console.clear()
            console.log('Scan this QR code with WhatsApp on your phone:')
            console.log('(WhatsApp → Linked Devices → Link a Device)\n')
            qrcode.generate(qr, { small: true })
        }

        if (connection === 'close') {
            const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut

            console.log(`Connection closed. Status: ${statusCode}`)

            if (shouldReconnect) {
                console.log('Reconnecting...')
                connectToWhatsApp()       // auto-reconnect
            } else {
                console.log('Logged out. Delete auth_info/ and restart to re-scan QR.')
            }
        }

        if (connection === 'open') {
            console.log('\n✅ WhatsApp connected successfully!')
            console.log('Bot is ready to receive messages.\n')
        }
    })

    // ── Save session credentials when updated ───────────────────
    sock.ev.on('creds.update', saveCreds)

    // ── Incoming messages ────────────────────────────────────────
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return     // only process new incoming messages

        for (const message of messages) {
            await handleMessage(sock, message)
        }
    })
}

// Start
console.log('Starting WhatsApp RAG Bot...')
connectToWhatsApp().catch(console.error)