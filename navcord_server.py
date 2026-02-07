import os
import json
import sqlite3
import hashlib
import uuid
import time
import base64
import threading
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
from cryptography.fernet import Fernet
from PIL import Image
import io

app = Flask(__name__)
app.config['SECRET_KEY'] = 'navcord-secret-key-2025'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

DATABASE = 'navcord.db'
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

users_online = {}
channels = {
    'general': {'name': 'General', 'type': 'text', 'users': []},
    'random': {'name': 'Random', 'type': 'text', 'users': []},
    'voice-chat': {'name': 'Voice Chat', 'type': 'voice', 'users': []},
    'gaming': {'name': 'Gaming', 'type': 'text', 'users': []},
    'music': {'name': 'Music', 'type': 'voice', 'users': []}
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
              (user_id, username, hashed_pw, email, avatar, datetime.now()))
    
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

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    for user_id, data in list(users_online.items()):
        if data.get('sid') == request.sid:
            del users_online[user_id]
            emit('user_offline', {'user_id': user_id}, broadcast=True)
            break

@socketio.on('authenticate')
def handle_authenticate(data):
    user_id = data.get('user_id')
    username = data.get('username')
    avatar = data.get('avatar')
    
    users_online[user_id] = {
        'sid': request.sid,
        'username': username,
        'avatar': avatar,
        'status': 'online'
    }
    
    emit('user_online', {
        'user_id': user_id,
        'username': username,
        'avatar': avatar,
        'status': 'online'
    }, broadcast=True)
    
    emit('users_list', list(users_online.values()))

@socketio.on('join_channel')
def handle_join_channel(data):
    channel = data.get('channel')
    user_id = data.get('user_id')
    
    if channel in channels:
        if user_id not in channels[channel]['users']:
            channels[channel]['users'].append(user_id)
        
        join_room(channel)
        
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
        
        emit('channel_history', {'channel': channel, 'messages': messages[::-1]})
        emit('user_joined_channel', {
            'channel': channel,
            'user_id': user_id,
            'username': users_online.get(user_id, {}).get('username', 'Unknown')
        }, room=channel)

@socketio.on('leave_channel')
def handle_leave_channel(data):
    channel = data.get('channel')
    user_id = data.get('user_id')
    
    if channel in channels and user_id in channels[channel]['users']:
        channels[channel]['users'].remove(user_id)
    
    leave_room(channel)
    
    emit('user_left_channel', {
        'channel': channel,
        'user_id': user_id
    }, room=channel)

@socketio.on('send_message')
def handle_send_message(data):
    message_id = str(uuid.uuid4())
    channel = data.get('channel')
    user_id = data.get('user_id')
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
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
              (message_id, channel, user_id, content,
               message_data['timestamp'],
               json.dumps(attachments),
               json.dumps({})))
    conn.commit()
    conn.close()
    
    emit('new_message', message_data, room=channel)

@socketio.on('add_reaction')
def handle_add_reaction(data):
    message_id = data.get('message_id')
    channel = data.get('channel')
    user_id = data.get('user_id')
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
            
            emit('reaction_added', {
                'message_id': message_id,
                'emoji': emoji,
                'user_id': user_id,
                'reactions': reactions
            }, room=channel)
    
    conn.close()

@socketio.on('start_voice')
def handle_start_voice(data):
    channel = data.get('channel')
    user_id = data.get('user_id')
    
    emit('voice_user_joined', {
        'channel': channel,
        'user_id': user_id,
        'username': users_online.get(user_id, {}).get('username', 'Unknown')
    }, room=channel)

@socketio.on('voice_data')
def handle_voice_data(data):
    channel = data.get('channel')
    user_id = data.get('user_id')
    audio_data = data.get('audio_data')
    
    emit('voice_stream', {
        'user_id': user_id,
        'audio_data': audio_data
    }, room=channel, include_self=False)

@socketio.on('end_voice')
def handle_end_voice(data):
    channel = data.get('channel')
    user_id = data.get('user_id')
    
    emit('voice_user_left', {
        'channel': channel,
        'user_id': user_id
    }, room=channel)

if __name__ == '__main__':
    print("Navcord Server starting on port 5000...")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
