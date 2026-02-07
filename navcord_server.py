import asyncio, json, os, time, sqlite3, base64, tempfile, bcrypt, websockets
from websockets.server import WebSocketServerProtocol

APP="Navcord"
def app_dir():
    p=os.path.join(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or tempfile.gettempdir(), APP)
    os.makedirs(p, exist_ok=True)
    return p

ROOT=app_dir()
DB=os.path.join(ROOT,"navcord.db")
AV_DIR=os.path.join(ROOT,"avatars")
FILE_DIR=os.path.join(ROOT,"files")

HOST=os.getenv("NAVCORD_HOST","0.0.0.0")
WS_PORT=int(os.getenv("NAVCORD_WS_PORT","8765"))
UDP_PORT=int(os.getenv("NAVCORD_UDP_PORT","9999"))

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

def last_messages(channel_id, limit=50):
    con=db_con();cur=con.cursor()
    cur.execute("""
        SELECT m.id,m.ts,m.content,m.kind,m.meta,u.username,u.avatar,u.id
        FROM messages m JOIN users u ON u.id=m.user_id
        WHERE m.channel_id=?
        ORDER BY m.id DESC LIMIT ?
    """, (channel_id, limit))
    rows=cur.fetchall()
    con.close()
    rows=list(reversed(rows))
    out=[]
    for (mid,ts,ct,kind,meta,un,av,uid) in rows:
        try: meta=json.loads(meta or "{}")
        except: meta={}
        out.append({"id":mid,"ts":ts,"content":ct,"kind":kind,"meta":meta,"username":un,"avatar":av,"user_id":uid})
    return out

def reactions_for_messages(message_ids):
    if not message_ids:
        return {}
    con=db_con();cur=con.cursor()
    q=",".join("?" for _ in message_ids)
    cur.execute(f"""
        SELECT r.message_id,r.emoji,u.username
        FROM reactions r JOIN users u ON u.id=r.user_id
        WHERE r.message_id IN ({q})
    """, tuple(message_ids))
    rows=cur.fetchall();con.close()
    out={}
    for mid, emo, uname in rows:
        out.setdefault(mid, {}).setdefault(emo, []).append(uname)
    return out

def toggle_reaction(mid, uid, emoji):
    emoji=str(emoji or "")[:24]
    if not emoji:
        return
    con=db_con();cur=con.cursor()
    cur.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid, uid, emoji))
    r=cur.fetchone()
    if r:
        cur.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid, uid, emoji))
    else:
        cur.execute("INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)", (mid, uid, emoji))
    con.commit();con.close()

def set_avatar(uid, b64png):
    try:
        data=base64.b64decode(b64png.encode(), validate=True)
    except:
        return None
    if len(data)>MAX_AVATAR:
        return None
    fn=f"u{uid}_{int(time.time())}.png"
    path=os.path.join(AV_DIR, fn)
    with open(path, "wb") as f:
        f.write(data)
    con=db_con();cur=con.cursor()
    cur.execute("UPDATE users SET avatar=? WHERE id=?", (fn, uid))
    con.commit();con.close()
    return fn

def save_file(uid, name, b64):
    name=clean_name(name, 64)
    if not name:
        name="file"
    try:
        data=base64.b64decode(b64.encode(), validate=True)
    except:
        return None
    if len(data)>MAX_FILE:
        return None
    fid=f"f{uid}_{int(time.time())}_{os.urandom(4).hex()}"
    path=os.path.join(FILE_DIR, fid)
    with open(path,"wb") as f:
        f.write(data)
    return {"id":fid,"name":name,"size":len(data)}

def load_file(fid):
    path=os.path.join(FILE_DIR, fid)
    if not os.path.isfile(path):
        return None
    with open(path,"rb") as f:
        return f.read()

WS_SESS={}
GUILD_MEMBERS={}
VOICE_MEMBERS={}
USER_RATE={}

def key(ws): return id(ws)

async def ws_send(ws, obj):
    await ws.send(json.dumps(obj))

async def guild_broadcast(gid, obj):
    if gid not in GUILD_MEMBERS:
        return
    dead=[]
    data=json.dumps(obj)
    for ws in list(GUILD_MEMBERS[gid]):
        try:
            await ws.send(data)
        except:
            dead.append(ws)
    for ws in dead:
        await cleanup(ws)

