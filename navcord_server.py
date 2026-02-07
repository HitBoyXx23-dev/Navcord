import asyncio, json, os, time, sqlite3, base64, tempfile, bcrypt, websockets
from websockets.server import WebSocketServerProtocol

APP="Navcord"
def app_dir():
    p=os.path.join(os.getenv("RENDER_DATA_DIR") or os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or tempfile.gettempdir(), APP)
    os.makedirs(p, exist_ok=True)
    return p

ROOT=app_dir()
DB=os.path.join(ROOT,"navcord.db")
AV_DIR=os.path.join(ROOT,"avatars")
FILE_DIR=os.path.join(ROOT,"files")

HOST="0.0.0.0"
WS_PORT=int(os.getenv("PORT","10000"))
UDP_PORT=9999

MAX_TEXT=4000
MAX_AVATAR=512*1024
MAX_FILE=25*1024*1024
MAX_MSG_RATE_WINDOW=3.0
MAX_MSG_RATE_COUNT=20

os.makedirs(AV_DIR, exist_ok=True)
os.makedirs(FILE_DIR, exist_ok=True)

def db_init():
    con=sqlite3.connect(DB)
    cur=con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, pw BLOB, avatar TEXT, created_at INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS guilds(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, icon TEXT, created_at INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS memberships(user_id INTEGER, guild_id INTEGER, role TEXT, PRIMARY KEY(user_id,guild_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, kind TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, user_id INTEGER, ts INTEGER, content TEXT, kind TEXT, meta TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER, user_id INTEGER, emoji TEXT, PRIMARY KEY(message_id,user_id,emoji))")
    con.commit()
    cur.execute("SELECT id FROM guilds ORDER BY id LIMIT 1")
    row=cur.fetchone()
    if not row:
        cur.execute("INSERT INTO guilds(name,icon,created_at) VALUES(?,?,?)", ("Home", None, int(time.time())))
        gid=cur.lastrowid
        cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)", (gid, "general", "text"))
        cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)", (gid, "Lounge", "voice"))
        con.commit()
    con.close()

def db_con():
    return sqlite3.connect(DB)

def clean_name(s, n=32):
    s=(s or "").strip()
    ok=[]
    for ch in s:
        if ch.isalnum() or ch in (" ","_","-",".","#"):
            ok.append(ch)
    s="".join(ok).strip()
    return s[:n] if s else ""

def create_user(username, pw):
    username=clean_name(username, 24)
    if not username or not pw or len(pw)<4:
        return False, "bad"
    h=bcrypt.hashpw(pw.encode(), bcrypt.gensalt())
    con=db_con();cur=con.cursor()
    try:
        cur.execute("INSERT INTO users(username,pw,avatar,created_at) VALUES(?,?,?,?)", (username, h, None, int(time.time())))
        con.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "exists"
    finally:
        con.close()

def check_login(username, pw):
    con=db_con();cur=con.cursor()
    cur.execute("SELECT id,pw,avatar FROM users WHERE username=?", (username,))
    r=cur.fetchone();con.close()
    if not r:
        return None
    uid, hpw, av = r
    try:
        if bcrypt.checkpw(pw.encode(), hpw):
            return (uid, username, av)
    except:
        return None
    return None

def ensure_membership(uid, gid):
    con=db_con();cur=con.cursor()
    cur.execute("INSERT OR IGNORE INTO memberships(user_id,guild_id,role) VALUES(?,?,?)", (uid, gid, "member"))
    con.commit();con.close()

def list_guilds_for(uid):
    con=db_con();cur=con.cursor()
    cur.execute("""
        SELECT g.id,g.name,g.icon
        FROM guilds g
        JOIN memberships m ON m.guild_id=g.id
        WHERE m.user_id=?
        ORDER BY g.id
    """, (uid,))
    rows=cur.fetchall();con.close()
    return [{"id":i,"name":n,"icon":ic} for (i,n,ic) in rows]

