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
from PIL import Image
import io

app = Flask(__name__)
CORS(app)

DATABASE = 'navcord.db'
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# WebSocket connections
connections = {}
users_online = {}
channels = {
    'general': {'name': 'General', 'type': 'text', 'users': set()},
    'random': {'name': 'Random', 'type': 'text', 'users': set()},
    'voice-chat': {'name': 'Voice Chat', 'type': 'voice', 'users': set()},
    'gaming': {'name': 'Gaming', 'type': 'text', 'users': set()},
    'music': {'name': 'Music', 'type': 'voice', 'users': set()}
}

servers = {
    'main': {
        'id': 'main',
        'name': 'Navcord',
        'icon': '🛡️',
        'channels': ['general', 'random', 'voice-chat'],
        'members': []
    },
    'gaming': {
        'id': 'gaming',
        'name': 'Gaming Hub',
        'icon': '🎮',
        'channels': ['gaming'],
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
                  email TEXT,
                  avatar TEXT,
                  created_at TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id TEXT PRIMARY KEY,
                  channel TEXT,
                  user_id TEXT,
                  content TEXT,
                  timestamp TIMESTAMP,
                  attachments TEXT,
                  reactions TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS friendships
                 (id TEXT PRIMARY KEY,
                  user1_id TEXT,
                  user2_id TEXT,
                  status TEXT,
                  created_at TIMESTAMP)''')
    
    conn.commit()
    conn.close()

init_db()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(stored_password, provided_password):
    return stored_password == hash_password(provided_password)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    email = data.get('email')
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute("SELECT * FROM users WHERE username = ? OR email = ?", (username, email))
    if c.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'User already exists'})
    
    user_id = str(uuid.uuid4())
    hashed_pw = hash_password(password)
    avatar = f'https://ui-avatars.com/api/?name={username}&background=1e40af&color=fff'
    
    c.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, username, hashed_pw, email, avatar, datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'user_id': user_id, 'username': username, 'avatar': avatar})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()
    
    if user and verify_password(user[2], password):
        user_data = {
            'id': user[0],
            'username': user[1],
            'avatar': user[4],
            'email': user[3]
        }
        return jsonify({'success': True, 'user': user_data})
    
    return jsonify({'success': False, 'error': 'Invalid credentials'})

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file'})
    
    file = request.files['file']
    user_id = request.form.get('user_id')
    
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'})
    
    file_id = str(uuid.uuid4())
    filename = f"{file_id}_{file.filename}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    
    file_url = f"/api/files/{filename}"
    
    return jsonify({'success': True, 'url': file_url, 'filename': file.filename, 'file_id': file_id})

@app.route('/api/files/<filename>')
def get_file(filename):
    return send_file(os.path.join(UPLOAD_FOLDER, filename))

async def broadcast_message(channel, message):
    """Broadcast message to all users in a channel"""
    users_in_channel = channels[channel]['users'] if channel in channels else set()
    for user_id in users_in_channel:
        if user_id in connections:
            try:
                await connections[user_id].send(json.dumps(message))
            except:
                pass

async def handle_websocket(websocket, path):
    """Handle WebSocket connections"""
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
                    'status': 'online'
                }
                
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
                
                # Send current users list to new user
                users_list = list(users_online.values())
                await websocket.send(json.dumps({
                    'action': 'users_list',
                    'users': users_list
                }))
                
            elif action == 'join_channel':
                channel = data.get('channel')
                if channel in channels:
                    channels[channel]['users'].add(user_id)
                    
                    # Get channel history
                    conn = sqlite3.connect(DATABASE)
                    c = conn.cursor()
                    c.execute("SELECT * FROM messages WHERE channel = ? ORDER BY timestamp DESC LIMIT 50", (channel,))
                    messages = []
                    for msg in c.fetchall():
                        messages.append({
                            'id': msg[0],
                            'channel': msg[1],
                            'user_id': msg[2],
                            'content': msg[3],
                            'timestamp': msg[4],
                            'attachments': json.loads(msg[5]) if msg[5] else [],
                            'reactions': json.loads(msg[6]) if msg[6] else {}
                        })
                    conn.close()
                    
                    await websocket.send(json.dumps({
                        'action': 'channel_history',
                        'channel': channel,
                        'messages': messages[::-1]
                    }))
                    
                    # Notify others in channel
                    for uid in channels[channel]['users']:
                        if uid != user_id and uid in connections:
                            try:
                                await connections[uid].send(json.dumps({
                                    'action': 'user_joined_channel',
                                    'channel': channel,
                                    'user_id': user_id,
                                    'username': users_online.get(user_id, {}).get('username', 'Unknown')
                                }))
                            except:
                                pass
                                
            elif action == 'leave_channel':
                channel = data.get('channel')
                if channel in channels and user_id in channels[channel]['users']:
                    channels[channel]['users'].remove(user_id)
                    
                    # Notify others in channel
                    for uid in channels[channel]['users']:
                        if uid in connections:
                            try:
                                await connections[uid].send(json.dumps({
                                    'action': 'user_left_channel',
                                    'channel': channel,
                                    'user_id': user_id
                                }))
                            except:
                                pass
                                
            elif action == 'send_message':
                message_id = str(uuid.uuid4())
                channel = data.get('channel')
                content = data.get('content')
                attachments = data.get('attachments', [])
                
                message_data = {
                    'id': message_id,
                    'channel': channel,
                    'user_id': user_id,
                    'content': content,
                    'timestamp': datetime.now().isoformat(),
                    'attachments': attachments,
                    'reactions': {},
                    'user_info': users_online.get(user_id, {})
                }
                
                # Save to database
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (message_id, channel, user_id, content,
                          message_data['timestamp'],
                          json.dumps(attachments),
                          json.dumps({})))
                conn.commit()
                conn.close()
                
                # Broadcast to channel
                broadcast_data = {
                    'action': 'new_message',
                    **message_data
                }
                
                for uid in channels[channel]['users']:
                    if uid in connections:
                        try:
                            await connections[uid].send(json.dumps(broadcast_data))
                        except:
                            pass
                            
            elif action == 'add_reaction':
                message_id = data.get('message_id')
                channel = data.get('channel')
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
                            'emoji': emoji,
                            'user_id': user_id,
                            'reactions': reactions
                        }
                        
                        for uid in channels[channel]['users']:
                            if uid in connections:
                                try:
                                    await connections[uid].send(json.dumps(reaction_data))
                                except:
                                    pass
                
                conn.close()
                
            elif action == 'start_voice':
                channel = data.get('channel')
                
                # Notify others in channel
                for uid in channels[channel]['users']:
                    if uid != user_id and uid in connections:
                        try:
                            await connections[uid].send(json.dumps({
                                'action': 'voice_user_joined',
                                'channel': channel,
                                'user_id': user_id,
                                'username': users_online.get(user_id, {}).get('username', 'Unknown')
                            }))
                        except:
                            pass
                            
            elif action == 'voice_data':
                channel = data.get('channel')
                audio_data = data.get('audio_data')
                
                # Broadcast to others in channel
                for uid in channels[channel]['users']:
                    if uid != user_id and uid in connections:
                        try:
                            await connections[uid].send(json.dumps({
                                'action': 'voice_stream',
                                'user_id': user_id,
                                'audio_data': audio_data
                            }))
                        except:
                            pass
                            
            elif action == 'end_voice':
                channel = data.get('channel')
                
                # Notify others in channel
                for uid in channels[channel]['users']:
                    if uid != user_id and uid in connections:
                        try:
                            await connections[uid].send(json.dumps({
                                'action': 'voice_user_left',
                                'channel': channel,
                                'user_id': user_id
                            }))
                        except:
                            pass
                            
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if user_id:
            if user_id in connections:
                del connections[user_id]
            if user_id in users_online:
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
    """Start WebSocket server"""
    async with websockets.serve(handle_websocket, "0.0.0.0", 5001):
        await asyncio.Future()  # run forever

def start_websocket_server():
    """Start WebSocket server in a separate thread"""
    asyncio.run(websocket_server())

@app.route('/')
def index():
    return jsonify({'status': 'Navcord Server Running', 'version': '1.0.0'})

if __name__ == '__main__':
    # Start WebSocket server in background thread
    websocket_thread = threading.Thread(target=start_websocket_server, daemon=True)
    websocket_thread.start()
    
    print("Navcord Server starting...")
    print("HTTP Server: http://0.0.0.0:5000")
    print("WebSocket Server: ws://0.0.0.0:5001")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
