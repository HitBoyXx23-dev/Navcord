import os, time, json, sqlite3, secrets, re
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import bcrypt

APP_NAME="Navcord"
DB_PATH=os.environ.get("NAVCORD_DB","/tmp/navcord.db")
FILES_DIR=os.environ.get("NAVCORD_FILES_DIR","/tmp/navcord_files")
MAX_FILE_BYTES=int(os.environ.get("NAVCORD_MAX_FILE_BYTES",str(25*1024*1024)))
os.makedirs(FILES_DIR,exist_ok=True)

def now_ms(): return int(time.time()*1000)

def db():
    conn=sqlite3.connect(DB_PATH,check_same_thread=False)
    conn.row_factory=sqlite3.Row
    return conn

def init_db():
    conn=db();cur=conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, passhash BLOB NOT NULL, avatar_file TEXT, created_ms INTEGER NOT NULL);")
    cur.execute("CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_ms INTEGER NOT NULL, last_ms INTEGER NOT NULL);")
    cur.execute("CREATE TABLE IF NOT EXISTS guilds(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_ms INTEGER NOT NULL);")
    cur.execute("CREATE TABLE IF NOT EXISTS memberships(user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, role TEXT NOT NULL, PRIMARY KEY(user_id,guild_id));")
    cur.execute("CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL, created_ms INTEGER NOT NULL, UNIQUE(guild_id,name,type));")
    cur.execute("CREATE TABLE IF NOT EXISTS dms(id INTEGER PRIMARY KEY AUTOINCREMENT, user1 INTEGER NOT NULL, user2 INTEGER NOT NULL, created_ms INTEGER NOT NULL, UNIQUE(user1,user2));")
    cur.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, channel_id INTEGER, dm_id INTEGER, sender_id INTEGER NOT NULL, content TEXT NOT NULL, attachments_json TEXT, created_ms INTEGER NOT NULL);")
    cur.execute("CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, emoji TEXT NOT NULL, created_ms INTEGER NOT NULL, PRIMARY KEY(message_id,user_id,emoji));")
    conn.commit();conn.close()

def hpw(pw:str)->bytes: return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12))
def vpw(pw:str, ph:bytes)->bool:
    try: return bcrypt.checkpw(pw.encode(), ph)
    except Exception: return False
def tok()->str: return secrets.token_urlsafe(32)

def require_user(auth: Optional[str]):
    if not auth or not auth.lower().startswith("bearer "): raise HTTPException(401,"Missing token")
    t=auth.split(" ",1)[1].strip()
    conn=db();cur=conn.cursor()
    cur.execute("SELECT user_id FROM sessions WHERE token=?",(t,))
    r=cur.fetchone()
    if not r:
        conn.close(); raise HTTPException(401,"Invalid token")
    cur.execute("UPDATE sessions SET last_ms=? WHERE token=?",(now_ms(),t))
    conn.commit()
    cur.execute("SELECT id, username, avatar_file FROM users WHERE id=?",(r["user_id"],))
    u=cur.fetchone(); conn.close()
    if not u: raise HTTPException(401,"Invalid user")
    return dict(u)

def safe_name(s:str)->str:
    s=re.sub(r"[^a-zA-Z0-9._-]+","_",s or "file").strip("._")
    return (s[:180] if s else "file")

def store_upload(raw:bytes, filename:str)->str:
    if len(raw)>MAX_FILE_BYTES: raise HTTPException(413,"File too large")
    fid=secrets.token_hex(16)
    fn=safe_name(filename)
    path=os.path.join(FILES_DIR,f"{fid}_{fn}")
    with open(path,"wb") as f: f.write(raw)
    return os.path.basename(path)

def file_path(file_id:str)->str:
    p=os.path.join(FILES_DIR,file_id)
    if not os.path.isfile(p): raise HTTPException(404,"File not found")
    return p

def ensure_defaults(user_id:int):
    conn=db();cur=conn.cursor()
    cur.execute("SELECT id FROM guilds ORDER BY id ASC LIMIT 1")
    g=cur.fetchone()
    if not g:
        cur.execute("INSERT INTO guilds(name,created_ms) VALUES(?,?)",("Navine Server",now_ms()))
        gid=cur.lastrowid
        for nm,tp in [("general","text"),("memes","text"),("Lobby","voice"),("Chill","voice")]:
            cur.execute("INSERT INTO channels(guild_id,name,type,created_ms) VALUES(?,?,?,?)",(gid,nm,tp,now_ms()))
        cur.execute("INSERT INTO memberships(user_id,guild_id,role) VALUES(?,?,?)",(user_id,gid,"admin"))
        conn.commit(); conn.close(); return
    gid=g["id"]
    cur.execute("INSERT OR IGNORE INTO memberships(user_id,guild_id,role) VALUES(?,?,?)",(user_id,gid,"member"))
    conn.commit(); conn.close()

