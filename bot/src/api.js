// bot/src/api.js
import axios from 'axios'
import * as dotenv from 'dotenv'

dotenv.config()

const FASTAPI_URL = process.env.FASTAPI_URL || 'http://localhost:8000'


export async function queryRAG(message, sender = 'anonymous', history = [], quotedText = null) {
    try {
        const response = await axios.post(
            `${FASTAPI_URL}/query`,
            { message, sender, history, quoted_text: quotedText },
            { timeout: 30000 }
        )
        return {
            text:             response.data.response,
            route:            response.data.route,
            rewrittenMessage: response.data.rewritten_message  ?? null,
            englishMessage:   response.data.english_message    ?? null,
            englishResponse:  response.data.english_response   ?? null,
        }
    } catch (error) {
        if (error.code === 'ECONNREFUSED')
            return { text: 'Sorry, the backend server is unavailable.', route: 'error', rewrittenMessage: null, englishMessage: null, englishResponse: null }
        if (error.code === 'ECONNABORTED')
            return { text: 'Sorry, the request timed out. Please try again.', route: 'error', rewrittenMessage: null, englishMessage: null, englishResponse: null }
        console.error('queryRAG error:', error.message)
        return { text: 'Sorry, something went wrong. Please try again.', route: 'error', rewrittenMessage: null, englishMessage: null, englishResponse: null }
    }
}


export async function notifyEscalation(customerJid, question, customerMsgId = null) {
    try {
        const response = await axios.post(
            `${FASTAPI_URL}/escalate/notify`,
            { customer_jid: customerJid, question, customer_msg_id: customerMsgId },
            { timeout: 10000 }
        )
        return response.data
    } catch (error) {
        console.error('notifyEscalation error:', error.message)
        return null
    }
}


export async function setEscalationMessageId(escalationId, notificationMsgId) {
    try {
        await axios.post(
            `${FASTAPI_URL}/escalate/${escalationId}/message-id`,
            { notification_msg_id: notificationMsgId },
            { timeout: 5000 }
        )
    } catch (error) {
        console.error('setEscalationMessageId error:', error.message)
    }
}


export async function resolveEscalation(answer, notificationMsgId = null) {
    try {
        const response = await axios.post(
            `${FASTAPI_URL}/escalate/resolve`,
            { answer, notification_msg_id: notificationMsgId },
            { timeout: 15000 }
        )
        return response.data
    } catch (error) {
        if (error.response?.status === 404) return null
        console.error('resolveEscalation error:', error.message)
        return null
    }
}

// ── Phase 5: Outbox delivery API ──────────────────────────────────────────────
// Add these functions to bot/src/api.js

export async function getOutboxPending() {
    try {
        const response = await axios.get(
            `${FASTAPI_URL}/outbox/pending`,
            { timeout: 5000 }
        )
        return response.data.messages || []
    } catch (error) {
        // Silent failure — outbox polling should never crash the bot
        return []
    }
}

export async function markOutboxDelivered(messageId) {
    try {
        await axios.post(
            `${FASTAPI_URL}/outbox/${messageId}/delivered`,
            {},
            { timeout: 5000 }
        )
    } catch (error) {
        console.error('markOutboxDelivered error:', error.message)
    }
}

export async function markOutboxFailed(messageId) {
    try {
        await axios.post(
            `${FASTAPI_URL}/outbox/${messageId}/failed`,
            {},
            { timeout: 5000 }
        )
    } catch (error) {
        console.error('markOutboxFailed error:', error.message)
    }
}