def in_rate(uid):
    now=time.time()
    bucket=USER_RATE.setdefault(uid, [])
    bucket[:] = [t for t in bucket if now-t<MAX_MSG_RATE_WINDOW]
    if len(bucket)>=MAX_MSG_RATE_COUNT:
        return True
    bucket.append(now)
    return False

async def push_presence(gid):
    members=[]
    for ws in list(GUILD_MEMBERS.get(gid, set())):
        s=WS_SESS.get(key(ws))
        if s:
            members.append({"username":s["username"],"user_id":s["uid"],"avatar":s.get("avatar")})
    members.sort(key=lambda x:x["username"].lower())
    voice={}
    for cid, names in VOICE_MEMBERS.items():
        voice[str(cid)]=sorted(list(names))
    await guild_broadcast(gid, {"t":"presence","gid":gid,"members":members,"voice":voice})

async def cleanup(ws):
    s=WS_SESS.pop(key(ws), None)
    if not s:
        return
    gid=s.get("gid")
    if gid and gid in GUILD_MEMBERS:
        GUILD_MEMBERS[gid].discard(ws)
        if not GUILD_MEMBERS[gid]:
            GUILD_MEMBERS.pop(gid, None)
    vc=s.get("voice_cid")
    if vc:
        VOICE_MEMBERS.get(vc,set()).discard(s["username"])
        if vc in VOICE_MEMBERS and not VOICE_MEMBERS[vc]:
            VOICE_MEMBERS.pop(vc, None)
    if gid:
        await push_presence(gid)