def list_channels(gid):
    con=db_con();cur=con.cursor()
    cur.execute("SELECT id,name,kind FROM channels WHERE guild_id=? ORDER BY kind DESC, id", (gid,))
    rows=cur.fetchall();con.close()
    return [{"id":i,"name":n,"kind":k} for (i,n,k) in rows]

def create_guild(name):
    name=clean_name(name, 32)
    if not name:
        return None
    con=db_con();cur=con.cursor()
    cur.execute("INSERT INTO guilds(name,icon,created_at) VALUES(?,?,?)", (name, None, int(time.time())))
    gid=cur.lastrowid
    cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)", (gid, "general", "text"))
    cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)", (gid, "Lounge", "voice"))
    con.commit();con.close()
    return gid

def add_message(channel_id, user_id, content, kind="text", meta=None):
    con=db_con();cur=con.cursor()
    cur.execute("INSERT INTO messages(channel_id,user_id,ts,content,kind,meta) VALUES(?,?,?,?,?,?)", (channel_id, user_id, int(time.time()), content, kind, json.dumps(meta or {})))
    mid=cur.lastrowid
    con.commit();con.close()
    return mid

def reactions_for_messages(message_ids):
    if not message_ids:
        return {}
    con=db_con();cur=con.cursor()
    q=",".join("?" for _ in message_ids)
    cur.execute(f"SELECT r.message_id,r.emoji,u.username FROM reactions r JOIN users u ON u.id=r.user_id WHERE r.message_id IN ({q})", tuple(message_ids))
    rows=cur.fetchall();con.close()
    out={}
    for mid, emo, uname in rows:
        out.setdefault(mid, {}).setdefault(emo, []).append(uname)
    return out

WS_SESS={}
GUILD_MEMBERS={}

def key(ws): return id(ws)

async def ws_send(ws, obj):
    await ws.send(json.dumps(obj))

async def guild_broadcast(gid, obj):
    if gid not in GUILD_MEMBERS:
        return
    data=json.dumps(obj)
    for ws in list(GUILD_MEMBERS[gid]):
        try:
            await ws.send(data)
        except:
            pass

async def handle(ws:WebSocketServerProtocol):
    try:
        async for raw in ws:
            try:
                m=json.loads(raw)
            except:
                continue
            t=m.get("t")

            if t=="register":
                ok,err=create_user(m.get("username"), m.get("password"))
                await ws_send(ws, {"t":"auth","ok":ok,"err":err})
                continue

            if t=="login":
                r=check_login(m.get("username"), m.get("password"))
                if not r:
                    await ws_send(ws, {"t":"auth","ok":False})
                    continue
                uid, un, av = r
                con=db_con();cur=con.cursor()
                cur.execute("SELECT id FROM guilds ORDER BY id LIMIT 1")
                gid=cur.fetchone()[0]
                con.close()
                ensure_membership(uid, gid)
                WS_SESS[key(ws)]={"uid":uid,"username":un,"gid":gid}
                GUILD_MEMBERS.setdefault(gid,set()).add(ws)
                await ws_send(ws, {"t":"auth","ok":True,"user":{"id":uid,"username":un},"guilds":list_guilds_for(uid),"gid":gid,"channels":list_channels(gid)})
                continue

            s=WS_SESS.get(key(ws))
            if not s:
                continue

            if t=="message":
                cid=int(m.get("cid") or 0)
                txt=str(m.get("text") or "")[:MAX_TEXT]
                mid=add_message(cid, s["uid"], txt)
                await guild_broadcast(s["gid"], {"t":"message","cid":cid,"message":{"id":mid,"ts":int(time.time()),"content":txt,"username":s["username"]}})
                continue

    except:
        pass

async def main():
    db_init()
    async with websockets.serve(handle, HOST, WS_PORT, max_size=32*1024*1024):
        print("Navcord Render Server running on", WS_PORT)
        await asyncio.Future()

asyncio.run(main())