def require_membership(user_id:int,guild_id:int):
    conn=db();cur=conn.cursor()
    cur.execute("SELECT 1 FROM memberships WHERE user_id=? AND guild_id=?",(user_id,guild_id))
    r=cur.fetchone(); conn.close()
    if not r: raise HTTPException(403,"Not a member")

def user_basic(uid:int):
    conn=db();cur=conn.cursor()
    cur.execute("SELECT id, username, avatar_file FROM users WHERE id=?",(uid,))
    r=cur.fetchone(); conn.close()
    if not r: return {"id":uid,"username":"Unknown","avatar_url":None}
    av=r["avatar_file"]
    return {"id":r["id"],"username":r["username"],"avatar_url":(f"/files/{av}" if av else None)}

app=FastAPI(title=APP_NAME)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
init_db()

class Hub:
    def __init__(self):
        import asyncio
        self.lock=asyncio.Lock()
        self.ws_by_user:Dict[int,Set[WebSocket]]={}
        self.subs:Dict[str,Set[WebSocket]]={}
        self.voice:Dict[int,int]={}
    async def add(self,uid:int,ws:WebSocket):
        async with self.lock: self.ws_by_user.setdefault(uid,set()).add(ws)
    async def remove(self,uid:int,ws:WebSocket):
        async with self.lock:
            if uid in self.ws_by_user and ws in self.ws_by_user[uid]:
                self.ws_by_user[uid].remove(ws)
                if not self.ws_by_user[uid]: del self.ws_by_user[uid]
            for k in list(self.subs.keys()):
                if ws in self.subs[k]:
                    self.subs[k].remove(ws)
                    if not self.subs[k]: del self.subs[k]
            if uid in self.voice: del self.voice[uid]
    async def sub(self,scope:str,ws:WebSocket):
        async with self.lock: self.subs.setdefault(scope,set()).add(ws)
    async def unsub_all(self,ws:WebSocket):
        async with self.lock:
            for k in list(self.subs.keys()):
                if ws in self.subs[k]:
                    self.subs[k].remove(ws)
                    if not self.subs[k]: del self.subs[k]
    async def broadcast(self,scope:str,payload:Dict[str,Any]):
        import json
        async with self.lock: conns=list(self.subs.get(scope,set()))
        dead=[]
        for w in conns:
            try: await w.send_text(json.dumps(payload,ensure_ascii=False))
            except Exception: dead.append(w)
        if dead:
            async with self.lock:
                if scope in self.subs:
                    for w in dead: self.subs[scope].discard(w)
                    if not self.subs[scope]: del self.subs[scope]
    async def send_user(self,uid:int,payload:Dict[str,Any]):
        import json
        async with self.lock: conns=list(self.ws_by_user.get(uid,set()))
        dead=[]
        for w in conns:
            try: await w.send_text(json.dumps(payload,ensure_ascii=False))
            except Exception: dead.append(w)
        if dead:
            async with self.lock:
                if uid in self.ws_by_user:
                    for w in dead: self.ws_by_user[uid].discard(w)
                    if not self.ws_by_user[uid]: del self.ws_by_user[uid]
hub=Hub()

def online_ids(): return list(hub.ws_by_user.keys())

@app.get("/health")
async def health(): return {"ok":True,"name":APP_NAME,"ts":now_ms()}

@app.post("/auth/register")
async def register(username:str=Form(...), password:str=Form(...)):
    un=username.strip()
    if not (3<=len(un)<=24): raise HTTPException(400,"Username must be 3-24 chars")
    if len(password)<6: raise HTTPException(400,"Password too short")
    conn=db();cur=conn.cursor()
    try:
        cur.execute("INSERT INTO users(username,passhash,avatar_file,created_ms) VALUES(?,?,?,?)",(un,hpw(password),None,now_ms()))
        uid=cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close(); raise HTTPException(409,"Username already taken")
    conn.close()
    ensure_defaults(uid)
    return {"ok":True}

@app.post("/auth/login")
async def login(username:str=Form(...), password:str=Form(...)):
    un=username.strip()
    conn=db();cur=conn.cursor()
    cur.execute("SELECT id, passhash FROM users WHERE username=?",(un,))
    r=cur.fetchone()
    if not r or not vpw(password,r["passhash"]):
        conn.close(); raise HTTPException(401,"Invalid credentials")
    t=tok()
    cur.execute("INSERT INTO sessions(token,user_id,created_ms,last_ms) VALUES(?,?,?,?)",(t,r["id"],now_ms(),now_ms()))
    conn.commit(); conn.close()
    ensure_defaults(int(r["id"]))
    return {"ok":True,"token":t,"user":user_basic(int(r["id"]))}

