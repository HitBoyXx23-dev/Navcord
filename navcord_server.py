import os
import json
import sqlite3
import hashlib
import uuid
import asyncio
import base64
import threading
import websockets
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from cryptography.fernet import Fernet
from PIL import Image, ImageDraw, ImageFont
import io

app = Flask(__name__)
CORS(app)

DATABASE = 'navcord.db'
UPLOAD_FOLDER = 'uploads'
AVATAR_FOLDER = 'avatars'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(AVATAR_FOLDER, exist_ok=True)

# WebSocket connections
connections = {}
users_online = {}
active_voice_users = {}

# Discord-like servers and channels
servers = {
    'main': {
        'id': 'main',
        'name': 'Navcord',
        'icon': '🛡️',
        'channels': {
            'welcome': {'id': 'welcome', 'name': 'welcome', 'type': 'text', 'position': 0},
            'general': {'id': 'general', 'name': 'general', 'type': 'text', 'position': 1},
            'memes': {'id': 'memes', 'name': 'memes', 'type': 'text', 'position': 2},
            'voice-chat': {'id': 'voice-chat', 'name': 'General Voice', 'type': 'voice', 'position': 3}
        },
        'members': []
    },
    'gaming': {
        'id': 'gaming',
        'name': 'Gaming Hub',
        'icon': '🎮',
        'channels': {
            'gaming-chat': {'id': 'gaming-chat', 'name': 'gaming-chat', 'type': 'text', 'position': 0},
            'game-voice': {'id': 'game-voice', 'name': 'Game Voice', 'type': 'voice', 'position': 1}
        },
        'members': []
    },
    'music': {
        'id': 'music',
        'name': 'Music Lovers',
        'icon': '🎵',
        'channels': {
            'music-chat': {'id': 'music-chat', 'name': 'music-chat', 'type': 'text', 'position': 0},
            'music-voice': {'id': 'music-voice', 'name': 'Music Voice', 'type': 'voice', 'position': 1}
        },
        'members': []
    }
}

