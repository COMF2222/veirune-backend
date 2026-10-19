"""Shared discovery: unique listeners and an owner-appointed editorial team."""
import json
import re
import time
from datetime import datetime, timedelta, timezone

MAX_PICKS = 30
MAX_PER_EDITOR = 10


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS community_editors(user_id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS community_picks(user_id INTEGER NOT NULL,track_id INTEGER NOT NULL,added INTEGER NOT NULL,PRIMARY KEY(user_id,track_id))")
    db.execute("CREATE TABLE IF NOT EXISTS community_tracks(track_id INTEGER PRIMARY KEY,value TEXT NOT NULL,checked INTEGER NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS listening_discovery ON listening(day,track_id,user_id)")


def track_id(value):
    if type(value) is not int or not 0 < value < 2**53:
        raise ValueError("Invalid track")
    return value


def can_edit(db, user_id, owner):
    return user_id == owner or db.execute("SELECT 1 FROM community_editors e JOIN users u ON u.id=e.user_id WHERE e.user_id=? AND u.status='approved'", (user_id,)).fetchone() is not None


def role(db, user_id, owner):
    return {"canEdit": can_edit(db, user_id, owner), "isOwner": user_id == owner,
            "pickedIds": [row[0] for row in db.execute("SELECT track_id FROM community_picks WHERE user_id=? ORDER BY added DESC,track_id", (user_id,))]}


def set_editor(db, user_id, enabled, owner):
    track_id(user_id)
    if type(enabled) is not bool or user_id == owner:
        raise ValueError("Invalid editor role")
    user = db.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or enabled and user[0] != "approved":
        raise ValueError("Approve this user first")
    if enabled:
        db.execute("INSERT OR IGNORE INTO community_editors VALUES (?)", (user_id,))
    else:
        db.execute("DELETE FROM community_editors WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM community_picks WHERE user_id=?", (user_id,))


def public_track(raw):
    if not isinstance(raw, dict) or raw.get("sharing") != "public":
        raise ValueError("Choose a public SoundCloud track")
    source_id = raw.get("id")
    if source_id is None:
        match = re.fullmatch(r"soundcloud:tracks:([1-9][0-9]*)", str(raw.get("urn", "")))
        source_id = int(match[1]) if match else None
    tid = track_id(source_id)
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 512:
        raise ValueError("Invalid track title")
    # Never persist signed stream URLs, credentials or private track fields.
    result = {"id": tid, "title": title, "urn": f"soundcloud:tracks:{tid}"}
    for key in ("duration", "full_duration_ms", "playback_count"):
        value = raw.get(key)
        if type(value) in (int, float) and 0 <= value <= 2**53-1:
            result[key] = value
    if type(raw.get("streamable")) is bool: result["streamable"] = raw["streamable"]
    for key in ("genre", "metadata_artist", "access", "policy", "monetization_model"):
        if isinstance(raw.get(key), str): result[key] = raw[key][:256]
    for key in ("artwork_url", "permalink_url"):
        value = raw.get(key)
        if isinstance(value, str) and value.startswith("https://") and len(value) <= 2048 and "?" not in value:
            result[key] = value
    user = raw.get("user")
    if isinstance(user, dict) and isinstance(user.get("username"), str):
        uid = user.get("id")
        if uid is None:
            match = re.fullmatch(r"soundcloud:users:([1-9][0-9]*)", str(user.get("urn", "")))
            uid = int(match[1]) if match else None
        if type(uid) is int and 0 < uid < 2**53:
            result["user"] = {"id": uid, "username": user["username"][:256]}
    metadata = raw.get("publisher_metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("artist"), str):
        result["publisher_metadata"] = {"artist": metadata["artist"][:256]}
    return result


def save_track(db, raw, now=None):
    value = public_track(raw)
    stamp = int(time.time() if now is None else now)
    db.execute("DELETE FROM community_tracks WHERE checked<? AND track_id NOT IN (SELECT track_id FROM community_picks)", (stamp-7*86400,))
    db.execute("INSERT INTO community_tracks VALUES(?,?,?) ON CONFLICT(track_id) DO UPDATE SET value=excluded.value,checked=excluded.checked",
               (value["id"], json.dumps(value, ensure_ascii=False), stamp))
    return value


def pick(db, user_id, owner, tid, active):
    track_id(tid)
    if type(active) is not bool: raise ValueError("Invalid pick")
    if not can_edit(db, user_id, owner): raise PermissionError("Editor role required")
    if not active:
        if user_id == owner: db.execute("DELETE FROM community_picks WHERE track_id=?", (tid,))
        else: db.execute("DELETE FROM community_picks WHERE user_id=? AND track_id=?", (user_id, tid))
        return
    if db.execute("SELECT 1 FROM community_picks WHERE user_id=? AND track_id=?", (user_id, tid)).fetchone(): return
    if not db.execute("SELECT 1 FROM community_tracks WHERE track_id=?", (tid,)).fetchone(): raise ValueError("Track is unavailable")
    if db.execute("SELECT COUNT(*) FROM community_picks WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_PER_EDITOR:
        raise ValueError("You can choose up to 10 tracks. Remove a pick first.")
    active_ids = {row[0] for row in db.execute("SELECT DISTINCT p.track_id FROM community_picks p JOIN users u ON u.id=p.user_id WHERE u.status='approved' AND (p.user_id=? OR EXISTS(SELECT 1 FROM community_editors e WHERE e.user_id=p.user_id))", (owner,))}
    if tid not in active_ids and len(active_ids) >= MAX_PICKS:
        raise ValueError("The collection holds 30 tracks. Remove a pick first.")
    db.execute("INSERT INTO community_picks VALUES(?,?,?)", (user_id, tid, int(time.time())))


def ranking(db, start, end, limit=5):
    # Deduplicate the account across devices and days before counting listeners.
    rows = db.execute("""SELECT track_id,COUNT(*) FROM (
        SELECT l.track_id,l.user_id FROM listening l JOIN users u ON u.id=l.user_id
        WHERE l.day>=? AND l.day<=? AND u.status='approved'
        GROUP BY l.track_id,l.user_id HAVING SUM(l.ms)>=30000 AND SUM(l.plays)>=1
        ) GROUP BY track_id HAVING COUNT(*)>=2 ORDER BY COUNT(*) DESC,track_id LIMIT ?""", (start, end, limit))
    return [{"trackId": tid, "listeners": count} for tid, count in rows]


def snapshot(db, viewer, owner, now=None):
    now = int(time.time() if now is None else now)
    today = datetime.fromtimestamp(now, timezone.utc).date()
    yesterday = (today-timedelta(days=1)).isoformat()
    picks = db.execute("""SELECT p.track_id,COUNT(*),MAX(p.added),MAX(p.user_id=?)
        FROM community_picks p JOIN users u ON u.id=p.user_id
        WHERE u.status='approved' AND (p.user_id=? OR EXISTS(SELECT 1 FROM community_editors e WHERE e.user_id=p.user_id))
        GROUP BY p.track_id ORDER BY COUNT(*) DESC,MAX(p.added) DESC,p.track_id LIMIT ?""", (viewer, owner, MAX_PICKS))
    return {"today": ranking(db, today.isoformat(), today.isoformat()), "yesterday": ranking(db, yesterday, yesterday),
            "week": ranking(db, (today-timedelta(days=6)).isoformat(), today.isoformat()),
            "editorial": [{"trackId": tid, "votes": votes, "pickedByMe": bool(mine)} for tid, votes, _, mine in picks],
            "role": role(db, viewer, owner), "day": today.isoformat(), "timezone": "UTC", "updatedAt": now}


def cached(db, ids, now=None):
    now = int(time.time() if now is None else now)
    result = {}
    for tid in ids:
        row = db.execute("SELECT value,checked FROM community_tracks WHERE track_id=?", (tid,)).fetchone()
        if row and now-row[1] < 300: result[tid] = json.loads(row[0])
    return result


def present(snapshot, tracks):
    return {**snapshot, **{key: [{**row, "track": tracks[row["trackId"]]} for row in snapshot[key] if row["trackId"] in tracks]
                           for key in ("today", "yesterday", "week", "editorial")}}
