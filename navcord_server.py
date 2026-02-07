import asyncio, json, os, time, sqlite3, base64, tempfile, bcrypt, websockets

HOST="0.0.0.0"
PORT=int(os.getenv("PORT","10000"))

APP="Navcord"
def data_dir():
    p=os.path.join(os.getenv("RENDER_DATA_DIR") or os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or tempfile.gettempdir(), APP)
    os.makedirs(p, exist_ok=True)
    return p

ROOT=data_dir()
DB=os.path.join(ROOT,"navcord.db")
FILES=os.path.join(ROOT,"files")
AV=os.path.join(ROOT,"avatars")
os.makedirs(FILES, exist_ok=True)
os.makedirs(AV, exist_ok=True)

MAX_TEXT=4000
MAX_FILE=25*1024*1024
MAX_AVATAR=512*1024

def db():
    return sqlite3.connect(DB)

def init_db():
    con=db();cur=con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, pw BLOB, avatar TEXT, created_at INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS guilds(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, icon TEXT, created_at INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS memberships(user_id INTEGER, guild_id INTEGER, role TEXT, PRIMARY KEY(user_id,guild_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, kind TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, user_id INTEGER, ts INTEGER, kind TEXT, content TEXT, meta TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER, user_id INTEGER, emoji TEXT, PRIMARY KEY(message_id,user_id,emoji))")
    cur.execute("CREATE TABLE IF NOT EXISTS dms(id INTEGER PRIMARY KEY AUTOINCREMENT, a INTEGER, b INTEGER, created_at INTEGER, UNIQUE(a,b))")
    cur.execute("CREATE TABLE IF NOT EXISTS dm_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, dm_id INTEGER, user_id INTEGER, ts INTEGER, kind TEXT, content TEXT, meta TEXT)")
    con.commit()
    cur.execute("SELECT id FROM guilds ORDER BY id LIMIT 1")
    r=cur.fetchone()
    if not r:
        cur.execute("INSERT INTO guilds(name,icon,created_at) VALUES(?,?,?)", ("Home", None, int(time.time())))
        gid=cur.lastrowid
        cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)", (gid,"general","text"))
        cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)", (gid,"random","text"))
        cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)", (gid,"Lounge","voice"))
        cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)", (gid,"Music","voice"))
        con.commit()
    con.close()

def clean_name(s, n=32):
    s=(s or "").strip()
    ok=[]
    for ch in s:
        if ch.isalnum() or ch in (" ","_","-",".","#"):
            ok.append(ch)
    s="".join(ok).strip()
    return s[:n] if s else ""

def create_user(username, pw):
    username=clean_name(username,24)
    pw=str(pw or "")
    if not username or len(pw)<4:
        return False,"bad"
    h=bcrypt.hashpw(pw.encode(), bcrypt.gensalt())
    con=db();cur=con.cursor()
    try:
        cur.execute("INSERT INTO users(username,pw,avatar,created_at) VALUES(?,?,?,?)", (username,h,None,int(time.time())))
        con.commit()
        return True,None
    except sqlite3.IntegrityError:
        return False,"exists"
    finally:
        con.close()

def check_login(username, pw):
    username=clean_name(username,24)
    pw=str(pw or "")
    con=db();cur=con.cursor()
    cur.execute("SELECT id,pw,avatar FROM users WHERE username=?", (username,))
    r=cur.fetchone();con.close()
    if not r:
        return None
    uid,h,av=r
    try:
        if bcrypt.checkpw(pw.encode(), h):
            return uid,username,av
    except:
        return None
    return None

def user_by_name(username):
    username=clean_name(username,24)
    con=db();cur=con.cursor()
    cur.execute("SELECT id,username,avatar FROM users WHERE username=?", (username,))
    r=cur.fetchone();con.close()
    return r

def ensure_membership(uid, gid):
    con=db();cur=con.cursor()
    cur.execute("INSERT OR IGNORE INTO memberships(user_id,guild_id,role) VALUES(?,?,?)", (uid,gid,"member"))
    con.commit();con.close()

