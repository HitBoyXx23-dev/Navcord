# Navcord_server.py
import os, json, time, base64, secrets, sqlite3, hashlib, asyncio
from typing import Dict, Any, Optional, Set, Tuple, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

APP_NAME = "Navcord"
DB_PATH = os.environ.get("NAVCORD_DB", "navcord.db")
DATA_DIR = os.environ.get("NAVCORD_DATA", "navcord_data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
AVATAR_DIR = os.path.join(DATA_DIR, "avatars")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(AVATAR_DIR, exist_ok=True)

def now() -> int:
    return int(time.time())

def j(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

def h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("utf-8")

def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("utf-8"))

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      avatar_file TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      expires_at INTEGER NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS guilds(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      owner_id INTEGER NOT NULL,
      created_at INTEGER NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS guild_members(
      guild_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      role TEXT NOT NULL,
      joined_at INTEGER NOT NULL,
      PRIMARY KEY (guild_id, user_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS channels(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      type TEXT NOT NULL,
      created_at INTEGER NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS dms(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_a INTEGER NOT NULL,
      user_b INTEGER NOT NULL,
      created_at INTEGER NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      target_id INTEGER NOT NULL,
      author_id INTEGER NOT NULL,
      content TEXT NOT NULL,
      attachments TEXT,
      created_at INTEGER NOT NULL,
      edited_at INTEGER,
      reactions TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS files(
      id TEXT PRIMARY KEY,
      owner_id INTEGER NOT NULL,
      filename TEXT NOT NULL,
      path TEXT NOT NULL,
      size INTEGER NOT NULL,
      mime TEXT,
      created_at INTEGER NOT NULL
    )
    """)
    conn.commit()
    conn.close()

init_db()

try:
    import bcrypt
except Exception:
    bcrypt = None

def require_bcrypt():
    if bcrypt is None:
        raise RuntimeError("bcrypt missing")

def hash_pw(pw: str) -> str:
    require_bcrypt()
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw.encode("utf-8"), salt).decode("utf-8")

def verify_pw(pw: str, pw_hash: str) -> bool:
    require_bcrypt()
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), pw_hash.encode("utf-8"))
    except Exception:
        return False

def create_default_guild_for_user(conn, user_id: int):
    cur = conn.cursor()
    cur.execute("INSERT INTO guilds(name,owner_id,created_at) VALUES(?,?,?)", ("Navine Home", user_id, now()))
    gid = cur.lastrowid
    cur.execute("INSERT INTO guild_members(guild_id,user_id,role,joined_at) VALUES(?,?,?,?)", (gid, user_id, "owner", now()))
    cur.execute("INSERT INTO channels(guild_id,name,type,created_at) VALUES(?,?,?,?)", (gid, "general", "text", now()))
    cur.execute("INSERT INTO channels(guild_id,name,type,created_at) VALUES(?,?,?,?)", (gid, "lounge", "voice", now()))
    return gid

def get_user_by_token(conn, token: str):
    cur = conn.cursor()
    cur.execute("SELECT user_id,expires_at FROM sessions WHERE token=?", (token,))
    r = cur.fetchone()
    if not r:
        return None
    if int(r["expires_at"]) < now():
        cur.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    cur.execute("SELECT id,username,avatar_file FROM users WHERE id=?", (int(r["user_id"]),))
    u = cur.fetchone()
    return u

def token_for_user(conn, user_id: int, ttl_sec: int = 60*60*24*7) -> str:
    t = secrets.token_urlsafe(32)
    cur = conn.cursor()
    cur.execute("INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)", (t, user_id, now(), now()+ttl_sec))
    conn.commit()
    return t

def user_public_row(row):
    if not row:
        return None
    return {"id": int(row["id"]), "username": row["username"], "avatar": row["avatar_file"]}

def list_guilds_for_user(conn, user_id: int):
    cur = conn.cursor()
    cur.execute("""
      SELECT g.id,g.name,g.owner_id,g.created_at
      FROM guilds g
      JOIN guild_members gm ON gm.guild_id=g.id
      WHERE gm.user_id=?
      ORDER BY g.id ASC
    """, (user_id,))
    out = []
    for r in cur.fetchall():
        out.append({"id": int(r["id"]), "name": r["name"], "owner_id": int(r["owner_id"]), "created_at": int(r["created_at"])})
    return out

def list_channels_for_guild(conn, guild_id: int):
    cur = conn.cursor()
    cur.execute("SELECT id,name,type,created_at FROM channels WHERE guild_id=? ORDER BY type DESC, id ASC", (guild_id,))
    out = []
    for r in cur.fetchall():
        out.append({"id": int(r["id"]), "name": r["name"], "type": r["type"], "created_at": int(r["created_at"])})
    return out

def list_members_for_guild(conn, guild_id: int):
    cur = conn.cursor()
    cur.execute("""
      SELECT u.id,u.username,u.avatar_file,gm.role
      FROM guild_members gm
      JOIN users u ON u.id=gm.user_id
      WHERE gm.guild_id=?
      ORDER BY u.username COLLATE NOCASE ASC
    """, (guild_id,))
    out = []
    for r in cur.fetchall():
        out.append({"id": int(r["id"]), "username": r["username"], "avatar": r["avatar_file"], "role": r["role"]})
    return out

def is_member(conn, guild_id: int, user_id: int) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM guild_members WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    return cur.fetchone() is not None

def ensure_dm(conn, a: int, b: int) -> int:
    if a == b:
        raise ValueError("self")
    x, y = (a, b) if a < b else (b, a)
    cur = conn.cursor()
    cur.execute("SELECT id FROM dms WHERE user_a=? AND user_b=?", (x, y))
    r = cur.fetchone()
    if r:
        return int(r["id"])
    cur.execute("INSERT INTO dms(user_a,user_b,created_at) VALUES(?,?,?)", (x, y, now()))
    conn.commit()
    return int(cur.lastrowid)

def list_dms_for_user(conn, user_id: int):
    cur = conn.cursor()
    cur.execute("SELECT id,user_a,user_b,created_at FROM dms WHERE user_a=? OR user_b=? ORDER BY id DESC", (user_id, user_id))
    out = []
    for r in cur.fetchall():
        other = int(r["user_b"]) if int(r["user_a"]) == user_id else int(r["user_a"])
        cur2 = conn.cursor()
        cur2.execute("SELECT id,username,avatar_file FROM users WHERE id=?", (other,))
        u = cur2.fetchone()
        out.append({"id": int(r["id"]), "other": user_public_row(u), "created_at": int(r["created_at"])})
    return out

def fetch_messages(conn, kind: str, target_id: int, limit: int = 100):
    cur = conn.cursor()
    cur.execute("""
      SELECT m.id,m.kind,m.target_id,m.author_id,m.content,m.attachments,m.created_at,m.edited_at,m.reactions,
             u.username,u.avatar_file
      FROM messages m
      JOIN users u ON u.id=m.author_id
      WHERE m.kind=? AND m.target_id=?
      ORDER BY m.id DESC
      LIMIT ?
    """, (kind, target_id, limit))
    rows = cur.fetchall()
    out = []
    for r in reversed(rows):
        out.append({
            "id": int(r["id"]),
            "kind": r["kind"],
            "target_id": int(r["target_id"]),
            "author": {"id": int(r["author_id"]), "username": r["username"], "avatar": r["avatar_file"]},
            "content": r["content"],
            "attachments": json.loads(r["attachments"]) if r["attachments"] else [],
            "created_at": int(r["created_at"]),
            "edited_at": int(r["edited_at"]) if r["edited_at"] else None,
            "reactions": json.loads(r["reactions"]) if r["reactions"] else {}
        })
    return out

def insert_message(conn, kind: str, target_id: int, author_id: int, content: str, attachments: Optional[list]):
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO messages(kind,target_id,author_id,content,attachments,created_at,reactions)
      VALUES(?,?,?,?,?,?,?)
    """, (kind, target_id, author_id, content, j(attachments or []), now(), j({})))
    conn.commit()
    return int(cur.lastrowid)

def update_reaction(conn, msg_id: int, user_id: int, emoji: str, add: bool):
    cur = conn.cursor()
    cur.execute("SELECT reactions FROM messages WHERE id=?", (msg_id,))
    r = cur.fetchone()
    if not r:
        return None
    reactions = json.loads(r["reactions"]) if r["reactions"] else {}
    entry = reactions.get(emoji, {"count": 0, "users": []})
    users = set(entry.get("users") or [])
    if add:
        if user_id not in users:
            users.add(user_id)
    else:
        if user_id in users:
            users.remove(user_id)
    entry["users"] = sorted(list(users))
    entry["count"] = len(users)
    if entry["count"] <= 0:
        if emoji in reactions:
            reactions.pop(emoji, None)
    else:
        reactions[emoji] = entry
    cur.execute("UPDATE messages SET reactions=? WHERE id=?", (j(reactions), msg_id))
    conn.commit()
    return reactions

class Hub:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.ws_by_user: Dict[int, Set[WebSocket]] = {}
        self.chat_rooms: Dict[str, Set[WebSocket]] = {}
        self.voice_rooms: Dict[str, Set[WebSocket]] = {}
        self.active_speaker: Dict[str, Optional[int]] = {}
        self.last_seen: Dict[int, int] = {}

    async def add_user_ws(self, user_id: int, ws: WebSocket):
        async with self.lock:
            self.ws_by_user.setdefault(user_id, set()).add(ws)
            self.last_seen[user_id] = now()

    async def remove_user_ws(self, user_id: int, ws: WebSocket):
        async with self.lock:
            s = self.ws_by_user.get(user_id)
            if s and ws in s:
                s.remove(ws)
            if s and len(s) == 0:
                self.ws_by_user.pop(user_id, None)
            self.last_seen[user_id] = now()

    async def join_room(self, room_key: str, ws: WebSocket, voice: bool = False):
        async with self.lock:
            rooms = self.voice_rooms if voice else self.chat_rooms
            rooms.setdefault(room_key, set()).add(ws)

    async def leave_room(self, room_key: str, ws: WebSocket, voice: bool = False):
        async with self.lock:
            rooms = self.voice_rooms if voice else self.chat_rooms
            s = rooms.get(room_key)
            if s and ws in s:
                s.remove(ws)
            if s and len(s) == 0:
                rooms.pop(room_key, None)
                if voice:
                    self.active_speaker.pop(room_key, None)

    async def broadcast_room(self, room_key: str, payload: dict, voice: bool = False):
        async with self.lock:
            rooms = self.voice_rooms if voice else self.chat_rooms
            targets = list(rooms.get(room_key, set()))
        msg = j(payload)
        dead = []
        for w in targets:
            try:
                await w.send_text(msg)
            except Exception:
                dead.append(w)
        if dead:
            async with self.lock:
                s = rooms.get(room_key, set())
                for d in dead:
                    if d in s:
                        s.remove(d)

    async def broadcast_room_binary(self, room_key: str, data: bytes, sender_id: int):
        async with self.lock:
            targets = list(self.voice_rooms.get(room_key, set()))
        dead = []
        for w in targets:
            try:
                await w.send_bytes(data)
            except Exception:
                dead.append(w)
        if dead:
            async with self.lock:
                s = self.voice_rooms.get(room_key, set())
                for d in dead:
                    if d in s:
                        s.remove(d)

    async def online_user_ids(self) -> List[int]:
        async with self.lock:
            return sorted(list(self.ws_by_user.keys()))

hub = Hub()

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True, "ts": now(), "name": APP_NAME}

@app.post("/auth/register")
async def register(payload: Dict[str, Any]):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if len(username) < 3 or len(username) > 24:
        raise HTTPException(status_code=400, detail="bad username")
    if len(password) < 6 or len(password) > 128:
        raise HTTPException(status_code=400, detail="bad password")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (username,))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="taken")
    pw_hash = hash_pw(password)
    cur.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)", (username, pw_hash, now()))
    user_id = int(cur.lastrowid)
    create_default_guild_for_user(conn, user_id)
    conn.commit()
    token = token_for_user(conn, user_id)
    conn.close()
    return {"token": token}

@app.post("/auth/login")
async def login(payload: Dict[str, Any]):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,password_hash FROM users WHERE username=?", (username,))
    r = cur.fetchone()
    if not r or not verify_pw(password, r["password_hash"]):
        conn.close()
        raise HTTPException(status_code=401, detail="bad creds")
    token = token_for_user(conn, int(r["id"]))
    conn.close()
    return {"token": token}

@app.get("/me")
async def me(authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").replace("Bearer", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="no token")
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        raise HTTPException(status_code=401, detail="bad token")
    user = user_public_row(u)
    guilds = list_guilds_for_user(conn, user["id"])
    dms = list_dms_for_user(conn, user["id"])
    online_ids = await hub.online_user_ids()
    conn.close()
    return {"user": user, "guilds": guilds, "dms": dms, "online": online_ids}

@app.post("/guilds/create")
async def create_guild(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").replace("Bearer", "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        raise HTTPException(status_code=401, detail="bad token")
    name = (payload.get("name") or "").strip()
    if len(name) < 2 or len(name) > 40:
        conn.close()
        raise HTTPException(status_code=400, detail="bad name")
    cur = conn.cursor()
    cur.execute("INSERT INTO guilds(name,owner_id,created_at) VALUES(?,?,?)", (name, int(u["id"]), now()))
    gid = int(cur.lastrowid)
    cur.execute("INSERT INTO guild_members(guild_id,user_id,role,joined_at) VALUES(?,?,?,?)", (gid, int(u["id"]), "owner", now()))
    cur.execute("INSERT INTO channels(guild_id,name,type,created_at) VALUES(?,?,?,?)", (gid, "general", "text", now()))
    cur.execute("INSERT INTO channels(guild_id,name,type,created_at) VALUES(?,?,?,?)", (gid, "voice", "voice", now()))
    conn.commit()
    conn.close()
    return {"guild_id": gid}

@app.post("/guilds/{guild_id}/join")
async def join_guild(guild_id: int, authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").replace("Bearer", "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        raise HTTPException(status_code=401, detail="bad token")
    if is_member(conn, guild_id, int(u["id"])):
        conn.close()
        return {"ok": True}
    cur = conn.cursor()
    cur.execute("SELECT id FROM guilds WHERE id=?", (guild_id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="no guild")
    cur.execute("INSERT OR IGNORE INTO guild_members(guild_id,user_id,role,joined_at) VALUES(?,?,?,?)", (guild_id, int(u["id"]), "member", now()))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/guilds/{guild_id}/channels")
async def guild_channels(guild_id: int, authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").replace("Bearer", "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        raise HTTPException(status_code=401, detail="bad token")
    if not is_member(conn, guild_id, int(u["id"])):
        conn.close()
        raise HTTPException(status_code=403, detail="not member")
    channels = list_channels_for_guild(conn, guild_id)
    members = list_members_for_guild(conn, guild_id)
    online = await hub.online_user_ids()
    conn.close()
    return {"channels": channels, "members": members, "online": online}

@app.get("/messages/{kind}/{target_id}")
async def messages(kind: str, target_id: int, authorization: Optional[str] = Header(default=None)):
    if kind not in ("channel", "dm"):
        raise HTTPException(status_code=400, detail="bad kind")
    token = (authorization or "").replace("Bearer", "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        raise HTTPException(status_code=401, detail="bad token")
    uid = int(u["id"])
    if kind == "channel":
        cur = conn.cursor()
        cur.execute("SELECT guild_id,type FROM channels WHERE id=?", (target_id,))
        r = cur.fetchone()
        if not r or r["type"] != "text":
            conn.close()
            raise HTTPException(status_code=404, detail="no channel")
        if not is_member(conn, int(r["guild_id"]), uid):
            conn.close()
            raise HTTPException(status_code=403, detail="not member")
    else:
        cur = conn.cursor()
        cur.execute("SELECT user_a,user_b FROM dms WHERE id=?", (target_id,))
        r = cur.fetchone()
        if not r:
            conn.close()
            raise HTTPException(status_code=404, detail="no dm")
        if uid not in (int(r["user_a"]), int(r["user_b"])):
            conn.close()
            raise HTTPException(status_code=403, detail="no dm access")
    msgs = fetch_messages(conn, kind, target_id, limit=150)
    conn.close()
    return {"messages": msgs}

@app.post("/dm/create")
async def dm_create(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").replace("Bearer", "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        raise HTTPException(status_code=401, detail="bad token")
    other_name = (payload.get("username") or "").strip()
    cur = conn.cursor()
    cur.execute("SELECT id,username,avatar_file FROM users WHERE username=?", (other_name,))
    other = cur.fetchone()
    if not other:
        conn.close()
        raise HTTPException(status_code=404, detail="no user")
    dm_id = ensure_dm(conn, int(u["id"]), int(other["id"]))
    conn.commit()
    conn.close()
    return {"dm_id": dm_id}

@app.post("/avatar")
async def upload_avatar(file: UploadFile = File(...), authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").replace("Bearer", "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        raise HTTPException(status_code=401, detail="bad token")
    content = await file.read()
    if len(content) > 5_000_000:
        conn.close()
        raise HTTPException(status_code=413, detail="too big")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        ext = ".png"
    fid = f"av_{int(u['id'])}_{secrets.token_hex(8)}{ext}"
    path = os.path.join(AVATAR_DIR, fid)
    with open(path, "wb") as f:
        f.write(content)
    cur = conn.cursor()
    cur.execute("UPDATE users SET avatar_file=? WHERE id=?", (fid, int(u["id"])))
    conn.commit()
    conn.close()
    return {"avatar": fid}

@app.get("/avatar/{avatar_file}")
async def get_avatar(avatar_file: str):
    path = os.path.join(AVATAR_DIR, avatar_file)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="no avatar")
    return FileResponse(path)

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").replace("Bearer", "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        raise HTTPException(status_code=401, detail="bad token")
    content = await file.read()
    if len(content) > 50_000_000:
        conn.close()
        raise HTTPException(status_code=413, detail="too big")
    fid = secrets.token_urlsafe(10).replace("-", "_")
    safe_name = (file.filename or "file").replace("\\", "_").replace("/", "_")[:140]
    path = os.path.join(UPLOAD_DIR, f"{fid}_{safe_name}")
    with open(path, "wb") as f:
        f.write(content)
    mime = file.content_type
    cur = conn.cursor()
    cur.execute("INSERT INTO files(id,owner_id,filename,path,size,mime,created_at) VALUES(?,?,?,?,?,?,?)",
                (fid, int(u["id"]), safe_name, path, len(content), mime, now()))
    conn.commit()
    conn.close()
    return {"file_id": fid, "filename": safe_name, "url": f"/files/{fid}"}

@app.get("/files/{file_id}")
async def get_file(file_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT filename,path FROM files WHERE id=?", (file_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="no file")
    path = r["path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="missing")
    return FileResponse(path, filename=r["filename"])

def room_key_for(kind: str, target_id: int) -> str:
    return f"{kind}:{target_id}"

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    token = (ws.query_params.get("token") or "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        await ws.send_text(j({"t": "err", "m": "bad token"}))
        await ws.close()
        return
    uid = int(u["id"])
    await hub.add_user_ws(uid, ws)
    await ws.send_text(j({"t": "ready", "user": user_public_row(u), "online": await hub.online_user_ids()}))
    joined: Set[str] = set()
    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except Exception:
                continue
            t = data.get("t")
            if t == "ping":
                await ws.send_text(j({"t": "pong", "ts": now()}))
            elif t == "join":
                kind = data.get("kind")
                target_id = int(data.get("target_id") or 0)
                if kind not in ("channel", "dm"):
                    continue
                if kind == "channel":
                    cur = conn.cursor()
                    cur.execute("SELECT guild_id,type FROM channels WHERE id=?", (target_id,))
                    r = cur.fetchone()
                    if not r or r["type"] != "text":
                        await ws.send_text(j({"t": "err", "m": "no channel"}))
                        continue
                    if not is_member(conn, int(r["guild_id"]), uid):
                        await ws.send_text(j({"t": "err", "m": "not member"}))
                        continue
                else:
                    cur = conn.cursor()
                    cur.execute("SELECT user_a,user_b FROM dms WHERE id=?", (target_id,))
                    r = cur.fetchone()
                    if not r or uid not in (int(r["user_a"]), int(r["user_b"])):
                        await ws.send_text(j({"t": "err", "m": "no dm access"}))
                        continue
                rk = room_key_for(kind, target_id)
                if rk not in joined:
                    await hub.join_room(rk, ws, voice=False)
                    joined.add(rk)
                msgs = fetch_messages(conn, kind, target_id, limit=120)
                await ws.send_text(j({"t": "history", "kind": kind, "target_id": target_id, "messages": msgs}))
            elif t == "say":
                kind = data.get("kind")
                target_id = int(data.get("target_id") or 0)
                content = (data.get("content") or "")[:4000]
                attachments = data.get("attachments") or []
                if kind not in ("channel", "dm"):
                    continue
                if kind == "channel":
                    cur = conn.cursor()
                    cur.execute("SELECT guild_id,type FROM channels WHERE id=?", (target_id,))
                    r = cur.fetchone()
                    if not r or r["type"] != "text":
                        continue
                    if not is_member(conn, int(r["guild_id"]), uid):
                        continue
                else:
                    cur = conn.cursor()
                    cur.execute("SELECT user_a,user_b FROM dms WHERE id=?", (target_id,))
                    r = cur.fetchone()
                    if not r or uid not in (int(r["user_a"]), int(r["user_b"])):
                        continue
                mid = insert_message(conn, kind, target_id, uid, content, attachments)
                cur = conn.cursor()
                cur.execute("SELECT username,avatar_file FROM users WHERE id=?", (uid,))
                ur = cur.fetchone()
                payload = {
                    "t": "msg",
                    "message": {
                        "id": mid,
                        "kind": kind,
                        "target_id": target_id,
                        "author": {"id": uid, "username": ur["username"], "avatar": ur["avatar_file"]},
                        "content": content,
                        "attachments": attachments,
                        "created_at": now(),
                        "edited_at": None,
                        "reactions": {}
                    }
                }
                await hub.broadcast_room(room_key_for(kind, target_id), payload, voice=False)
            elif t == "react":
                msg_id = int(data.get("msg_id") or 0)
                emoji = (data.get("emoji") or "")[:16]
                add = bool(data.get("add"))
                if not emoji:
                    continue
                cur = conn.cursor()
                cur.execute("SELECT kind,target_id FROM messages WHERE id=?", (msg_id,))
                mr = cur.fetchone()
                if not mr:
                    continue
                kind = mr["kind"]; target_id = int(mr["target_id"])
                if kind == "channel":
                    cur2 = conn.cursor()
                    cur2.execute("SELECT guild_id FROM channels WHERE id=?", (target_id,))
                    cr = cur2.fetchone()
                    if not cr or not is_member(conn, int(cr["guild_id"]), uid):
                        continue
                else:
                    cur2 = conn.cursor()
                    cur2.execute("SELECT user_a,user_b FROM dms WHERE id=?", (target_id,))
                    dr = cur2.fetchone()
                    if not dr or uid not in (int(dr["user_a"]), int(dr["user_b"])):
                        continue
                reactions = update_reaction(conn, msg_id, uid, emoji, add)
                if reactions is None:
                    continue
                await hub.broadcast_room(room_key_for(kind, target_id), {"t": "reactions", "msg_id": msg_id, "reactions": reactions}, voice=False)
            elif t == "presence":
                online = await hub.online_user_ids()
                await ws.send_text(j({"t": "online", "online": online}))
            elif t == "dm_create":
                other = (data.get("username") or "").strip()
                if not other:
                    continue
                cur = conn.cursor()
                cur.execute("SELECT id FROM users WHERE username=?", (other,))
                r = cur.fetchone()
                if not r:
                    await ws.send_text(j({"t": "err", "m": "no user"}))
                    continue
                dm_id = ensure_dm(conn, uid, int(r["id"]))
                await ws.send_text(j({"t": "dm", "dm_id": dm_id}))
            elif t == "guild_state":
                guild_id = int(data.get("guild_id") or 0)
                if not is_member(conn, guild_id, uid):
                    continue
                channels = list_channels_for_guild(conn, guild_id)
                members = list_members_for_guild(conn, guild_id)
                online = await hub.online_user_ids()
                await ws.send_text(j({"t": "guild_state", "guild_id": guild_id, "channels": channels, "members": members, "online": online}))
            else:
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        for rk in list(joined):
            try:
                await hub.leave_room(rk, ws, voice=False)
            except Exception:
                pass
        try:
            await hub.remove_user_ws(uid, ws)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        try:
            online = await hub.online_user_ids()
            await hub.broadcast_room("presence:all", {"t": "online", "online": online}, voice=False)
        except Exception:
            pass

@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    await ws.accept()
    token = (ws.query_params.get("token") or "").strip()
    room = (ws.query_params.get("room") or "").strip()
    conn = get_conn()
    u = get_user_by_token(conn, token)
    if not u:
        conn.close()
        await ws.close()
        return
    uid = int(u["id"])
    if ":" not in room:
        conn.close()
        await ws.close()
        return
    rk = f"voice:{room}"
    await hub.join_room(rk, ws, voice=True)
    if rk not in hub.active_speaker:
        hub.active_speaker[rk] = None
    await ws.send_text(j({"t": "voice_ready", "room": room, "active": hub.active_speaker.get(rk)}))
    try:
        while True:
            m = await ws.receive()
            if "text" in m and m["text"] is not None:
                try:
                    data = json.loads(m["text"])
                except Exception:
                    continue
                t = data.get("t")
                if t == "voice_begin":
                    async with hub.lock:
                        if hub.active_speaker.get(rk) in (None, uid):
                            hub.active_speaker[rk] = uid
                    await hub.broadcast_room(rk, {"t": "voice_active", "user_id": hub.active_speaker.get(rk)}, voice=True)
                elif t == "voice_end":
                    async with hub.lock:
                        if hub.active_speaker.get(rk) == uid:
                            hub.active_speaker[rk] = None
                    await hub.broadcast_room(rk, {"t": "voice_active", "user_id": hub.active_speaker.get(rk)}, voice=True)
                else:
                    continue
            if "bytes" in m and m["bytes"] is not None:
                async with hub.lock:
                    active = hub.active_speaker.get(rk)
                if active != uid:
                    continue
                data = m["bytes"]
                if not data:
                    continue
                if len(data) > 65536:
                    continue
                await hub.broadcast_room_binary(rk, data, uid)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            async with hub.lock:
                if hub.active_speaker.get(rk) == uid:
                    hub.active_speaker[rk] = None
        except Exception:
            pass
        try:
            await hub.broadcast_room(rk, {"t": "voice_active", "user_id": hub.active_speaker.get(rk)}, voice=True)
        except Exception:
            pass
        try:
            await hub.leave_room(rk, ws, voice=True)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("Navcord_server:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
