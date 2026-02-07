import os
import json
import hashlib
import base64
import threading
import time
from datetime import datetime
from flask import Flask, request, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import eventlet
import sqlite3
from cryptography.fernet import Fernet
import uuid

eventlet.monkey_patch()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'navcord-secret-key-2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

DATABASE = 'navcord.db'
MEDIA_FOLDER = 'media'
os.makedirs(MEDIA_FOLDER, exist_ok=True)

cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'navcord-encryption-key').digest()))

def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id TEXT PRIMARY KEY, username TEXT UNIQUE, password TEXT, 
                  email TEXT, avatar TEXT, status TEXT, last_seen TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS servers
                 (id TEXT PRIMARY KEY, name TEXT, owner_id TEXT, 
                  icon TEXT, created_at TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS channels
                 (id TEXT PRIMARY KEY, server_id TEXT, name TEXT, 
                  type TEXT, position INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id TEXT PRIMARY KEY, channel_id TEXT, user_id TEXT,
                  content TEXT, timestamp TIMESTAMP, attachments TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS server_members
                 (server_id TEXT, user_id TEXT, role TEXT,
                  PRIMARY KEY (server_id, user_id))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS voice_sessions
                 (id TEXT PRIMARY KEY, channel_id TEXT, user_id TEXT,
                  ip TEXT, port INTEGER, start_time TIMESTAMP)''')
    
    conn.commit()
    conn.close()

init_db()

connected_users = {}
voice_sessions = {}

class Database:
    @staticmethod
    def query(query, params=(), fetchone=False, fetchall=False):
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute(query, params)
        result = None
        if fetchone:
            result = c.fetchone()
        elif fetchall:
            result = c.fetchall()
        conn.commit()
        conn.close()
        return result

    @staticmethod
    def create_user(username, password, email):
        user_id = str(uuid.uuid4())
        hashed = hashlib.sha256(password.encode()).hexdigest()
        Database.query(
            "INSERT INTO users (id, username, password, email, status, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, hashed, email, 'online', datetime.now())
        )
        return user_id

    @staticmethod
    def verify_user(username, password):
        hashed = hashlib.sha256(password.encode()).hexdigest()
        result = Database.query(
            "SELECT id, username, avatar, status FROM users WHERE username = ? AND password = ?",
            (username, hashed), fetchone=True
        )
        return result

    @staticmethod
    def update_status(user_id, status):
        Database.query(
            "UPDATE users SET status = ?, last_seen = ? WHERE id = ?",
            (status, datetime.now(), user_id)
        )

    @staticmethod
    def create_server(name, owner_id):
        server_id = str(uuid.uuid4())
        Database.query(
            "INSERT INTO servers (id, name, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (server_id, name, owner_id, datetime.now())
        )
        channels = ['general', 'random', 'voice-chat']
        for i, chan in enumerate(channels):
            chan_id = str(uuid.uuid4())
            chan_type = 'voice' if 'voice' in chan else 'text'
            Database.query(
                "INSERT INTO channels (id, server_id, name, type, position) VALUES (?, ?, ?, ?, ?)",
                (chan_id, server_id, chan, chan_type, i)
            )
        Database.query(
            "INSERT INTO server_members (server_id, user_id, role) VALUES (?, ?, ?)",
            (server_id, owner_id, 'owner')
        )
        return server_id

    @staticmethod
    def get_user_servers(user_id):
        return Database.query(
            "SELECT s.* FROM servers s JOIN server_members sm ON s.id = sm.server_id WHERE sm.user_id = ?",
            (user_id,), fetchall=True
        )

    @staticmethod
    def get_server_channels(server_id):
        return Database.query(
            "SELECT * FROM channels WHERE server_id = ? ORDER BY position",
            (server_id,), fetchall=True
        )

    @staticmethod
    def add_message(channel_id, user_id, content, attachments=None):
        msg_id = str(uuid.uuid4())
        attachments_json = json.dumps(attachments) if attachments else '[]'
        Database.query(
            "INSERT INTO messages (id, channel_id, user_id, content, timestamp, attachments) VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, channel_id, user_id, content, datetime.now(), attachments_json)
        )
        return msg_id

    @staticmethod
    def get_messages(channel_id, limit=50):
        return Database.query(
            "SELECT m.*, u.username, u.avatar FROM messages m JOIN users u ON m.user_id = u.id WHERE m.channel_id = ? ORDER BY m.timestamp DESC LIMIT ?",
            (channel_id, limit), fetchall=True
        )

    @staticmethod
    def get_channel_users(channel_id):
        return Database.query(
            "SELECT DISTINCT u.id, u.username, u.avatar, u.status FROM users u JOIN messages m ON u.id = m.user_id WHERE m.channel_id = ? UNION SELECT u.id, u.username, u.avatar, u.status FROM users u JOIN server_members sm ON u.id = sm.user_id JOIN channels c ON c.server_id = sm.server_id WHERE c.id = ?",
            (channel_id, channel_id), fetchall=True
        )

@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    user_id = None
    for uid, data in connected_users.items():
        if data['sid'] == request.sid:
            user_id = uid
            break
    if user_id:
        del connected_users[user_id]
        Database.update_status(user_id, 'offline')
        emit('user_status', {'user_id': user_id, 'status': 'offline'}, broadcast=True)

@socketio.on('login')
def handle_login(data):
    username = data.get('username')
    password = data.get('password')
    result = Database.verify_user(username, password)
    if result:
        user_id, username, avatar, status = result
        connected_users[user_id] = {'sid': request.sid, 'username': username}
        Database.update_status(user_id, 'online')
        servers = Database.get_user_servers(user_id)
        channels = []
        if servers:
            channels = Database.get_server_channels(servers[0][0])
        emit('login_success', {
            'user_id': user_id,
            'username': username,
            'avatar': avatar,
            'servers': servers,
            'channels': channels
        })
        emit('user_status', {'user_id': user_id, 'status': 'online'}, broadcast=True)
    else:
        emit('login_failed', {'message': 'Invalid credentials'})

@socketio.on('register')
def handle_register(data):
    username = data.get('username')
    password = data.get('password')
    email = data.get('email')
    try:
        user_id = Database.create_user(username, password, email)
        emit('register_success', {'message': 'Account created successfully'})
    except:
        emit('register_failed', {'message': 'Username already exists'})

@socketio.on('join_channel')
def handle_join_channel(data):
    channel_id = data.get('channel_id')
    join_room(channel_id)
    messages = Database.get_messages(channel_id)
    users = Database.get_channel_users(channel_id)
    emit('channel_history', {'messages': messages, 'users': users})

@socketio.on('send_message')
def handle_send_message(data):
    channel_id = data.get('channel_id')
    user_id = data.get('user_id')
    content = data.get('content')
    attachments = data.get('attachments', [])
    
    msg_id = Database.add_message(channel_id, user_id, content, attachments)
    user = Database.query(
        "SELECT username, avatar FROM users WHERE id = ?",
        (user_id,), fetchone=True
    )
    
    message_data = {
        'id': msg_id,
        'channel_id': channel_id,
        'user_id': user_id,
        'username': user[0],
        'avatar': user[1],
        'content': content,
        'timestamp': datetime.now().isoformat(),
        'attachments': attachments
    }
    
    emit('new_message', message_data, room=channel_id)

@socketio.on('create_server')
def handle_create_server(data):
    name = data.get('name')
    user_id = data.get('user_id')
    server_id = Database.create_server(name, user_id)
    emit('server_created', {'server_id': server_id, 'name': name})

@socketio.on('join_server')
def handle_join_server(data):
    server_id = data.get('server_id')
    user_id = data.get('user_id')
    Database.query(
        "INSERT OR IGNORE INTO server_members (server_id, user_id, role) VALUES (?, ?, ?)",
        (server_id, user_id, 'member')
    )
    channels = Database.get_server_channels(server_id)
    emit('server_joined', {'channels': channels})

@socketio.on('voice_join')
def handle_voice_join(data):
    channel_id = data.get('channel_id')
    user_id = data.get('user_id')
    session_id = str(uuid.uuid4())
    voice_sessions[session_id] = {
        'channel_id': channel_id,
        'user_id': user_id,
        'start_time': datetime.now()
    }
    join_room(f'voice_{channel_id}')
    emit('voice_users_update', {
        'users': list(voice_sessions.values()),
        'session_id': session_id
    }, room=f'voice_{channel_id}')

@socketio.on('voice_leave')
def handle_voice_leave(data):
    session_id = data.get('session_id')
    if session_id in voice_sessions:
        channel_id = voice_sessions[session_id]['channel_id']
        del voice_sessions[session_id]
        leave_room(f'voice_{channel_id}')
        emit('voice_users_update', {
            'users': list(voice_sessions.values())
        }, room=f'voice_{channel_id}')

@socketio.on('voice_data')
def handle_voice_data(data):
    channel_id = data.get('channel_id')
    user_id = data.get('user_id')
    audio_data = data.get('audio_data')
    emit('voice_stream', {
        'user_id': user_id,
        'audio_data': audio_data
    }, room=f'voice_{channel_id}', include_self=False)

@socketio.on('upload_file')
def handle_upload(data):
    filename = data.get('filename')
    file_data = data.get('file_data')
    filepath = os.path.join(MEDIA_FOLDER, filename)
    with open(filepath, 'wb') as f:
        f.write(base64.b64decode(file_data))
    emit('file_uploaded', {'filename': filename, 'url': f'/media/{filename}'})

@app.route('/media/<filename>')
def serve_media(filename):
    return send_file(os.path.join(MEDIA_FOLDER, filename))

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=10000, debug=True)
