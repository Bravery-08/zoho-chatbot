// bot/src/handler.js
import {
    queryRAG,
    notifyEscalation,
    setEscalationMessageId,
    resolveEscalation,
} from "./api.js";

const ESCALATION_JID = process.env.ESCALATION_JID || "";
console.log(
    `Escalation JID configured: ${
        ESCALATION_JID || "(not set — escalation disabled)"
    }`
);

const conversationHistory = new Map();
const MAX_HISTORY = 20;

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatHistoryForEscalation(history) {
    if (!history || history.length === 0) return null;
    return history
        .map(h => h.role === 'user'
            ? `*Customer:* ${h.content}`
            : `*Bot:* ${h.content}`
        )
        .join('\n\n');
}

function jidNumber(jid) {
    // Strips @s.whatsapp.net / @lid so comparisons work across JID formats
    return jid ? jid.split("@")[0] : "";
}

function extractText(message) {
    const msg = message.message;
    if (!msg) return null;
    if (msg.conversation) return msg.conversation;
    if (msg.extendedTextMessage?.text) return msg.extendedTextMessage.text;
    return null;
}

function extractQuotedMsgId(message) {
    // Returns the stanzaId of the message the human replied to, or null
    return message.message?.extendedTextMessage?.contextInfo?.stanzaId || null;
}

function extractQuotedText(message) {
    const quoted =
        message.message?.extendedTextMessage?.contextInfo?.quotedMessage;
    if (!quoted) return null;
    if (quoted.conversation) return quoted.conversation;
    if (quoted.extendedTextMessage?.text)
        return quoted.extendedTextMessage.text;
    return null;
}

function getHistory(sender) {
    if (!conversationHistory.has(sender)) conversationHistory.set(sender, []);
    return conversationHistory.get(sender);
}

function updateHistory(sender, userText, botReply) {
    const history = getHistory(sender);
    history.push({ role: "user", content: userText });
    history.push({ role: "assistant", content: botReply });
    if (history.length > MAX_HISTORY) {
        history.splice(0, history.length - MAX_HISTORY);
    }
}

function clearHistory(sender) {
    conversationHistory.delete(sender);
}

// ── Escalation reply handler ──────────────────────────────────────────────────

async function handleEscalationReply(sock, message, answerText) {
    console.log(`\n📬 Escalation reply: "${answerText.slice(0, 80)}..."`);

    // Extract the quoted message ID — this is how we identify which escalation
    // the human is answering. They must swipe-reply to the notification message.
    const quotedMsgId = extractQuotedMsgId(message);

    if (!quotedMsgId) {
        console.warn("   ⚠️  No quoted reply detected");
        await sock.sendMessage(ESCALATION_JID, {
            text: "⚠️ Please swipe right on the customer question you want to answer and reply to it. This ensures your answer goes to the right customer.",
        });
        return;
    }

    console.log(`   Quoted msg ID: ${quotedMsgId}`);
    const result = await resolveEscalation(answerText, quotedMsgId);

    if (!result) {
        console.error(
            "   ⚠️  No matching pending escalation found for this message"
        );
        await sock.sendMessage(ESCALATION_JID, {
            text: "⚠️ Could not match your reply to a pending question. It may have already been resolved or expired.",
        });
        return;
    }

    // Deliver the answer to the original customer
    const quotedMessage = {
        key: {
            remoteJid: result.customer_jid,
            fromMe: false,
            id: result.customer_msg_id,
        },
        message: {
            conversation: result.question,
        },
    };

    await sock.sendMessage(
        result.customer_jid,
        { text: result.answer },
        { quoted: quotedMessage }
    );
    console.log(`   ✅ Delivered to ${result.customer_jid}`);
    console.log(`   📝 KB chunks written: ${result.chunks_written}`);

    // Confirm to the human
    await sock.sendMessage(ESCALATION_JID, {
        text: `✅ Answer delivered to customer.\n📝 Added to knowledge base (${result.chunks_written} chunk(s) written).`,
    });
}

// ── Main message handler ──────────────────────────────────────────────────────

export async function handleMessage(sock, message) {
    if (message.key.fromMe) return;
    if (message.key.remoteJid === "status@broadcast") return;

    const text = extractText(message);
    if (!text || !text.trim()) return;

    const sender = message.key.remoteJid;
    const isGroup = sender.endsWith("@g.us");
    const senderName = message.pushName || "there";

    console.log(`\n📩 Message from ${senderName} (${sender})`);
    console.log(`   Text: ${text}`);

    if (isGroup) {
        console.log("   Skipping — group message");
        return;
    }

    // Route messages from the escalation number — must come before customer flow
    if (ESCALATION_JID && jidNumber(sender) === jidNumber(ESCALATION_JID)) {
        await handleEscalationReply(sock, message, text);
        return;
    }

    // Regular customer flow
    if (text.trim().toLowerCase() === "/reset") {
        clearHistory(sender);
        await sock.sendMessage(sender, {
            text: "Conversation history cleared. Starting fresh!",
        });
        return;
    }

    await sock.sendPresenceUpdate("composing", sender);

    try {
        const history = getHistory(sender);
        console.log(`   History: ${history.length} messages`);

        const quotedText = extractQuotedText(message);
        const {
            text: answer,
            route,
            rewrittenMessage,
            englishMessage,
            englishResponse,
        } = await queryRAG(text, sender, history, quotedText);
        console.log(`   Route: ${route}`);

        await sock.sendMessage(sender, { text: answer });
        updateHistory(sender, englishMessage || text, englishResponse || answer);
        console.log(`   Replied [${route}]: ${answer.slice(0, 80)}...`);

        if (route === "escalate" && ESCALATION_JID) {
            const customerMsgId = message.key.id;
            const questionToStore = rewrittenMessage || text; // prefer complete rewritten query

            const escalation = await notifyEscalation(
                sender,
                questionToStore,
                customerMsgId
            );

            if (escalation) {
                const historyBlock = formatHistoryForEscalation(history);
                const notificationText = [
                    `❓ *Unanswered customer query*`,
                    ``,
                    `*From:* ${sender}`,
                    `*Question:* ${questionToStore}`,
                    historyBlock ? `\n*Conversation history:*\n\n${historyBlock}` : null,
                    `\n_Swipe right on this message and reply with your answer._`
                ]
                    .filter(line => line !== null)
                    .join('\n');

                const sentMsg = await sock.sendMessage(
                    escalation.escalation_jid,
                    {
                        text: notificationText,
                    }
                );

                // Store the message ID so the human's quoted reply can be matched
                const notificationMsgId = sentMsg?.key?.id;
                if (notificationMsgId) {
                    await setEscalationMessageId(
                        escalation.escalation_id,
                        notificationMsgId
                    );
                    console.log(
                        `   📤 Forwarded to escalation number (msg_id=${notificationMsgId})`
                    );
                } else {
                    console.warn(
                        "   ⚠️  Could not capture notification message ID"
                    );
                }
            } else {
                console.error("   ⚠️  notifyEscalation failed");
            }
        }
    } catch (error) {
        console.error("   Failed to handle message:", error.message);
    } finally {
        await sock.sendPresenceUpdate("paused", sender);
    }
}