def list_guilds(uid):
    con=db();cur=con.cursor()
    cur.execute("""
        SELECT g.id,g.name,g.icon
        FROM guilds g JOIN memberships m ON m.guild_id=g.id
        WHERE m.user_id=?
        ORDER BY g.id
    """,(uid,))
    rows=cur.fetchall();con.close()
    return [{"id":i,"name":n,"icon":ic} for (i,n,ic) in rows]

def list_channels(gid):
    con=db();cur=con.cursor()
    cur.execute("SELECT id,name,kind FROM channels WHERE guild_id=? ORDER BY kind DESC,id",(gid,))
    rows=cur.fetchall();con.close()
    return [{"id":i,"name":n,"kind":k} for (i,n,k) in rows]

def create_guild(name):
    name=clean_name(name,32)
    if not name:
        return None
    con=db();cur=con.cursor()
    cur.execute("INSERT INTO guilds(name,icon,created_at) VALUES(?,?,?)", (name,None,int(time.time())))
    gid=cur.lastrowid
    cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)",(gid,"general","text"))
    cur.execute("INSERT INTO channels(guild_id,name,kind) VALUES(?,?,?)",(gid,"Lounge","voice"))
    con.commit();con.close()
    return gid

def add_message(cid, uid, kind, content, meta):
    con=db();cur=con.cursor()
    cur.execute("INSERT INTO messages(channel_id,user_id,ts,kind,content,meta) VALUES(?,?,?,?,?,?)",
                (cid,uid,int(time.time()),kind,content,json.dumps(meta or {})))
    mid=cur.lastrowid
    con.commit();con.close()
    return mid

def last_messages(cid, limit=80):
    con=db();cur=con.cursor()
    cur.execute("""
        SELECT m.id,m.ts,m.kind,m.content,m.meta,u.username,u.avatar,u.id
        FROM messages m JOIN users u ON u.id=m.user_id
        WHERE m.channel_id=?
        ORDER BY m.id DESC LIMIT ?
    """,(cid,limit))
    rows=list(reversed(cur.fetchall()))
    con.close()
    out=[]
    for mid,ts,k,ct,meta,un,av,uid in rows:
        try: meta=json.loads(meta or "{}")
        except: meta={}
        out.append({"id":mid,"ts":ts,"kind":k,"content":ct,"meta":meta,"username":un,"avatar":av,"user_id":uid})
    return out

def reactions_for(mids):
    if not mids:
        return {}
    con=db();cur=con.cursor()
    q=",".join("?" for _ in mids)
    cur.execute(f"SELECT r.message_id,r.emoji,u.username FROM reactions r JOIN users u ON u.id=r.user_id WHERE r.message_id IN ({q})", tuple(mids))
    rows=cur.fetchall();con.close()
    out={}
    for mid,emo,un in rows:
        out.setdefault(mid,{}).setdefault(emo,[]).append(un)
    return out

def toggle_reaction(mid, uid, emoji):
    emoji=str(emoji or "")[:24]
    if not emoji:
        return
    con=db();cur=con.cursor()
    cur.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid,uid,emoji))
    r=cur.fetchone()
    if r:
        cur.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid,uid,emoji))
    else:
        cur.execute("INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)", (mid,uid,emoji))
    con.commit();con.close()

def set_avatar(uid, b64png):
    try:
        data=base64.b64decode((b64png or "").encode(), validate=True)
    except:
        return None
    if len(data)>MAX_AVATAR:
        return None
    fn=f"u{uid}_{int(time.time())}.png"
    path=os.path.join(AV, fn)
    with open(path,"wb") as f:
        f.write(data)
    con=db();cur=con.cursor()
    cur.execute("UPDATE users SET avatar=? WHERE id=?", (fn,uid))
    con.commit();con.close()
    return fn

def avatar_get(fn):
    if not fn:
        return None
    path=os.path.join(AV, fn)
    if not os.path.isfile(path):
        return None
    with open(path,"rb") as f:
        return f.read()

