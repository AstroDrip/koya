"""Short-lived claim for the channel currently responding to a CRM case."""

from .crm import CRMError


def claim(conn, case_id, channel, owner_key, now, until):
    row = conn.execute(
        """INSERT INTO crm_channel_claims(case_id,channel,owner_key,expires_at) VALUES (?,?,?,?)
           ON CONFLICT(case_id) DO UPDATE SET channel=excluded.channel,owner_key=excluded.owner_key,expires_at=excluded.expires_at
           WHERE crm_channel_claims.expires_at<=? OR crm_channel_claims.owner_key=excluded.owner_key
           RETURNING case_id""", (case_id, channel, owner_key, until, now),
    ).fetchone()
    if not row:
        raise CRMError(409, "Another channel is currently handling this case.")


def release(conn, case_id, owner_key):
    conn.execute("DELETE FROM crm_channel_claims WHERE case_id=? AND owner_key=?", (case_id, owner_key))