def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id TEXT PRIMARY KEY,
                  username TEXT UNIQUE,
                  password TEXT,
                  avatar TEXT,
                  status TEXT DEFAULT 'online',
                  custom_status TEXT,
                  created_at TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id TEXT PRIMARY KEY,
                  server_id TEXT,
                  channel_id TEXT,
                  user_id TEXT,
                  content TEXT,
                  timestamp TIMESTAMP,
                  attachments TEXT,
                  reactions TEXT,
                  reply_to TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS direct_messages
                 (id TEXT PRIMARY KEY,
                  conversation_id TEXT,
                  user_id TEXT,
                  content TEXT,
                  timestamp TIMESTAMP,
                  attachments TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS conversations
                 (id TEXT PRIMARY KEY,
                  user1_id TEXT,
                  user2_id TEXT,
                  last_message TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                 (user_id TEXT PRIMARY KEY,
                  theme TEXT DEFAULT 'dark',
                  show_avatars INTEGER DEFAULT 1,
                  compact_mode INTEGER DEFAULT 0)''')
    
    conn.commit()
    conn.close()

init_db()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(stored_password, provided_password):
    return stored_password == hash_password(provided_password)

def generate_default_avatar(username):
    """Generate a colorful avatar based on username"""
    colors = [
        (41, 128, 185),   # Blue
        (39, 174, 96),    # Green
        (142, 68, 173),   # Purple
        (230, 126, 34),   # Orange
        (231, 76, 60),    # Red
        (52, 152, 219),   # Light Blue
    ]
    
    color = colors[hash(username) % len(colors)]
    img = Image.new('RGB', (128, 128), color)
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("arial.ttf", 60)
    except:
        font = ImageFont.load_default()
    
    text = username[0].upper()
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    position = ((128 - text_width) // 2, (128 - text_height) // 2 - 10)
    draw.text(position, text, fill=(255, 255, 255), font=font)
    
    avatar_filename = f"{uuid.uuid4()}.png"
    avatar_path = os.path.join(AVATAR_FOLDER, avatar_filename)
    img.save(avatar_path, format='PNG')
    
    return f"/api/avatars/{avatar_filename}"

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'success': False, 'error': 'Username and password required'})
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    if c.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Username already exists'})
    
    user_id = str(uuid.uuid4())
    hashed_pw = hash_password(password)
    avatar_url = generate_default_avatar(username)
    
    c.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
              (user_id, username, hashed_pw, avatar_url, 'online', '', datetime.now().isoformat()))
    
    c.execute("INSERT INTO user_settings VALUES (?, ?, ?, ?)",
              (user_id, 'dark', 1, 0))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'user_id': user_id,
        'username': username,
        'avatar': avatar_url,
        'status': 'online'
    })

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    
    if user and verify_password(user[2], password):
        user_data = {
            'id': user[0],
            'username': user[1],
            'avatar': user[3],
            'status': user[4],
            'custom_status': user[5]
        }
        
        # Get user settings
        c.execute("SELECT * FROM user_settings WHERE user_id = ?", (user[0],))
        settings = c.fetchone()
        if settings:
            user_data['settings'] = {
                'theme': settings[1],
                'show_avatars': bool(settings[2]),
                'compact_mode': bool(settings[3])
            }
        
        conn.close()
        return jsonify({'success': True, 'user': user_data})
    
    conn.close()
    return jsonify({'success': False, 'error': 'Invalid credentials'})

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file'})
    
    file = request.files['file']
    user_id = request.form.get('user_id')
    file_type = request.form.get('type', 'attachment')
    
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'})
    
    file_id = str(uuid.uuid4())
    filename = f"{file_id}_{file.filename}"
    
    if file_type == 'avatar':
        filepath = os.path.join(AVATAR_FOLDER, filename)
        
        # Process avatar image
        try:
            img = Image.open(file)
            img = img.convert('RGB')
            img.thumbnail((256, 256))
            img.save(filepath, format='JPEG', quality=95)
            
            # Update user avatar in database
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute("UPDATE users SET avatar = ? WHERE id = ?", 
                     (f"/api/avatars/{filename}", user_id))
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True,
                'url': f"/api/avatars/{filename}",
                'filename': file.filename,
                'file_id': file_id,
                'type': 'avatar'
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    else:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        
        return jsonify({
            'success': True,
            'url': f"/api/files/{filename}",
            'filename': file.filename,
            'file_id': file_id,
            'type': 'attachment'
        })

@app.route('/api/avatars/<filename>')
def get_avatar(filename):
    return send_file(os.path.join(AVATAR_FOLDER, filename))

@app.route('/api/files/<filename>')
def get_file(filename):
    return send_file(os.path.join(UPLOAD_FOLDER, filename))

@app.route('/api/servers', methods=['GET'])
def get_servers():
    return jsonify({'success': True, 'servers': servers})

@app.route('/api/update_status', methods=['POST'])
def update_status():
    data = request.json
    user_id = data.get('user_id')
    status = data.get('status')
    custom_status = data.get('custom_status', '')
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET status = ?, custom_status = ? WHERE id = ?",
              (status, custom_status, user_id))
    conn.commit()
    conn.close()
    
    # Broadcast status change
    broadcast_data = {
        'action': 'status_update',
        'user_id': user_id,
        'status': status,
        'custom_status': custom_status
    }
    
    asyncio.run(broadcast_to_all(broadcast_data))
    
    return jsonify({'success': True})

async def broadcast_to_all(data):
    """Broadcast data to all connected clients"""
    for user_id, ws in connections.items():
        try:
            await ws.send(json.dumps(data))
        except:
            pass

async def broadcast_to_channel(server_id, channel_id, data):
    """Broadcast data to users in a specific channel"""
    if server_id in servers and channel_id in servers[server_id]['channels']:
        for user_id in users_online:
            if user_id in connections:
                try:
                    await connections[user_id].send(json.dumps(data))
                except:
                    pass

async def handle_websocket(websocket, path):
    user_id = None
    
    try:
        async for message in websocket:
            data = json.loads(message)
            action = data.get('action')
            
            if action == 'authenticate':
                user_id = data.get('user_id')
                username = data.get('username')
                avatar = data.get('avatar')
                
                connections[user_id] = websocket
                users_online[user_id] = {
                    'username': username,
                    'avatar': avatar,
                    'status': 'online',
                    'current_server': None,
                    'current_channel': None
                }
                
                # Update database status
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute("UPDATE users SET status = ? WHERE id = ?", ('online', user_id))
                conn.commit()
                conn.close()
                
                # Notify all users
                broadcast_data = {
                    'action': 'user_online',
                    'user_id': user_id,
                    'username': username,
                    'avatar': avatar,
                    'status': 'online'
                }
                
                for uid, ws in connections.items():
                    if uid != user_id:
                        try:
                            await ws.send(json.dumps(broadcast_data))
                        except:
                            pass
                
                # Send current online users
                online_users = []
                for uid, user_data in users_online.items():
                    online_users.append({
                        'user_id': uid,
                        **user_data
                    })
                
                await websocket.send(json.dumps({
                    'action': 'users_list',
                    'users': online_users
                }))
                
                # Send server list
                await websocket.send(json.dumps({
                    'action': 'servers_list',
                    'servers': servers
                }))
                
            elif action == 'join_server':
                server_id = data.get('server_id')
                if server_id in servers:
                    users_online[user_id]['current_server'] = server_id
                    
                    # Send server channels
                    await websocket.send(json.dumps({
                        'action': 'server_channels',
                        'server_id': server_id,
                        'channels': list(servers[server_id]['channels'].values())
                    }))
                    
            elif action == 'join_channel':
                server_id = data.get('server_id')
                channel_id = data.get('channel_id')
                
                if server_id in servers and channel_id in servers[server_id]['channels']:
                    users_online[user_id]['current_channel'] = channel_id
                    
                    # Get channel history
                    conn = sqlite3.connect(DATABASE)
                    c = conn.cursor()
                    c.execute("""
                        SELECT * FROM messages 
                        WHERE server_id = ? AND channel_id = ?
                        ORDER BY timestamp DESC LIMIT 100
                    """, (server_id, channel_id))
                    
                    messages = []
                    for msg in c.fetchall():
                        # Get user info for each message
                        c.execute("SELECT username, avatar FROM users WHERE id = ?", (msg[3],))
                        user_info = c.fetchone()
                        
                        messages.append({
                            'id': msg[0],
                            'server_id': msg[1],
                            'channel_id': msg[2],
                            'user_id': msg[3],
                            'username': user_info[0] if user_info else 'Unknown',
                            'avatar': user_info[1] if user_info else '',
                            'content': msg[4],
                            'timestamp': msg[5],
                            'attachments': json.loads(msg[6]) if msg[6] else [],
                            'reactions': json.loads(msg[7]) if msg[7] else {},
                            'reply_to': msg[8]
                        })
                    
                    conn.close()
                    
                    await websocket.send(json.dumps({
                        'action': 'channel_history',
                        'server_id': server_id,
                        'channel_id': channel_id,
                        'messages': messages[::-1]
                    }))
                    
                    # Notify others in server
                    for uid, user_data in users_online.items():
                        if (uid != user_id and 
                            user_data.get('current_server') == server_id and
                            uid in connections):
                            try:
                                await connections[uid].send(json.dumps({
                                    'action': 'user_joined_channel',
                                    'user_id': user_id,
                                    'username': users_online[user_id]['username'],
                                    'server_id': server_id,
                                    'channel_id': channel_id
                                }))
                            except:
                                pass
                                
            elif action == 'send_message':
                message_id = str(uuid.uuid4())
                server_id = data.get('server_id')
                channel_id = data.get('channel_id')
                content = data.get('content')
                attachments = data.get('attachments', [])
                reply_to = data.get('reply_to')
                
                user_info = users_online.get(user_id, {})
                
                message_data = {
                    'id': message_id,
                    'server_id': server_id,
                    'channel_id': channel_id,
                    'user_id': user_id,
                    'username': user_info.get('username', 'Unknown'),
                    'avatar': user_info.get('avatar', ''),
                    'content': content,
                    'timestamp': datetime.now().isoformat(),
                    'attachments': attachments,
                    'reactions': {},
                    'reply_to': reply_to
                }
                
                # Save to database
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (message_id, server_id, channel_id, user_id, content,
                          message_data['timestamp'],
                          json.dumps(attachments),
                          json.dumps({}),
                          reply_to))
                conn.commit()
                conn.close()
                
                # Broadcast to users in the same server
                broadcast_data = {
                    'action': 'new_message',
                    **message_data
                }
                
                for uid, user_data in users_online.items():
                    if (user_data.get('current_server') == server_id and
                        uid in connections):
                        try:
                            await connections[uid].send(json.dumps(broadcast_data))
                        except:
                            pass
                            
            elif action == 'add_reaction':
                message_id = data.get('message_id')
                server_id = data.get('server_id')
                channel_id = data.get('channel_id')
                emoji = data.get('emoji')
                
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute("SELECT reactions FROM messages WHERE id = ?", (message_id,))
                result = c.fetchone()
                
                if result:
                    reactions = json.loads(result[0]) if result[0] else {}
                    if emoji not in reactions:
                        reactions[emoji] = []
                    if user_id not in reactions[emoji]:
                        reactions[emoji].append(user_id)
                        
                        c.execute("UPDATE messages SET reactions = ? WHERE id = ?",
                                 (json.dumps(reactions), message_id))
                        conn.commit()
                        
                        # Broadcast reaction
                        reaction_data = {
                            'action': 'reaction_added',
                            'message_id': message_id,
                            'server_id': server_id,
                            'channel_id': channel_id,
                            'emoji': emoji,
                            'user_id': user_id,
                            'reactions': reactions
                        }
                        
                        for uid, user_data in users_online.items():
                            if (user_data.get('current_server') == server_id and
                                uid in connections):
                                try:
                                    await connections[uid].send(json.dumps(reaction_data))
                                except:
                                    pass
                
                conn.close()
                
            elif action == 'start_voice':
                server_id = data.get('server_id')
                channel_id = data.get('channel_id')
                
                active_voice_users[user_id] = {
                    'server_id': server_id,
                    'channel_id': channel_id
                }
                
                # Notify others in server
                for uid, user_data in users_online.items():
                    if (uid != user_id and 
                        user_data.get('current_server') == server_id and
                        uid in connections):
                        try:
                            await connections[uid].send(json.dumps({
                                'action': 'voice_user_joined',
                                'user_id': user_id,
                                'username': users_online[user_id]['username'],
                                'server_id': server_id,
                                'channel_id': channel_id
                            }))
                        except:
                            pass
                            
            elif action == 'voice_data':
                server_id = data.get('server_id')
                channel_id = data.get('channel_id')
                audio_data = data.get('audio_data')
                
                # Broadcast to others in same voice channel
                for uid, voice_data in active_voice_users.items():
                    if (uid != user_id and 
                        voice_data['server_id'] == server_id and
                        voice_data['channel_id'] == channel_id and
                        uid in connections):
                        try:
                            await connections[uid].send(json.dumps({
                                'action': 'voice_stream',
                                'user_id': user_id,
                                'audio_data': audio_data
                            }))
                        except:
                            pass
                            
            elif action == 'end_voice':
                if user_id in active_voice_users:
                    server_id = active_voice_users[user_id]['server_id']
                    del active_voice_users[user_id]
                    
                    # Notify others in server
                    for uid, user_data in users_online.items():
                        if (user_data.get('current_server') == server_id and
                            uid in connections):
                            try:
                                await connections[uid].send(json.dumps({
                                    'action': 'voice_user_left',
                                    'user_id': user_id,
                                    'server_id': server_id
                                }))
                            except:
                                pass
                                
            elif action == 'typing_start':
                server_id = data.get('server_id')
                channel_id = data.get('channel_id')
                
                # Notify others in channel
                for uid, user_data in users_online.items():
                    if (uid != user_id and 
                        user_data.get('current_server') == server_id and
                        uid in connections):
                        try:
                            await connections[uid].send(json.dumps({
                                'action': 'user_typing',
                                'user_id': user_id,
                                'username': users_online[user_id]['username'],
                                'server_id': server_id,
                                'channel_id': channel_id
                            }))
                        except:
                            pass
                            
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if user_id:
            # Clean up connections
            if user_id in connections:
                del connections[user_id]
            if user_id in users_online:
                # Update database status
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute("UPDATE users SET status = ? WHERE id = ?", ('offline', user_id))
                conn.commit()
                conn.close()
                
                del users_online[user_id]
            
            # Notify all users
            for uid, ws in connections.items():
                try:
                    await ws.send(json.dumps({
                        'action': 'user_offline',
                        'user_id': user_id
                    }))
                except:
                    pass

async def websocket_server():
    async with websockets.serve(handle_websocket, "0.0.0.0", 5001):
        await asyncio.Future()

def start_websocket_server():
    asyncio.run(websocket_server())

@app.route('/')
def index():
    return jsonify({'status': 'Navcord Server Running', 'version': '2.0.0'})

if __name__ == '__main__':
    websocket_thread = threading.Thread(target=start_websocket_server, daemon=True)
    websocket_thread.start()
    
    print("╔══════════════════════════════════════╗")
    print("║        🛡️  NAVCORD SERVER v2.0       ║")
    print("╠══════════════════════════════════════╣")
    print("║ HTTP Server:  http://0.0.0.0:5000    ║")
    print("║ WebSocket:    ws://0.0.0.0:5001      ║")
    print("║ Status:       ✅ Running             ║")
    print("╚══════════════════════════════════════╝")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