def save_file(uid, name, b64):
    name=clean_name(name,80) or "file"
    try:
        data=base64.b64decode((b64 or "").encode(), validate=True)
    except:
        return None
    if len(data)>MAX_FILE:
        return None
    fid=f"f{uid}_{int(time.time())}_{os.urandom(4).hex()}"
    path=os.path.join(FILES, fid)
    with open(path,"wb") as f:
        f.write(data)
    return {"id":fid,"name":name,"size":len(data)}

def load_file(fid):
    path=os.path.join(FILES, fid or "")
    if not os.path.isfile(path):
        return None
    with open(path,"rb") as f:
        return f.read()

def dm_pair(a,b):
    a=int(a);b=int(b)
    return (a,b) if a<b else (b,a)

def ensure_dm(uid, other_uid):
    a,b=dm_pair(uid, other_uid)
    con=db();cur=con.cursor()
    cur.execute("SELECT id FROM dms WHERE a=? AND b=?", (a,b))
    r=cur.fetchone()
    if r:
        con.close()
        return r[0]
    cur.execute("INSERT INTO dms(a,b,created_at) VALUES(?,?,?)",(a,b,int(time.time())))
    did=cur.lastrowid
    con.commit();con.close()
    return did

def list_dms(uid):
    uid=int(uid)
    con=db();cur=con.cursor()
    cur.execute("""
        SELECT d.id,
               CASE WHEN d.a=? THEN u2.id ELSE u1.id END,
               CASE WHEN d.a=? THEN u2.username ELSE u1.username END,
               CASE WHEN d.a=? THEN u2.avatar ELSE u1.avatar END
        FROM dms d
        JOIN users u1 ON u1.id=d.a
        JOIN users u2 ON u2.id=d.b
        WHERE d.a=? OR d.b=?
        ORDER BY d.id DESC
    """,(uid,uid,uid,uid,uid))
    rows=cur.fetchall();con.close()
    return [{"id":did,"other":{"id":oid,"username":un,"avatar":av}} for (did,oid,un,av) in rows]

def add_dm_message(did, uid, kind, content, meta):
    con=db();cur=con.cursor()
    cur.execute("INSERT INTO dm_messages(dm_id,user_id,ts,kind,content,meta) VALUES(?,?,?,?,?,?)",
                (did,uid,int(time.time()),kind,content,json.dumps(meta or {})))
    mid=cur.lastrowid
    con.commit();con.close()
    return mid

def last_dm_messages(did, limit=100):
    con=db();cur=con.cursor()
    cur.execute("""
        SELECT m.id,m.ts,m.kind,m.content,m.meta,u.username,u.avatar,u.id
        FROM dm_messages m JOIN users u ON u.id=m.user_id
        WHERE m.dm_id=?
        ORDER BY m.id DESC LIMIT ?
    """,(did,limit))
    rows=list(reversed(cur.fetchall()))
    con.close()
    out=[]
    for mid,ts,k,ct,meta,un,av,uid in rows:
        try: meta=json.loads(meta or "{}")
        except: meta={}
        out.append({"id":mid,"ts":ts,"kind":k,"content":ct,"meta":meta,"username":un,"avatar":av,"user_id":uid})
    return out

SESS={}
USER_SOCKS={}
GUILD_SOCKS={}
VOICE_ROOMS={}

def sid(ws): return id(ws)

async def send(ws,obj):
    await ws.send(json.dumps(obj))

async def fanout(sockset,obj):
    if not sockset:
        return
    data=json.dumps(obj)
    dead=[]
    for w in list(sockset):
        try:
            await w.send(data)
        except:
            dead.append(w)
    for w in dead:
        sockset.discard(w)

async def send_uid(uid,obj):
    await fanout(USER_SOCKS.get(uid,set()), obj)

async def guild_broadcast(gid,obj):
    await fanout(GUILD_SOCKS.get(gid,set()), obj)

async def voice_room_broadcast(room_id, obj, exclude=None):
    ss=VOICE_ROOMS.get(room_id,set())
    if exclude is not None:
        ss=set([w for w in ss if w!=exclude])
    await fanout(ss, obj)

