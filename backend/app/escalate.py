# backend/app/escalate.py
import sqlite3
import logging
import uuid
from datetime import datetime
from app.config import ESCALATION_DB_PATH

log = logging.getLogger(__name__)


def init_db():
    con = sqlite3.connect(ESCALATION_DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS escalations (
            id                  TEXT PRIMARY KEY,
            customer_jid        TEXT NOT NULL,
            question            TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            answer              TEXT,
            customer_msg_id     TEXT,
            notification_msg_id TEXT,
            created_at          DATETIME NOT NULL,
            resolved_at         DATETIME
        )
    """)
    for col in ("notification_msg_id", "customer_msg_id"):
        try:
            con.execute(f"ALTER TABLE escalations ADD COLUMN {col} TEXT")
            con.commit()
            log.info(f"  [escalate] migrated: added {col} column")
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()
    log.info(f"  [escalate] DB ready at {ESCALATION_DB_PATH}")


def create_escalation(customer_jid: str, question: str, customer_msg_id: str = None) -> str:
    eid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    con = sqlite3.connect(ESCALATION_DB_PATH)
    con.execute(
        "INSERT INTO escalations (id, customer_jid, question, status, customer_msg_id, created_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (eid, customer_jid, question, customer_msg_id, now)
    )
    con.commit()
    con.close()
    log.info(f"  [escalate] created id={eid} customer={customer_jid}")
    return eid


def set_notification_msg_id(escalation_id: str, msg_id: str):
    """
    Store the Baileys message ID of the notification sent to the human.
    Called after the bot successfully sends the WhatsApp notification.
    """
    con = sqlite3.connect(ESCALATION_DB_PATH)
    con.execute(
        "UPDATE escalations SET notification_msg_id = ? WHERE id = ?",
        (msg_id, escalation_id)
    )
    con.commit()
    con.close()
    log.info(
        f"  [escalate] stored notification_msg_id={msg_id} for id={escalation_id}")


def resolve_escalation(answer: str, notification_msg_id: str = None) -> dict | None:
    """
    Resolve an escalation.

    If notification_msg_id is provided: match by the exact message the human replied to.
    Fallback: resolve oldest pending (FIFO) — only used if quoted reply isn't detected.
    """
    now = datetime.utcnow().isoformat()
    con = sqlite3.connect(ESCALATION_DB_PATH)
    con.row_factory = sqlite3.Row

    if notification_msg_id:
        row = con.execute(
            "SELECT * FROM escalations WHERE notification_msg_id = ? AND status = 'pending'",
            (notification_msg_id,)
        ).fetchone()
        if not row:
            log.warning(
                f"  [escalate] no pending escalation found for msg_id={notification_msg_id}")
    else:
        row = None

    if not row:
        # Fallback to FIFO only if quoted reply matching found nothing
        row = con.execute(
            "SELECT * FROM escalations WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

    if not row:
        con.close()
        log.warning(
            "  [escalate] resolve called but no pending escalations found")
        return None

    con.execute(
        "UPDATE escalations SET status = 'resolved', answer = ?, resolved_at = ? WHERE id = ?",
        (answer, now, row["id"])
    )
    con.commit()
    con.close()
    log.info(
        f"  [escalate] resolved id={row['id']} customer={row['customer_jid']}")
    return dict(row)


def get_pending_count() -> int:
    con = sqlite3.connect(ESCALATION_DB_PATH)
    count = con.execute(
        "SELECT COUNT(*) FROM escalations WHERE status = 'pending'"
    ).fetchone()[0]
    con.close()
    return count