@app.get("/me")
async def me(authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization)
    return {"ok":True,"user":user_basic(int(u["id"]))}

@app.post("/auth/logout")
async def logout(authorization:Optional[str]=Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "): return {"ok":True}
    t=authorization.split(" ",1)[1].strip()
    conn=db();cur=conn.cursor()
    cur.execute("DELETE FROM sessions WHERE token=?",(t,))
    conn.commit(); conn.close()
    return {"ok":True}

@app.post("/me/avatar")
async def set_avatar(file:UploadFile=File(...), authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization)
    raw=await file.read()
    stored=store_upload(raw,file.filename or "avatar.png")
    conn=db();cur=conn.cursor()
    cur.execute("UPDATE users SET avatar_file=? WHERE id=?",(stored,int(u["id"])))
    conn.commit(); conn.close()
    return {"ok":True,"avatar_url":f"/files/{stored}"}

@app.get("/guilds")
async def guilds(authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization)
    conn=db();cur=conn.cursor()
    cur.execute("SELECT g.id,g.name,m.role FROM guilds g JOIN memberships m ON m.guild_id=g.id WHERE m.user_id=? ORDER BY g.id ASC",(int(u["id"]),))
    rows=cur.fetchall(); conn.close()
    return {"ok":True,"guilds":[{"id":r["id"],"name":r["name"],"role":r["role"]} for r in rows]}

@app.get("/guilds/{guild_id}/channels")
async def channels(guild_id:int, authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization); require_membership(int(u["id"]),guild_id)
    conn=db();cur=conn.cursor()
    cur.execute("SELECT id,name,type FROM channels WHERE guild_id=? ORDER BY type ASC,name ASC",(guild_id,))
    rows=cur.fetchall(); conn.close()
    out={"text":[],"voice":[]}
    for r in rows: out[r["type"]].append({"id":r["id"],"name":r["name"],"type":r["type"]})
    return {"ok":True,"channels":out}

@app.post("/channels/create")
async def create_channel(guild_id:int=Form(...), name:str=Form(...), type:str=Form(...), authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization); require_membership(int(u["id"]),guild_id)
    nm=name.strip().lower().replace(" ","-")
    tp=type.strip().lower()
    if not nm: raise HTTPException(400,"Bad name")
    if tp not in ("text","voice"): raise HTTPException(400,"Bad type")
    conn=db();cur=conn.cursor()
    try:
        cur.execute("INSERT INTO channels(guild_id,name,type,created_ms) VALUES(?,?,?,?)",(guild_id,nm,tp,now_ms()))
        cid=cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close(); raise HTTPException(409,"Channel exists")
    conn.close()
    await hub.broadcast(f"guild:{guild_id}",{"t":"channel_created","channel":{"id":cid,"guild_id":guild_id,"name":nm,"type":tp}})
    return {"ok":True,"channel_id":cid}

@app.post("/dms/open")
async def open_dm(username:str=Form(...), authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization)
    conn=db();cur=conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?",(username.strip(),))
    tr=cur.fetchone()
    if not tr: conn.close(); raise HTTPException(404,"User not found")
    uid1=int(u["id"]); uid2=int(tr["id"])
    a,b=(uid1,uid2) if uid1<uid2 else (uid2,uid1)
    cur.execute("SELECT id FROM dms WHERE user1=? AND user2=?",(a,b))
    dm=cur.fetchone()
    if not dm:
        cur.execute("INSERT INTO dms(user1,user2,created_ms) VALUES(?,?,?)",(a,b,now_ms()))
        dm_id=cur.lastrowid
        conn.commit()
    else:
        dm_id=dm["id"]
    conn.close()
    return {"ok":True,"dm_id":dm_id,"peer":user_basic(uid2)}

@app.get("/messages/channel/{channel_id}")
async def channel_messages(channel_id:int, limit:int=50, authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization)
    conn=db();cur=conn.cursor()
    cur.execute("SELECT guild_id FROM channels WHERE id=?",(channel_id,))
    ch=cur.fetchone()
    if not ch: conn.close(); raise HTTPException(404,"Channel not found")
    require_membership(int(u["id"]),int(ch["guild_id"]))
    cur.execute("SELECT * FROM messages WHERE scope='channel' AND channel_id=? ORDER BY id DESC LIMIT ?",(channel_id,int(limit)))
    rows=cur.fetchall()
    mids=[r["id"] for r in rows]
    rx={}
    if mids:
        q=",".join("?" for _ in mids)
        cur.execute(f"SELECT message_id,emoji,COUNT(*) AS c FROM reactions WHERE message_id IN ({q}) GROUP BY message_id,emoji",mids)
        for r in cur.fetchall(): rx.setdefault(r["message_id"],[]).append({"emoji":r["emoji"],"count":r["c"]})
    out=[]
    for r in reversed(rows):
        out.append({"id":r["id"],"scope":r["scope"],"channel_id":r["channel_id"],"dm_id":r["dm_id"],"sender":user_basic(int(r["sender_id"])),"content":r["content"],"attachments":json.loads(r["attachments_json"] or "[]"),"created_ms":r["created_ms"],"reactions":rx.get(r["id"],[])})
    conn.close()
    return {"ok":True,"messages":out}

@app.get("/messages/dm/{dm_id}")
async def dm_messages(dm_id:int, limit:int=50, authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization)
    conn=db();cur=conn.cursor()
    cur.execute("SELECT user1,user2 FROM dms WHERE id=?",(dm_id,))
    dm=cur.fetchone()
    if not dm: conn.close(); raise HTTPException(404,"DM not found")
    uid=int(u["id"])
    if uid not in (dm["user1"],dm["user2"]): conn.close(); raise HTTPException(403,"Forbidden")
    cur.execute("SELECT * FROM messages WHERE scope='dm' AND dm_id=? ORDER BY id DESC LIMIT ?",(dm_id,int(limit)))
    rows=cur.fetchall()
    mids=[r["id"] for r in rows]
    rx={}
    if mids:
        q=",".join("?" for _ in mids)
        cur.execute(f"SELECT message_id,emoji,COUNT(*) AS c FROM reactions WHERE message_id IN ({q}) GROUP BY message_id,emoji",mids)
        for r in cur.fetchall(): rx.setdefault(r["message_id"],[]).append({"emoji":r["emoji"],"count":r["c"]})
    out=[]
    for r in reversed(rows):
        out.append({"id":r["id"],"scope":r["scope"],"channel_id":r["channel_id"],"dm_id":r["dm_id"],"sender":user_basic(int(r["sender_id"])),"content":r["content"],"attachments":json.loads(r["attachments_json"] or "[]"),"created_ms":r["created_ms"],"reactions":rx.get(r["id"],[])})
    conn.close()
    return {"ok":True,"messages":out}

@app.post("/files/upload")
async def upload_file(file:UploadFile=File(...), authorization:Optional[str]=Header(default=None)):
    require_user(authorization)
    raw=await file.read()
    stored=store_upload(raw,file.filename or "file.bin")
    return {"ok":True,"file_id":stored,"url":f"/files/{stored}","name":file.filename or "file.bin","bytes":len(raw)}

@app.get("/files/{file_id}")
async def get_file(file_id:str):
    return FileResponse(file_path(file_id), filename=file_id.split("_",1)[-1])

@app.post("/reactions/toggle")
async def toggle_reaction(message_id:int=Form(...), emoji:str=Form(...), authorization:Optional[str]=Header(default=None)):
    u=require_user(authorization)
    em=emoji.strip()
    if not em or len(em)>32: raise HTTPException(400,"Bad emoji")
    conn=db();cur=conn.cursor()
    cur.execute("SELECT scope,channel_id,dm_id FROM messages WHERE id=?",(int(message_id),))
    m=cur.fetchone()
    if not m: conn.close(); raise HTTPException(404,"Message not found")
    scope=m["scope"]
    if scope=="channel":
        cur.execute("SELECT guild_id FROM channels WHERE id=?",(int(m["channel_id"]),))
        ch=cur.fetchone()
        if not ch: conn.close(); raise HTTPException(404,"Channel not found")
        require_membership(int(u["id"]),int(ch["guild_id"]))
        bscope=f"channel:{int(m['channel_id'])}"
    else:
        cur.execute("SELECT user1,user2 FROM dms WHERE id=?",(int(m["dm_id"]),))
        dm=cur.fetchone()
        if not dm: conn.close(); raise HTTPException(404,"DM not found")
        uid=int(u["id"])
        if uid not in (dm["user1"],dm["user2"]): conn.close(); raise HTTPException(403,"Forbidden")
        bscope=f"dm:{int(m['dm_id'])}"
    cur.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(int(message_id),int(u["id"]),em))
    ex=cur.fetchone()
    if ex:
        cur.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(int(message_id),int(u["id"]),em))
        conn.commit(); conn.close()
        await hub.broadcast(bscope,{"t":"reaction_update","message_id":int(message_id)})
        return {"ok":True,"state":"removed"}
    cur.execute("INSERT INTO reactions(message_id,user_id,emoji,created_ms) VALUES(?,?,?,?)",(int(message_id),int(u["id"]),em,now_ms()))
    conn.commit(); conn.close()
    await hub.broadcast(bscope,{"t":"reaction_update","message_id":int(message_id)})
    return {"ok":True,"state":"added"}