async def presence_push(gid):
    members=[]
    for w in list(GUILD_SOCKS.get(gid,set())):
        s=SESS.get(sid(w))
        if s:
            members.append({"id":s["uid"],"username":s["username"],"avatar":s.get("avatar")})
    members.sort(key=lambda x:x["username"].lower())
    await guild_broadcast(gid, {"t":"presence","gid":gid,"members":members})

async def cleanup(ws):
    s=SESS.pop(sid(ws),None)
    if not s:
        return
    uid=s.get("uid")
    gid=s.get("gid")
    vr=s.get("voice_room")
    if uid in USER_SOCKS:
        USER_SOCKS[uid].discard(ws)
        if not USER_SOCKS[uid]:
            USER_SOCKS.pop(uid,None)
    if gid in GUILD_SOCKS:
        GUILD_SOCKS[gid].discard(ws)
        if not GUILD_SOCKS[gid]:
            GUILD_SOCKS.pop(gid,None)
    if vr is not None:
        room=VOICE_ROOMS.get(vr,set())
        room.discard(ws)
        if not room:
            VOICE_ROOMS.pop(vr,None)
        else:
            await voice_room_broadcast(vr, {"t":"voice_peer_left","room":vr,"peer":uid})
    if gid:
        await presence_push(gid)

