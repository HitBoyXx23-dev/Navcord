import asyncio, os, time, json, sqlite3, secrets, hashlib
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import bcrypt

APP_NAME = "Navcord"
DB_PATH = os.environ.get("NAVCORD_DB", "navcord.db")
FILES_DIR = os.environ.get("NAVCORD_FILES_DIR", "/tmp/navcord_files")
MAX_FILE_BYTES = int(os.environ.get("NAVCORD_MAX_FILE_BYTES", str(25 * 1024 * 1024)))

os.makedirs(FILES_DIR, exist_ok=True)

def now_ms() -> int:
    return int(time.time() * 1000)

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        passhash BLOB NOT NULL,
        avatar_file TEXT,
        created_ms INTEGER NOT NULL
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_ms INTEGER NOT NULL,
        last_ms INTEGER NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS guilds(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_ms INTEGER NOT NULL
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS memberships(
        user_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        PRIMARY KEY(user_id, guild_id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(guild_id) REFERENCES guilds(id)
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS channels(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        created_ms INTEGER NOT NULL,
        UNIQUE(guild_id, name, type),
        FOREIGN KEY(guild_id) REFERENCES guilds(id)
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS dms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user1 INTEGER NOT NULL,
        user2 INTEGER NOT NULL,
        created_ms INTEGER NOT NULL,
        UNIQUE(user1, user2),
        FOREIGN KEY(user1) REFERENCES users(id),
        FOREIGN KEY(user2) REFERENCES users(id)
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        channel_id INTEGER,
        dm_id INTEGER,
        sender_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        attachments_json TEXT,
        created_ms INTEGER NOT NULL,
        FOREIGN KEY(channel_id) REFERENCES channels(id),
        FOREIGN KEY(dm_id) REFERENCES dms(id),
        FOREIGN KEY(sender_id) REFERENCES users(id)
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS reactions(
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        created_ms INTEGER NOT NULL,
        PRIMARY KEY(message_id, user_id, emoji),
        FOREIGN KEY(message_id) REFERENCES messages(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );""")
    conn.commit()
    conn.close()

def hash_password(pw: str) -> bytes:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12))

def verify_password(pw: str, ph: bytes) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), ph)
    except Exception:
        return False

def create_token() -> str:
    return secrets.token_urlsafe(32)

def require_user(authorization: Optional[str]) -> sqlite3.Row:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    tok = authorization.split(" ", 1)[1].strip()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM sessions WHERE token=?", (tok,))
    r = cur.fetchone()
    if not r:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid token")
    cur.execute("UPDATE sessions SET last_ms=? WHERE token=?", (now_ms(), tok))
    conn.commit()
    cur.execute("SELECT id, username, avatar_file, created_ms FROM users WHERE id=?", (r["user_id"],))
    u = cur.fetchone()
    conn.close()
    if not u:
        raise HTTPException(status_code=401, detail="Invalid user")
    return u

def ensure_default_guild_and_channels(user_id: int) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM guilds ORDER BY id ASC LIMIT 1")
    g = cur.fetchone()
    if not g:
        cur.execute("INSERT INTO guilds(name, created_ms) VALUES(?, ?)", ("Navine Server", now_ms()))
        gid = cur.lastrowid
        cur.execute("INSERT INTO channels(guild_id, name, type, created_ms) VALUES(?,?,?,?)", (gid, "general", "text", now_ms()))
        cur.execute("INSERT INTO channels(guild_id, name, type, created_ms) VALUES(?,?,?,?)", (gid, "memes", "text", now_ms()))
        cur.execute("INSERT INTO channels(guild_id, name, type, created_ms) VALUES(?,?,?,?)", (gid, "Lobby", "voice", now_ms()))
        cur.execute("INSERT INTO channels(guild_id, name, type, created_ms) VALUES(?,?,?,?)", (gid, "Chill", "voice", now_ms()))
        cur.execute("INSERT INTO memberships(user_id, guild_id, role) VALUES(?,?,?)", (user_id, gid, "admin"))
        conn.commit()
        conn.close()
        return
    gid = g["id"]
    cur.execute("SELECT 1 FROM memberships WHERE user_id=? AND guild_id=?", (user_id, gid))
    m = cur.fetchone()
    if not m:
        cur.execute("INSERT OR IGNORE INTO memberships(user_id, guild_id, role) VALUES(?,?,?)", (user_id, gid, "member"))
        conn.commit()
    conn.close()

def safe_name(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s or "file")
    s = s.strip("._")
    return s[:180] if s else "file"

def store_upload(raw: bytes, filename: str) -> str:
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    fid = secrets.token_hex(16)
    fn = safe_name(filename)
    path = os.path.join(FILES_DIR, f"{fid}_{fn}")
    with open(path, "wb") as f:
        f.write(raw)
    return os.path.basename(path)

def file_path(file_id: str) -> str:
    p = os.path.join(FILES_DIR, file_id)
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="File not found")
    return p

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

init_db()

class Hub:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.ws_by_user: Dict[int, Set[WebSocket]] = {}
        self.subscriptions: Dict[str, Set[WebSocket]] = {}
        self.user_active_voice: Dict[int, int] = {}

    async def add_ws(self, user_id: int, ws: WebSocket) -> None:
        async with self.lock:
            self.ws_by_user.setdefault(user_id, set()).add(ws)

    async def remove_ws(self, user_id: int, ws: WebSocket) -> None:
        async with self.lock:
            if user_id in self.ws_by_user and ws in self.ws_by_user[user_id]:
                self.ws_by_user[user_id].remove(ws)
                if not self.ws_by_user[user_id]:
                    del self.ws_by_user[user_id]
            for k in list(self.subscriptions.keys()):
                if ws in self.subscriptions[k]:
                    self.subscriptions[k].remove(ws)
                    if not self.subscriptions[k]:
                        del self.subscriptions[k]
            if user_id in self.user_active_voice:
                del self.user_active_voice[user_id]

    async def subscribe(self, scope: str, ws: WebSocket) -> None:
        async with self.lock:
            self.subscriptions.setdefault(scope, set()).add(ws)

    async def unsubscribe_all(self, ws: WebSocket) -> None:
        async with self.lock:
            for k in list(self.subscriptions.keys()):
                if ws in self.subscriptions[k]:
                    self.subscriptions[k].remove(ws)
                    if not self.subscriptions[k]:
                        del self.subscriptions[k]

    async def broadcast(self, scope: str, payload: Dict[str, Any]) -> None:
        async with self.lock:
            conns = list(self.subscriptions.get(scope, set()))
        dead = []
        for w in conns:
            try:
                await w.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:
                dead.append(w)
        if dead:
            async with self.lock:
                if scope in self.subscriptions:
                    for w in dead:
                        self.subscriptions[scope].discard(w)

    async def send_user(self, user_id: int, payload: Dict[str, Any]) -> None:
        async with self.lock:
            conns = list(self.ws_by_user.get(user_id, set()))
        dead = []
        for w in conns:
            try:
                await w.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:
                dead.append(w)
        if dead:
            async with self.lock:
                if user_id in self.ws_by_user:
                    for w in dead:
                        self.ws_by_user[user_id].discard(w)
                    if not self.ws_by_user[user_id]:
                        del self.ws_by_user[user_id]

hub = Hub()

def list_guilds_for_user(user_id: int) -> List[Dict[str, Any]]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""SELECT g.id, g.name, m.role FROM guilds g
                   JOIN memberships m ON m.guild_id=g.id
                   WHERE m.user_id=?
                   ORDER BY g.id ASC""", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "role": r["role"]} for r in rows]

def list_channels_for_guild(guild_id: int) -> Dict[str, List[Dict[str, Any]]]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""SELECT id, name, type FROM channels WHERE guild_id=? ORDER BY type ASC, name ASC""", (guild_id,))
    rows = cur.fetchall()
    conn.close()
    out = {"text": [], "voice": []}
    for r in rows:
        out[r["type"]].append({"id": r["id"], "name": r["name"], "type": r["type"]})
    return out

def user_basic(user_id: int) -> Dict[str, Any]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, avatar_file FROM users WHERE id=?", (user_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return {"id": user_id, "username": "Unknown", "avatar_url": None}
    av = r["avatar_file"]
    return {"id": r["id"], "username": r["username"], "avatar_url": (f"/files/{av}" if av else None)}

def online_user_ids() -> Set[int]:
    return set(hub.ws_by_user.keys())

def require_membership(user_id: int, guild_id: int) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM memberships WHERE user_id=? AND guild_id=?", (user_id, guild_id))
    r = cur.fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=403, detail="Not a member")

@app.get("/health")
async def health():
    return {"ok": True, "name": APP_NAME, "ts": now_ms()}

@app.post("/auth/register")
async def register(username: str = Form(...), password: str = Form(...)):
    un = username.strip()
    if not (3 <= len(un) <= 24):
        raise HTTPException(status_code=400, detail="Username must be 3-24 chars")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password too short")
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    count = cur.fetchone()["c"]
    role = "admin" if count == 0 else "member"
    try:
        cur.execute("INSERT INTO users(username, passhash, avatar_file, created_ms) VALUES(?,?,?,?)",
                    (un, hash_password(password), None, now_ms()))
        uid = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="Username already taken")
    conn.close()
    ensure_default_guild_and_channels(uid)
    return {"ok": True, "role": role}

@app.post("/auth/login")
async def login(username: str = Form(...), password: str = Form(...)):
    un = username.strip()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, passhash FROM users WHERE username=?", (un,))
    r = cur.fetchone()
    if not r or not verify_password(password, r["passhash"]):
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid credentials")
    tok = create_token()
    cur.execute("INSERT INTO sessions(token, user_id, created_ms, last_ms) VALUES(?,?,?,?)", (tok, r["id"], now_ms(), now_ms()))
    conn.commit()
    conn.close()
    ensure_default_guild_and_channels(r["id"])
    return {"ok": True, "token": tok, "user": user_basic(r["id"])}

@app.post("/auth/logout")
async def logout(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        return {"ok": True}
    tok = authorization.split(" ", 1)[1].strip()
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE token=?", (tok,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/me")
async def me(authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    return {"ok": True, "user": user_basic(u["id"])}

@app.post("/me/avatar")
async def set_avatar(file: UploadFile = File(...), authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    raw = await file.read()
    stored = store_upload(raw, file.filename or "avatar.png")
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET avatar_file=? WHERE id=?", (stored, u["id"]))
    conn.commit()
    conn.close()
    await hub.send_user(u["id"], {"t": "me_update", "user": user_basic(u["id"])})
    return {"ok": True, "avatar_url": f"/files/{stored}"}

@app.get("/guilds")
async def guilds(authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    return {"ok": True, "guilds": list_guilds_for_user(u["id"])}

@app.get("/guilds/{guild_id}/channels")
async def guild_channels(guild_id: int, authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    require_membership(u["id"], guild_id)
    return {"ok": True, "channels": list_channels_for_guild(guild_id)}

@app.post("/channels/create")
async def create_channel(guild_id: int = Form(...), name: str = Form(...), type: str = Form(...), authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    require_membership(u["id"], int(guild_id))
    nm = name.strip().lower().replace(" ", "-")
    if not nm:
        raise HTTPException(status_code=400, detail="Bad name")
    tp = type.strip().lower()
    if tp not in ("text", "voice"):
        raise HTTPException(status_code=400, detail="Bad type")
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO channels(guild_id, name, type, created_ms) VALUES(?,?,?,?)", (int(guild_id), nm, tp, now_ms()))
        cid = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="Channel exists")
    conn.close()
    await hub.broadcast(f"guild:{int(guild_id)}", {"t": "channel_created", "channel": {"id": cid, "guild_id": int(guild_id), "name": nm, "type": tp}})
    return {"ok": True, "channel_id": cid}

@app.post("/dms/open")
async def dm_open(username: str = Form(...), authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    target = username.strip()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (target,))
    tr = cur.fetchone()
    if not tr:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    uid1 = int(u["id"])
    uid2 = int(tr["id"])
    a, b = (uid1, uid2) if uid1 < uid2 else (uid2, uid1)
    cur.execute("SELECT id FROM dms WHERE user1=? AND user2=?", (a, b))
    dm = cur.fetchone()
    if not dm:
        cur.execute("INSERT INTO dms(user1, user2, created_ms) VALUES(?,?,?)", (a, b, now_ms()))
        dm_id = cur.lastrowid
        conn.commit()
    else:
        dm_id = dm["id"]
    conn.close()
    return {"ok": True, "dm_id": dm_id, "peer": user_basic(uid2)}

@app.get("/messages/channel/{channel_id}")
async def channel_messages(channel_id: int, limit: int = 50, before_id: int = 0, authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT guild_id FROM channels WHERE id=?", (channel_id,))
    ch = cur.fetchone()
    if not ch:
        conn.close()
        raise HTTPException(status_code=404, detail="Channel not found")
    require_membership(u["id"], ch["guild_id"])
    if before_id and before_id > 0:
        cur.execute("""SELECT * FROM messages WHERE scope='channel' AND channel_id=? AND id<? ORDER BY id DESC LIMIT ?""", (channel_id, before_id, int(limit)))
    else:
        cur.execute("""SELECT * FROM messages WHERE scope='channel' AND channel_id=? ORDER BY id DESC LIMIT ?""", (channel_id, int(limit)))
    rows = cur.fetchall()
    mids = [r["id"] for r in rows]
    rx = {}
    if mids:
        q = ",".join("?" for _ in mids)
        cur.execute(f"SELECT message_id, emoji, COUNT(*) AS c FROM reactions WHERE message_id IN ({q}) GROUP BY message_id, emoji", mids)
        for r in cur.fetchall():
            rx.setdefault(r["message_id"], []).append({"emoji": r["emoji"], "count": r["c"]})
    out = []
    for r in reversed(rows):
        out.append({
            "id": r["id"],
            "scope": r["scope"],
            "channel_id": r["channel_id"],
            "dm_id": r["dm_id"],
            "sender": user_basic(r["sender_id"]),
            "content": r["content"],
            "attachments": json.loads(r["attachments_json"] or "[]"),
            "created_ms": r["created_ms"],
            "reactions": rx.get(r["id"], [])
        })
    conn.close()
    return {"ok": True, "messages": out}

@app.get("/messages/dm/{dm_id}")
async def dm_messages(dm_id: int, limit: int = 50, before_id: int = 0, authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user1, user2 FROM dms WHERE id=?", (dm_id,))
    dm = cur.fetchone()
    if not dm:
        conn.close()
        raise HTTPException(status_code=404, detail="DM not found")
    uid = int(u["id"])
    if uid not in (dm["user1"], dm["user2"]):
        conn.close()
        raise HTTPException(status_code=403, detail="Forbidden")
    if before_id and before_id > 0:
        cur.execute("""SELECT * FROM messages WHERE scope='dm' AND dm_id=? AND id<? ORDER BY id DESC LIMIT ?""", (dm_id, before_id, int(limit)))
    else:
        cur.execute("""SELECT * FROM messages WHERE scope='dm' AND dm_id=? ORDER BY id DESC LIMIT ?""", (dm_id, int(limit)))
    rows = cur.fetchall()
    mids = [r["id"] for r in rows]
    rx = {}
    if mids:
        q = ",".join("?" for _ in mids)
        cur.execute(f"SELECT message_id, emoji, COUNT(*) AS c FROM reactions WHERE message_id IN ({q}) GROUP BY message_id, emoji", mids)
        for r in cur.fetchall():
            rx.setdefault(r["message_id"], []).append({"emoji": r["emoji"], "count": r["c"]})
    out = []
    for r in reversed(rows):
        out.append({
            "id": r["id"],
            "scope": r["scope"],
            "channel_id": r["channel_id"],
            "dm_id": r["dm_id"],
            "sender": user_basic(r["sender_id"]),
            "content": r["content"],
            "attachments": json.loads(r["attachments_json"] or "[]"),
            "created_ms": r["created_ms"],
            "reactions": rx.get(r["id"], [])
        })
    conn.close()
    return {"ok": True, "messages": out}

@app.post("/files/upload")
async def upload_file(file: UploadFile = File(...), authorization: Optional[str] = Header(default=None)):
    require_user(authorization)
    raw = await file.read()
    stored = store_upload(raw, file.filename or "file.bin")
    return {"ok": True, "file_id": stored, "url": f"/files/{stored}", "name": file.filename or "file.bin", "bytes": len(raw)}

@app.get("/files/{file_id}")
async def get_file(file_id: str):
    return FileResponse(file_path(file_id), filename=file_id.split("_", 1)[-1])

@app.post("/reactions/toggle")
async def toggle_reaction(message_id: int = Form(...), emoji: str = Form(...), authorization: Optional[str] = Header(default=None)):
    u = require_user(authorization)
    em = emoji.strip()
    if not em or len(em) > 32:
        raise HTTPException(status_code=400, detail="Bad emoji")
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT scope, channel_id, dm_id FROM messages WHERE id=?", (int(message_id),))
    m = cur.fetchone()
    if not m:
        conn.close()
        raise HTTPException(status_code=404, detail="Message not found")
    scope = m["scope"]
    if scope == "channel":
        cur.execute("SELECT guild_id FROM channels WHERE id=?", (m["channel_id"],))
        ch = cur.fetchone()
        if not ch:
            conn.close()
            raise HTTPException(status_code=404, detail="Channel not found")
        require_membership(u["id"], ch["guild_id"])
        broadcast_scope = f"channel:{m['channel_id']}"
    else:
        cur.execute("SELECT user1, user2 FROM dms WHERE id=?", (m["dm_id"],))
        dm = cur.fetchone()
        if not dm:
            conn.close()
            raise HTTPException(status_code=404, detail="DM not found")
        uid = int(u["id"])
        if uid not in (dm["user1"], dm["user2"]):
            conn.close()
            raise HTTPException(status_code=403, detail="Forbidden")
        broadcast_scope = f"dm:{m['dm_id']}"
    cur.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (int(message_id), u["id"], em))
    exists = cur.fetchone()
    if exists:
        cur.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (int(message_id), u["id"], em))
        conn.commit()
        conn.close()
        await hub.broadcast(broadcast_scope, {"t": "reaction_update", "message_id": int(message_id)})
        return {"ok": True, "state": "removed"}
    cur.execute("INSERT INTO reactions(message_id, user_id, emoji, created_ms) VALUES(?,?,?,?)", (int(message_id), u["id"], em, now_ms()))
    conn.commit()
    conn.close()
    await hub.broadcast(broadcast_scope, {"t": "reaction_update", "message_id": int(message_id)})
    return {"ok": True, "state": "added"}

@app.get("/presence")
async def presence(authorization: Optional[str] = Header(default=None)):
    require_user(authorization)
    return {"ok": True, "online_user_ids": list(online_user_ids()), "voice": hub.user_active_voice}

async def insert_message(scope: str, channel_id: Optional[int], dm_id: Optional[int], sender_id: int, content: str, attachments: List[Dict[str, Any]]) -> Dict[str, Any]:
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO messages(scope, channel_id, dm_id, sender_id, content, attachments_json, created_ms) VALUES(?,?,?,?,?,?,?)",
                (scope, channel_id, dm_id, sender_id, content, json.dumps(attachments, ensure_ascii=False), now_ms()))
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    msg = {
        "id": mid,
        "scope": scope,
        "channel_id": channel_id,
        "dm_id": dm_id,
        "sender": user_basic(sender_id),
        "content": content,
        "attachments": attachments,
        "created_ms": now_ms(),
        "reactions": []
    }
    return msg

@app.websocket("/ws")
async def ws(ws: WebSocket):
    token = ws.query_params.get("token", "")
    if not token:
        await ws.close(code=4401)
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM sessions WHERE token=?", (token,))
    r = cur.fetchone()
    conn.close()
    if not r:
        await ws.close(code=4401)
        return
    user_id = int(r["user_id"])
    await ws.accept()
    await hub.add_ws(user_id, ws)
    await hub.send_user(user_id, {"t": "ready", "user": user_basic(user_id), "online_user_ids": list(online_user_ids())})
    await hub.broadcast("presence", {"t": "presence_update", "online_user_ids": list(online_user_ids()), "voice": hub.user_active_voice})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            op = str(data.get("op", ""))
            if op == "subscribe":
                scope = str(data.get("scope", ""))
                if scope.startswith("channel:") or scope.startswith("dm:") or scope.startswith("guild:") or scope == "presence":
                    await hub.subscribe(scope, ws)
                    await ws.send_text(json.dumps({"t": "subscribed", "scope": scope}, ensure_ascii=False))
            elif op == "unsubscribe_all":
                await hub.unsubscribe_all(ws)
            elif op == "send_channel":
                channel_id = int(data.get("channel_id", 0))
                content = str(data.get("content", ""))[:4000]
                attachments = data.get("attachments") or []
                if not isinstance(attachments, list):
                    attachments = []
                conn = db()
                cur = conn.cursor()
                cur.execute("SELECT guild_id FROM channels WHERE id=?", (channel_id,))
                ch = cur.fetchone()
                conn.close()
                if not ch:
                    continue
                require_membership(user_id, ch["guild_id"])
                msg = await insert_message("channel", channel_id, None, user_id, content, attachments)
                await hub.broadcast(f"channel:{channel_id}", {"t": "message", "message": msg})
            elif op == "send_dm":
                dm_id = int(data.get("dm_id", 0))
                content = str(data.get("content", ""))[:4000]
                attachments = data.get("attachments") or []
                if not isinstance(attachments, list):
                    attachments = []
                conn = db()
                cur = conn.cursor()
                cur.execute("SELECT user1, user2 FROM dms WHERE id=?", (dm_id,))
                dm = cur.fetchone()
                conn.close()
                if not dm:
                    continue
                if user_id not in (dm["user1"], dm["user2"]):
                    continue
                msg = await insert_message("dm", None, dm_id, user_id, content, attachments)
                await hub.broadcast(f"dm:{dm_id}", {"t": "message", "message": msg})
            elif op == "voice_join":
                channel_id = int(data.get("channel_id", 0))
                hub.user_active_voice[user_id] = channel_id
                await hub.broadcast("presence", {"t": "presence_update", "online_user_ids": list(online_user_ids()), "voice": hub.user_active_voice})
                await hub.broadcast(f"voice:{channel_id}", {"t": "voice_state", "channel_id": channel_id, "voice": hub.user_active_voice})
            elif op == "voice_leave":
                if user_id in hub.user_active_voice:
                    del hub.user_active_voice[user_id]
                await hub.broadcast("presence", {"t": "presence_update", "online_user_ids": list(online_user_ids()), "voice": hub.user_active_voice})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.remove_ws(user_id, ws)
        await hub.broadcast("presence", {"t": "presence_update", "online_user_ids": list(online_user_ids()), "voice": hub.user_active_voice})