UDP_CLIENTS={}
class VoiceRelay(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport=transport
    def datagram_received(self, data, addr):
        try:
            if len(data)<8:
                return
            cid=int.from_bytes(data[0:4],"big",signed=False)
            n=int.from_bytes(data[4:6],"big",signed=False)
            if 6+n>len(data):
                return
            uname=data[6:6+n].decode("utf-8","ignore")
            UDP_CLIENTS[uname]=addr
            targets=VOICE_MEMBERS.get(cid,set())
            for u in list(targets):
                if u==uname:
                    continue
                a=UDP_CLIENTS.get(u)
                if a:
                    self.transport.sendto(data, a)
        except:
            return

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
                username=clean_name(m.get("username"),24)
                password=str(m.get("password") or "")
                r=check_login(username, password)
                if not r:
                    await ws_send(ws, {"t":"auth","ok":False,"err":"bad"})
                    continue
                uid, un, av = r
                con=db_con();cur=con.cursor()
                cur.execute("SELECT id FROM guilds ORDER BY id LIMIT 1")
                gid=cur.fetchone()[0]
                con.close()
                ensure_membership(uid, gid)
                WS_SESS[key(ws)]={"uid":uid,"username":un,"avatar":av,"gid":gid,"text_cid":None,"voice_cid":None}
                GUILD_MEMBERS.setdefault(gid,set()).add(ws)
                guilds=list_guilds_for(uid)
                chans=list_channels(gid)
                await ws_send(ws, {"t":"auth","ok":True,"user":{"id":uid,"username":un,"avatar":av},"guilds":guilds,"gid":gid,"channels":chans,"udp_port":UDP_PORT})
                await push_presence(gid)
                continue

            s=WS_SESS.get(key(ws))
            if not s:
                await ws_send(ws, {"t":"err","err":"not_authed"})
                continue

            if t=="select_guild":
                gid=int(m.get("gid") or 0)
                ensure_membership(s["uid"], gid)
                old=s["gid"]
                if old in GUILD_MEMBERS:
                    GUILD_MEMBERS[old].discard(ws)
                s["gid"]=gid
                s["text_cid"]=None
                s["voice_cid"]=None
                GUILD_MEMBERS.setdefault(gid,set()).add(ws)
                await ws_send(ws, {"t":"guild_data","gid":gid,"channels":list_channels(gid)})
                await push_presence(old)
                await push_presence(gid)
                continue

            if t=="create_guild":
                name=clean_name(m.get("name"),32)
                gid=create_guild(name)
                if not gid:
                    continue
                ensure_membership(s["uid"], gid)
                await ws_send(ws, {"t":"guild_created","gid":gid,"guilds":list_guilds_for(s["uid"])})
                continue

            if t=="select_channel":
                cid=int(m.get("cid") or 0)
                s["text_cid"]=cid
                msgs=last_messages(cid, 50)
                rx=reactions_for_messages([x["id"] for x in msgs])
                await ws_send(ws, {"t":"history","cid":cid,"messages":msgs,"reactions":rx})
                continue

            if t=="message":
                cid=int(m.get("cid") or 0)
                txt=str(m.get("text") or "")[:MAX_TEXT]
                if not txt.strip() or in_rate(s["uid"]):
                    continue
                mid=add_message(cid, s["uid"], txt, "text", {})
                payload={"t":"message","cid":cid,"message":{"id":mid,"ts":int(time.time()),"content":txt,"kind":"text","meta":{},"username":s["username"],"avatar":s.get("avatar"),"user_id":s["uid"]}}
                await guild_broadcast(s["gid"], payload)
                continue

            if t=="file_send":
                cid=int(m.get("cid") or 0)
                name=str(m.get("name") or "")[:96]
                b64=str(m.get("b64") or "")
                if in_rate(s["uid"]):
                    continue
                meta=save_file(s["uid"], name, b64)
                if not meta:
                    await ws_send(ws, {"t":"file_send","ok":False})
                    continue
                mid=add_message(cid, s["uid"], "[file]", "file", meta)
                payload={"t":"message","cid":cid,"message":{"id":mid,"ts":int(time.time()),"content":"[file]","kind":"file","meta":meta,"username":s["username"],"avatar":s.get("avatar"),"user_id":s["uid"]}}
                await guild_broadcast(s["gid"], payload)
                await ws_send(ws, {"t":"file_send","ok":True,"meta":meta})
                continue

            if t=="file_get":
                fid=str(m.get("id") or "")
                data=load_file(fid)
                if data is None:
                    await ws_send(ws, {"t":"file_data","ok":False,"id":fid})
                    continue
                await ws_send(ws, {"t":"file_data","ok":True,"id":fid,"b64":base64.b64encode(data).decode()})
                continue

            if t=="react":
                mid=int(m.get("mid") or 0)
                emoji=str(m.get("emoji") or "")[:24]
                if not mid or not emoji:
                    continue
                toggle_reaction(mid, s["uid"], emoji)
                rx=reactions_for_messages([mid]).get(mid, {})
                await guild_broadcast(s["gid"], {"t":"reactions","mid":mid,"reactions":rx})
                continue

            if t=="avatar_set":
                b64=str(m.get("png_b64") or "")
                fn=set_avatar(s["uid"], b64)
                if not fn:
                    await ws_send(ws, {"t":"avatar_set","ok":False})
                    continue
                s["avatar"]=fn
                await ws_send(ws, {"t":"avatar_set","ok":True,"avatar":fn})
                await push_presence(s["gid"])
                continue

            if t=="avatar_get":
                fn=str(m.get("avatar") or "")
                path=os.path.join(AV_DIR, fn)
                if not fn or not os.path.isfile(path):
                    await ws_send(ws, {"t":"avatar_data","avatar":fn,"ok":False})
                    continue
                with open(path,"rb") as f:
                    data=f.read()
                await ws_send(ws, {"t":"avatar_data","avatar":fn,"ok":True,"png_b64":base64.b64encode(data).decode()})
                continue

            if t=="voice_join":
                cid=int(m.get("cid") or 0)
                old=s.get("voice_cid")
                if old:
                    VOICE_MEMBERS.get(old,set()).discard(s["username"])
                s["voice_cid"]=cid
                VOICE_MEMBERS.setdefault(cid,set()).add(s["username"])
                await ws_send(ws, {"t":"voice_cfg","udp_port":UDP_PORT,"cid":cid})
                await push_presence(s["gid"])
                continue

            if t=="voice_leave":
                old=s.get("voice_cid")
                if old:
                    VOICE_MEMBERS.get(old,set()).discard(s["username"])
                    if old in VOICE_MEMBERS and not VOICE_MEMBERS[old]:
                        VOICE_MEMBERS.pop(old, None)
                s["voice_cid"]=None
                await push_presence(s["gid"])
                continue
    except:
        pass
    finally:
        await cleanup(ws)

async def main():
    db_init()
    loop=asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda:VoiceRelay(), local_addr=(HOST, UDP_PORT))
    async with websockets.serve(handle, HOST, WS_PORT, ping_interval=25, max_size=32*1024*1024):
        print(f"{APP} Server WS ws://{HOST}:{WS_PORT} | Voice UDP {UDP_PORT} | Data {ROOT}")
        await asyncio.Future()

asyncio.run(main())