async def handle(ws):
    try:
        async for raw in ws:
            try:
                m=json.loads(raw)
            except:
                continue
            t=m.get("t")

            if t=="register":
                ok,err=create_user(m.get("username"), m.get("password"))
                await send(ws, {"t":"auth","ok":ok,"err":err})
                continue

            if t=="login":
                r=check_login(m.get("username"), m.get("password"))
                if not r:
                    await send(ws, {"t":"auth","ok":False,"err":"bad"})
                    continue
                uid,un,av=r
                con=db();cur=con.cursor()
                cur.execute("SELECT id FROM guilds ORDER BY id LIMIT 1")
                gid=cur.fetchone()[0]
                con.close()
                ensure_membership(uid,gid)
                SESS[sid(ws)]={"uid":uid,"username":un,"avatar":av,"gid":gid,"voice_room":None}
                USER_SOCKS.setdefault(uid,set()).add(ws)
                GUILD_SOCKS.setdefault(gid,set()).add(ws)
                await send(ws, {"t":"auth","ok":True,"user":{"id":uid,"username":un,"avatar":av},"gid":gid,"guilds":list_guilds(uid),"channels":list_channels(gid),"dms":list_dms(uid)})
                await presence_push(gid)
                continue

            s=SESS.get(sid(ws))
            if not s:
                await send(ws, {"t":"err","err":"not_authed"})
                continue

            if t=="select_guild":
                gid=int(m.get("gid") or 0)
                ensure_membership(s["uid"],gid)
                old=s["gid"]
                if old in GUILD_SOCKS:
                    GUILD_SOCKS[old].discard(ws)
                s["gid"]=gid
                GUILD_SOCKS.setdefault(gid,set()).add(ws)
                await send(ws, {"t":"guild_data","gid":gid,"channels":list_channels(gid)})
                await presence_push(old)
                await presence_push(gid)
                continue

            if t=="create_guild":
                gid=create_guild(m.get("name"))
                if gid:
                    ensure_membership(s["uid"],gid)
                    await send(ws, {"t":"guild_created","gid":gid,"guilds":list_guilds(s["uid"])})
                continue

            if t=="select_channel":
                cid=int(m.get("cid") or 0)
                msgs=last_messages(cid,80)
                rx=reactions_for([x["id"] for x in msgs])
                await send(ws, {"t":"history","mode":"guild","cid":cid,"messages":msgs,"reactions":rx})
                continue

            if t=="message":
                cid=int(m.get("cid") or 0)
                txt=str(m.get("text") or "")[:MAX_TEXT]
                if not txt.strip():
                    continue
                mid=add_message(cid,s["uid"],"text",txt,{})
                payload={"t":"message","mode":"guild","cid":cid,"message":{"id":mid,"ts":int(time.time()),"kind":"text","content":txt,"meta":{},"username":s["username"],"avatar":s.get("avatar"),"user_id":s["uid"]}}
                await guild_broadcast(s["gid"], payload)
                continue

            if t=="file_send":
                cid=int(m.get("cid") or 0)
                meta=save_file(s["uid"], m.get("name"), m.get("b64"))
                if not meta:
                    await send(ws, {"t":"file_send","ok":False})
                    continue
                mid=add_message(cid,s["uid"],"file","[file]",meta)
                payload={"t":"message","mode":"guild","cid":cid,"message":{"id":mid,"ts":int(time.time()),"kind":"file","content":"[file]","meta":meta,"username":s["username"],"avatar":s.get("avatar"),"user_id":s["uid"]}}
                await guild_broadcast(s["gid"], payload)
                await send(ws, {"t":"file_send","ok":True,"meta":meta})
                continue

            if t=="file_get":
                fid=str(m.get("id") or "")
                data=load_file(fid)
                if data is None:
                    await send(ws, {"t":"file_data","ok":False,"id":fid})
                    continue
                await send(ws, {"t":"file_data","ok":True,"id":fid,"b64":base64.b64encode(data).decode()})
                continue

            if t=="react":
                mid=int(m.get("mid") or 0)
                emoji=str(m.get("emoji") or "")[:24]
                if not mid or not emoji:
                    continue
                toggle_reaction(mid, s["uid"], emoji)
                rx=reactions_for([mid]).get(mid,{})
                await guild_broadcast(s["gid"], {"t":"reactions","mid":mid,"reactions":rx})
                continue

            if t=="avatar_set":
                fn=set_avatar(s["uid"], m.get("png_b64"))
                if not fn:
                    await send(ws, {"t":"avatar_set","ok":False})
                    continue
                s["avatar"]=fn
                await send(ws, {"t":"avatar_set","ok":True,"avatar":fn})
                await presence_push(s["gid"])
                continue

            if t=="avatar_get":
                fn=str(m.get("avatar") or "")
                data=avatar_get(fn)
                if data is None:
                    await send(ws, {"t":"avatar_data","ok":False,"avatar":fn})
                    continue
                await send(ws, {"t":"avatar_data","ok":True,"avatar":fn,"png_b64":base64.b64encode(data).decode()})
                continue

            if t=="dm_open":
                u=user_by_name(m.get("username"))
                if not u:
                    await send(ws, {"t":"dm_open","ok":False,"err":"no_user"})
                    continue
                ouid,oun,oav=u
                if ouid==s["uid"]:
                    await send(ws, {"t":"dm_open","ok":False,"err":"self"})
                    continue
                did=ensure_dm(s["uid"],ouid)
                msgs=last_dm_messages(did,100)
                await send(ws, {"t":"history","mode":"dm","dm_id":did,"other":{"id":ouid,"username":oun,"avatar":oav},"messages":msgs,"reactions":{}})
                await send(ws, {"t":"dm_list","dms":list_dms(s["uid"])})
                continue

            if t=="dm_select":
                did=int(m.get("dm_id") or 0)
                con=db();cur=con.cursor()
                cur.execute("""
                    SELECT d.a,d.b,u1.id,u1.username,u1.avatar,u2.id,u2.username,u2.avatar
                    FROM dms d
                    JOIN users u1 ON u1.id=d.a
                    JOIN users u2 ON u2.id=d.b
                    WHERE d.id=?
                """,(did,))
                r=cur.fetchone();con.close()
                if not r:
                    continue
                a,b,u1id,u1n,u1a,u2id,u2n,u2a=r
                if s["uid"]==u1id:
                    other={"id":u2id,"username":u2n,"avatar":u2a}
                elif s["uid"]==u2id:
                    other={"id":u1id,"username":u1n,"avatar":u1a}
                else:
                    continue
                msgs=last_dm_messages(did,100)
                await send(ws, {"t":"history","mode":"dm","dm_id":did,"other":other,"messages":msgs,"reactions":{}})
                continue

            if t=="dm_message":
                did=int(m.get("dm_id") or 0)
                txt=str(m.get("text") or "")[:MAX_TEXT]
                if not txt.strip():
                    continue
                con=db();cur=con.cursor()
                cur.execute("SELECT a,b FROM dms WHERE id=?", (did,))
                r=cur.fetchone();con.close()
                if not r:
                    continue
                a,b=r
                if s["uid"] not in (a,b):
                    continue
                mid=add_dm_message(did,s["uid"],"text",txt,{})
                payload={"t":"message","mode":"dm","dm_id":did,"message":{"id":mid,"ts":int(time.time()),"kind":"text","content":txt,"meta":{},"username":s["username"],"avatar":s.get("avatar"),"user_id":s["uid"]}}
                await send_uid(a,payload)
                await send_uid(b,payload)
                continue

            if t=="dm_file_send":
                did=int(m.get("dm_id") or 0)
                con=db();cur=con.cursor()
                cur.execute("SELECT a,b FROM dms WHERE id=?", (did,))
                r=cur.fetchone();con.close()
                if not r:
                    continue
                a,b=r
                if s["uid"] not in (a,b):
                    continue
                meta=save_file(s["uid"], m.get("name"), m.get("b64"))
                if not meta:
                    await send(ws, {"t":"dm_file_send","ok":False})
                    continue
                mid=add_dm_message(did,s["uid"],"file","[file]",meta)
                payload={"t":"message","mode":"dm","dm_id":did,"message":{"id":mid,"ts":int(time.time()),"kind":"file","content":"[file]","meta":meta,"username":s["username"],"avatar":s.get("avatar"),"user_id":s["uid"]}}
                await send_uid(a,payload)
                await send_uid(b,payload)
                await send(ws, {"t":"dm_file_send","ok":True,"meta":meta})
                continue

            if t=="dm_list":
                await send(ws, {"t":"dm_list","dms":list_dms(s["uid"])})
                continue

            if t=="voice_join":
                room=int(m.get("room") or 0)
                old=s.get("voice_room")
                if old is not None:
                    rset=VOICE_ROOMS.get(old,set())
                    rset.discard(ws)
                    if not rset:
                        VOICE_ROOMS.pop(old,None)
                    else:
                        await voice_room_broadcast(old, {"t":"voice_peer_left","room":old,"peer":s["uid"]}, exclude=ws)
                s["voice_room"]=room
                VOICE_ROOMS.setdefault(room,set()).add(ws)
                peers=[]
                for w in list(VOICE_ROOMS.get(room,set())):
                    if w==ws:
                        continue
                    ss=SESS.get(sid(w))
                    if ss:
                        peers.append({"id":ss["uid"],"username":ss["username"]})
                await send(ws, {"t":"voice_joined","room":room,"self":s["uid"],"peers":peers})
                await voice_room_broadcast(room, {"t":"voice_peer_joined","room":room,"peer":{"id":s["uid"],"username":s["username"]}}, exclude=ws)
                continue

            if t=="voice_leave":
                room=s.get("voice_room")
                if room is None:
                    continue
                s["voice_room"]=None
                rset=VOICE_ROOMS.get(room,set())
                rset.discard(ws)
                if not rset:
                    VOICE_ROOMS.pop(room,None)
                else:
                    await voice_room_broadcast(room, {"t":"voice_peer_left","room":room,"peer":s["uid"]}, exclude=ws)
                await send(ws, {"t":"voice_left","room":room})
                continue

            if t in ("rtc_offer","rtc_answer","rtc_ice"):
                room=s.get("voice_room")
                if room is None:
                    continue
                to=int(m.get("to") or 0)
                payload={"t":t,"room":room,"from":s["uid"]}
                if t=="rtc_ice":
                    payload["candidate"]=m.get("candidate")
                else:
                    payload["sdp"]=m.get("sdp")
                for w in list(VOICE_ROOMS.get(room,set())):
                    ss=SESS.get(sid(w))
                    if ss and ss["uid"]==to:
                        await send(w, payload)
                        break
                continue

    except:
        pass
    finally:
        await cleanup(ws)

async def main():
    init_db()
    async with websockets.serve(handle, HOST, PORT, ping_interval=25, max_size=32*1024*1024):
        print("Navcord WS on",PORT,"data",ROOT)
        await asyncio.Future()

asyncio.run(main())