@app.websocket("/ws")
async def ws(ws:WebSocket):
    token=ws.query_params.get("token","").strip()
    if not token:
        await ws.close(code=4401); return
    conn=db();cur=conn.cursor()
    cur.execute("SELECT user_id FROM sessions WHERE token=?",(token,))
    r=cur.fetchone(); conn.close()
    if not r:
        await ws.close(code=4401); return
    uid=int(r["user_id"])
    await ws.accept()
    await hub.add(uid,ws)
    await hub.broadcast("presence",{"t":"presence_update","online_user_ids":online_ids(),"voice":hub.voice})
    try:
        while True:
            raw=await ws.receive_text()
            try: data=json.loads(raw)
            except Exception: continue
            op=str(data.get("op",""))
            if op=="subscribe":
                sc=str(data.get("scope",""))
                if sc.startswith(("channel:","dm:","guild:")) or sc=="presence":
                    await hub.sub(sc,ws)
                    await ws.send_text(json.dumps({"t":"subscribed","scope":sc},ensure_ascii=False))
            elif op=="unsubscribe_all":
                await hub.unsub_all(ws)
            elif op=="send_channel":
                channel_id=int(data.get("channel_id",0))
                content=str(data.get("content",""))[:4000]
                attachments=data.get("attachments") or []
                if not isinstance(attachments,list): attachments=[]
                conn=db();cur=conn.cursor()
                cur.execute("SELECT guild_id FROM channels WHERE id=?",(channel_id,))
                ch=cur.fetchone(); conn.close()
                if not ch: continue
                require_membership(uid,int(ch["guild_id"]))
                conn=db();cur=conn.cursor()
                cur.execute("INSERT INTO messages(scope,channel_id,dm_id,sender_id,content,attachments_json,created_ms) VALUES(?,?,?,?,?,?,?)",("channel",channel_id,None,uid,content,json.dumps(attachments,ensure_ascii=False),now_ms()))
                mid=cur.lastrowid; conn.commit(); conn.close()
                msg={"id":mid,"scope":"channel","channel_id":channel_id,"dm_id":None,"sender":user_basic(uid),"content":content,"attachments":attachments,"created_ms":now_ms(),"reactions":[]}
                await hub.broadcast(f"channel:{channel_id}",{"t":"message","message":msg})
            elif op=="send_dm":
                dm_id=int(data.get("dm_id",0))
                content=str(data.get("content",""))[:4000]
                attachments=data.get("attachments") or []
                if not isinstance(attachments,list): attachments=[]
                conn=db();cur=conn.cursor()
                cur.execute("SELECT user1,user2 FROM dms WHERE id=?",(dm_id,))
                dm=cur.fetchone(); conn.close()
                if not dm or uid not in (dm["user1"],dm["user2"]): continue
                conn=db();cur=conn.cursor()
                cur.execute("INSERT INTO messages(scope,channel_id,dm_id,sender_id,content,attachments_json,created_ms) VALUES(?,?,?,?,?,?,?)",("dm",None,dm_id,uid,content,json.dumps(attachments,ensure_ascii=False),now_ms()))
                mid=cur.lastrowid; conn.commit(); conn.close()
                msg={"id":mid,"scope":"dm","channel_id":None,"dm_id":dm_id,"sender":user_basic(uid),"content":content,"attachments":attachments,"created_ms":now_ms(),"reactions":[]}
                await hub.broadcast(f"dm:{dm_id}",{"t":"message","message":msg})
            elif op=="voice_join":
                channel_id=int(data.get("channel_id",0))
                hub.voice[uid]=channel_id
                await hub.broadcast("presence",{"t":"presence_update","online_user_ids":online_ids(),"voice":hub.voice})
            elif op=="voice_leave":
                if uid in hub.voice: del hub.voice[uid]
                await hub.broadcast("presence",{"t":"presence_update","online_user_ids":online_ids(),"voice":hub.voice})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.remove(uid,ws)
        await hub.broadcast("presence",{"t":"presence_update","online_user_ids":online_ids(),"voice":hub.voice})
