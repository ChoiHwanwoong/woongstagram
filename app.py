from flask import Flask, render_template, request, jsonify, session, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
import psycopg2
import psycopg2.extras
import os
import re
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import cloudinary
import cloudinary.uploader
import traceback

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key_for_woongstagram_app_2026')

# 🔐 대용량 업로드(50MB) & 365일 영구 세션 설정
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

DEFAULT_DB_URL = "postgresql://neondb_owner:YOUR_PASSWORD@ep-xyz.region.aws.neon.tech/neondb?sslmode=require"
DATABASE_URL = os.environ.get('DATABASE_URL', DEFAULT_DB_URL)

# ☁️ Cloudinary 표준 URL 파싱 연동
c_url = os.environ.get('CLOUDINARY_URL', '').strip().strip('"').strip("'")
if c_url:
    if c_url.startswith('CLOUDINARY_URL='):
        c_url = c_url.split('=', 1)[1].strip()
    os.environ['CLOUDINARY_URL'] = c_url
    try:
        parsed = urllib.parse.urlparse(c_url)
        c_key = parsed.username
        c_secret = parsed.password
        c_name = parsed.hostname

        if c_key and c_secret and c_name:
            cloudinary.config(
                cloud_name=c_name,
                api_key=c_key,
                api_secret=c_secret,
                secure=True
            )
        else:
            cloudinary.config(cloudinary_url=c_url, secure=True)
    except Exception as e:
        print("Cloudinary Config Exception:", e)
        cloudinary.config(secure=True)
else:
    cloudinary.config(
        cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
        api_key=os.environ.get('CLOUDINARY_API_KEY', ''),
        api_secret=os.environ.get('CLOUDINARY_API_SECRET', ''),
        secure=True
    )

NEWS_CACHE = {
    'updated_at': None,
    'articles': []
}

def get_kst_now():
    return datetime.now(timezone.utc) + timedelta(hours=9)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(100),
                email VARCHAR(255),
                password VARCHAR(255) NOT NULL,
                profile_img TEXT DEFAULT '',
                bio VARCHAR(30) DEFAULT '',
                is_private BOOLEAN DEFAULT FALSE,
                username_updated_at TIMESTAMP
            );
        ''')

        cursor.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS bio VARCHAR(30) DEFAULT \'\';')
        cursor.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS is_private BOOLEAN DEFAULT FALSE;')
        cursor.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS username_updated_at TIMESTAMP;')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                title VARCHAR(255) DEFAULT '',
                content TEXT NOT NULL,
                image_url TEXT,
                is_video BOOLEAN DEFAULT FALSE,
                likes INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cursor.execute('ALTER TABLE posts ADD COLUMN IF NOT EXISTS is_video BOOLEAN DEFAULT FALSE;')
        cursor.execute('ALTER TABLE posts ADD COLUMN IF NOT EXISTS title VARCHAR(255) DEFAULT \'\';')
        cursor.execute('ALTER TABLE posts ALTER COLUMN title DROP NOT NULL;')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                post_id INT NOT NULL,
                username VARCHAR(100) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS post_likes (
                id SERIAL PRIMARY KEY,
                post_id INT NOT NULL,
                username VARCHAR(100) NOT NULL,
                UNIQUE(post_id, username)
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bookmarks (
                id SERIAL PRIMARY KEY,
                post_id INT NOT NULL,
                username VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(post_id, username)
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stories (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                title VARCHAR(255) DEFAULT '',
                desc_text TEXT NOT NULL,
                image_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cursor.execute('ALTER TABLE stories ADD COLUMN IF NOT EXISTS title VARCHAR(255) DEFAULT \'\';')
        cursor.execute('ALTER TABLE stories ALTER COLUMN title DROP NOT NULL;')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS story_views (
                id SERIAL PRIMARY KEY,
                story_id INT NOT NULL,
                username VARCHAR(100) NOT NULL,
                UNIQUE(story_id, username)
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS follows (
                id SERIAL PRIMARY KEY,
                follower VARCHAR(100) NOT NULL,
                following VARCHAR(100) NOT NULL,
                UNIQUE(follower, following)
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS follow_requests (
                id SERIAL PRIMARY KEY,
                requester VARCHAR(100) NOT NULL,
                target VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(requester, target)
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS direct_messages (
                id SERIAL PRIMARY KEY,
                sender VARCHAR(100) NOT NULL,
                receiver VARCHAR(100) NOT NULL,
                message TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                recipient VARCHAR(100) NOT NULL,
                actor VARCHAR(100) NOT NULL,
                type VARCHAR(30) NOT NULL,
                post_id INT,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print("DB Init Error:", e)

init_db()

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'status': 'error', 'message': '파일 용량이 너무 큽니다. (최대 50MB)'}), 413

@app.errorhandler(Exception)
def handle_all_exceptions(e):
    print("SERVER ERROR TRACEBACK:\n", traceback.format_exc())
    return jsonify({'status': 'error', 'message': f'서버 오류: {str(e)}'}), 400

def create_notification(cursor, recipient, actor, notif_type, post_id=None):
    if recipient == actor:
        return
    now_kst = get_kst_now()
    try:
        cursor.execute('''
            INSERT INTO notifications (recipient, actor, type, post_id, created_at)
            VALUES (%s, %s, %s, %s, %s);
        ''', (recipient, actor, notif_type, post_id, now_kst))
    except Exception as e:
        print("Create Notification Error:", e)

def parse_and_notify_mentions(cursor, text, actor, post_id=None):
    if not text:
        return
    mentions = set(re.findall(r'@([a-zA-Z0-9_]+)', text))
    for username in mentions:
        cursor.execute('SELECT 1 FROM users WHERE username = %s;', (username,))
        if cursor.fetchone():
            create_notification(cursor, recipient=username, actor=actor, notif_type='mention', post_id=post_id)

def is_admin():
    return session.get('username') == 'admin'

# --- 👑 Main Routes & PWA Files ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/service-worker.js')
def serve_service_worker():
    response = send_from_directory('static', 'service-worker.js')
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/profile')
@app.route('/profile/<username>')
def profile_page(username=None):
    return render_template('profile.html', target_username=username)

@app.route('/admin')
def admin_page():
    if not is_admin():
        return "<script>alert('관리자 권한이 필요합니다.'); location.href='/';</script>"
    return render_template('admin.html')

# --- ☁️ Media Upload API ---
@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    image_urls = []
    is_video = False

    files = request.files.getlist('files')
    if not files and 'file' in request.files:
        files = [request.files['file']]

    if not files or all(f.filename == '' for f in files):
        return jsonify({'status': 'error', 'message': '선택된 파일이 없습니다.'}), 400

    for file in files:
        if file and file.filename != '':
            try:
                mimetype = file.content_type or ''
                fn_lower = file.filename.lower()
                resource_type = "video" if (mimetype.startswith('video') or fn_lower.endswith(('.mp4', '.mov', '.avi', '.webm'))) else "image"
                if resource_type == 'video':
                    is_video = True
                
                upload_result = cloudinary.uploader.upload(
                    file, 
                    folder="woongstagram/posts", 
                    resource_type=resource_type
                )
                image_urls.append(upload_result.get('secure_url'))
            except Exception as e:
                print("Cloudinary Upload Exception:\n", traceback.format_exc())
                return jsonify({'status': 'error', 'message': f'Cloudinary 업로드 실패: {str(e)}'}), 400

    return jsonify({
        'status': 'success', 
        'image_urls': image_urls, 
        'image_url': image_urls[0] if image_urls else '', 
        'is_video': is_video
    })

# --- 💬 1:1 DM APIs ---
@app.route('/api/dm/conversations', methods=['GET'])
def get_dm_conversations():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    me = session['username']
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query = '''
        SELECT DISTINCT ON (partner) 
            CASE WHEN sender = %s THEN receiver ELSE sender END as partner,
            message, created_at, sender, is_read
        FROM direct_messages
        WHERE sender = %s OR receiver = %s
        ORDER BY partner, id DESC;
    '''
    cursor.execute(query, (me, me, me))
    rows = cursor.fetchall()

    conversations = []
    for r in rows:
        cursor.execute('SELECT profile_img FROM users WHERE username = %s;', (r['partner'],))
        u = cursor.fetchone()
        
        cursor.execute('SELECT COUNT(*) FROM direct_messages WHERE sender = %s AND receiver = %s AND is_read = FALSE;', (r['partner'], me))
        unread_cnt = cursor.fetchone()[0]

        conversations.append({
            'partner': r['partner'],
            'last_message': r['message'],
            'last_time': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else '',
            'profile_img': u['profile_img'] if u and u['profile_img'] else '',
            'unread_count': unread_cnt
        })

    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'conversations': conversations})

@app.route('/api/dm/<partner>', methods=['GET'])
def get_dm_messages(partner):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    me = session['username']
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cursor.execute('UPDATE direct_messages SET is_read = TRUE WHERE sender = %s AND receiver = %s;', (partner, me))
    conn.commit()

    cursor.execute('''
        SELECT id, sender, receiver, message, is_read, created_at
        FROM direct_messages
        WHERE (sender = %s AND receiver = %s) OR (sender = %s AND receiver = %s)
        ORDER BY id ASC;
    ''', (me, partner, partner, me))
    rows = cursor.fetchall()

    messages = [{
        'id': r['id'],
        'sender': r['sender'],
        'receiver': r['receiver'],
        'message': r['message'],
        'created_at': r['created_at'].strftime('%H:%M') if r['created_at'] else ''
    } for r in rows]

    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'messages': messages})

@app.route('/api/dm/<partner>', methods=['POST'])
def send_dm_message(partner):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    me = session['username']
    data = request.json or {}
    msg = data.get('message', '').strip()

    if not msg:
        return jsonify({'status': 'error', 'message': '메시지를 입력해 주세요.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    now_kst = get_kst_now()

    cursor.execute('INSERT INTO direct_messages (sender, receiver, message, created_at) VALUES (%s, %s, %s, %s);', (me, partner, msg, now_kst))
    create_notification(cursor, recipient=partner, actor=me, notif_type='dm')
    
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

# --- 🔖 북마크 (저장하기) APIs ---
@app.route('/api/posts/<int:post_id>/bookmark', methods=['POST'])
def toggle_bookmark(post_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT 1 FROM bookmarks WHERE post_id = %s AND username = %s;', (post_id, username))
    bookmarked = cursor.fetchone()

    if bookmarked:
        cursor.execute('DELETE FROM bookmarks WHERE post_id = %s AND username = %s;', (post_id, username))
        is_bookmarked = False
    else:
        cursor.execute('INSERT INTO bookmarks (post_id, username, created_at) VALUES (%s, %s, %s);', (post_id, username, get_kst_now()))
        is_bookmarked = True

    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'is_bookmarked': is_bookmarked})

@app.route('/api/users/<username>/bookmarks', methods=['GET'])
def get_user_bookmarks(username):
    if session.get('username') != username and not is_admin():
        return jsonify({'status': 'error', 'message': '비공개 정보입니다.'}), 403

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    query = '''
        SELECT p.id, p.title, p.content, p.image_url, p.likes, p.is_video, p.created_at 
        FROM posts p
        JOIN bookmarks b ON p.id = b.post_id
        WHERE b.username = %s
        ORDER BY b.id DESC;
    '''
    cursor.execute(query, (username,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    posts = []
    for r in rows:
        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try: image_urls = json.loads(raw_img)
                except: image_urls = [raw_img]
            else: image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'is_video': r['is_video'],
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else ''
        })

    return jsonify({'status': 'success', 'posts': posts})

# --- 🔒 비공개 계정 & 팔로우 승인제 APIs ---
@app.route('/api/profile-privacy', methods=['POST'])
def toggle_profile_privacy():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json or {}
    is_private = bool(data.get('is_private', False))
    username = session['username']

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_private = %s WHERE username = %s;', (is_private, username))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success', 'is_private': is_private})

@app.route('/api/follow-requests', methods=['GET'])
def get_follow_requests():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    me = session['username']
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cursor.execute('''
        SELECT fr.id, fr.requester, u.profile_img, fr.created_at
        FROM follow_requests fr
        LEFT JOIN users u ON fr.requester = u.username
        WHERE fr.target = %s
        ORDER BY fr.id DESC;
    ''', (me,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    requests_list = [{
        'id': r['id'],
        'requester': r['requester'],
        'profile_img': r['profile_img'] or '',
        'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else ''
    } for r in rows]

    return jsonify({'status': 'success', 'requests': requests_list})

@app.route('/api/follow-requests/<requester>/<action>', methods=['POST'])
def handle_follow_request(requester, action):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    me = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('DELETE FROM follow_requests WHERE requester = %s AND target = %s;', (requester, me))
    
    if action == 'accept':
        cursor.execute('INSERT INTO follows (follower, following) VALUES (%s, %s);', (requester, me))
        create_notification(cursor, recipient=requester, actor=me, notif_type='follow_accept')

    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

# --- 🤝 Follow APIs (프로필 사진 포함 반환) ---
@app.route('/api/users/<username>/followers', methods=['GET'])
def get_followers(username):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('''
        SELECT u.username, u.profile_img 
        FROM follows f
        JOIN users u ON f.follower = u.username
        WHERE f.following = %s;
    ''', (username,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    users = [{'username': r['username'], 'profile_img': r['profile_img'] or ''} for r in rows]
    return jsonify({'status': 'success', 'users': users})

@app.route('/api/users/<username>/following', methods=['GET'])
def get_following(username):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('''
        SELECT u.username, u.profile_img 
        FROM follows f
        JOIN users u ON f.following = u.username
        WHERE f.follower = %s;
    ''', (username,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    users = [{'username': r['username'], 'profile_img': r['profile_img'] or ''} for r in rows]
    return jsonify({'status': 'success', 'users': users})

# --- 🎬 숏폼 릴스 API ---
@app.route('/api/reels', methods=['GET'])
def get_reels():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('''
        SELECT p.id, p.username, p.content, p.image_url, p.likes, p.created_at, u.profile_img
        FROM posts p
        LEFT JOIN users u ON p.username = u.username
        WHERE p.is_video = TRUE
        ORDER BY p.id DESC;
    ''')
    rows = cursor.fetchall()

    user = session.get('username')
    reels = []
    for r in rows:
        liked = False
        if user:
            cursor.execute('SELECT 1 FROM post_likes WHERE post_id = %s AND username = %s;', (r['id'], user))
            liked = bool(cursor.fetchone())

        raw_img = r['image_url'] or ''
        video_url = ''
        if raw_img.startswith('['):
            try: video_url = json.loads(raw_img)[0]
            except: video_url = raw_img
        else: video_url = raw_img

        reels.append({
            'id': r['id'],
            'username': r['username'],
            'content': r['content'],
            'video_url': video_url,
            'likes': r['likes'],
            'profile_img': r['profile_img'] or '',
            'is_liked': liked
        })

    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'reels': reels})

# --- #️⃣ 해시태그 게시물 조회 API ---
@app.route('/api/posts/tag/<tag>', methods=['GET'])
def get_tag_posts(tag):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    pattern = f"%#{tag}%"
    cursor.execute('SELECT id, title, content, image_url, likes, is_video, created_at FROM posts WHERE content ILIKE %s ORDER BY id DESC;', (pattern,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    posts = []
    for r in rows:
        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try: image_urls = json.loads(raw_img)
                except: image_urls = [raw_img]
            else: image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'is_video': r['is_video'],
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else ''
        })

    return jsonify({'status': 'success', 'tag': tag, 'posts': posts})

# --- 🔔 알림 목록 조회 API ---
@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cursor.execute('''
        SELECT n.id, n.actor, n.type, n.post_id, n.is_read, n.created_at, u.profile_img
        FROM notifications n
        LEFT JOIN users u ON n.actor = u.username
        WHERE n.recipient = %s
        ORDER BY n.id DESC
        LIMIT 25;
    ''', (username,))
    rows = cursor.fetchall()

    notifications = []
    unread_count = 0
    for r in rows:
        if not r['is_read']:
            unread_count += 1

        notifications.append({
            'id': r['id'],
            'actor': r['actor'],
            'type': r['type'],
            'post_id': r['post_id'],
            'is_read': r['is_read'],
            'profile_img': r['profile_img'] or '',
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else ''
        })

    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'notifications': notifications, 'unread_count': unread_count})

@app.route('/api/notifications/read', methods=['POST'])
def mark_notifications_read():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE notifications SET is_read = TRUE WHERE recipient = %s;', (username,))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

# --- 🔍 통합 검색 API ---
@app.route('/api/search', methods=['GET'])
def search_all():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'status': 'success', 'users': [], 'posts': []})

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    search_pattern = f"%{query}%"

    cursor.execute('SELECT username, profile_img, bio FROM users WHERE username ILIKE %s ORDER BY id DESC LIMIT 5;', (search_pattern,))
    user_rows = cursor.fetchall()
    users = [{'username': r['username'], 'profile_img': r['profile_img'] or '', 'bio': r['bio'] or ''} for r in user_rows]

    cursor.execute('SELECT p.id, p.username, p.content, p.image_url, p.is_video, u.profile_img FROM posts p LEFT JOIN users u ON p.username = u.username WHERE p.content ILIKE %s ORDER BY p.id DESC LIMIT 5;', (search_pattern,))
    post_rows = cursor.fetchall()
    posts = []
    for r in post_rows:
        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try: image_urls = json.loads(raw_img)
                except: image_urls = [raw_img]
            else: image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'username': r['username'],
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'is_video': r['is_video'],
            'profile_img': r['profile_img'] or ''
        })

    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'users': users, 'posts': posts})

# --- 📰 뉴스 API ---
@app.route('/api/news', methods=['GET'])
def get_hot_news():
    now = get_kst_now()
    if NEWS_CACHE['updated_at'] and (now - NEWS_CACHE['updated_at']) < timedelta(hours=1):
        return jsonify({'status': 'success', 'articles': NEWS_CACHE['articles'], 'cached': True})

    articles = []
    try:
        rss_url = 'https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko'
        req = urllib.request.Request(rss_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            xml_data = response.read()
            root = ET.fromstring(xml_data)
            for item in root.findall('./channel/item')[:5]:
                title = item.find('title').text if item.find('title') is not None else ''
                link = item.find('link').text if item.find('link') is not None else '#'
                articles.append({'title': title, 'link': link})
    except Exception as e:
        print("RSS Error:", e)

    NEWS_CACHE['updated_at'] = now
    NEWS_CACHE['articles'] = articles
    return jsonify({'status': 'success', 'articles': articles, 'cached': False})

# --- 🔐 User & Profile APIs ---
@app.route('/api/me', methods=['GET'])
def get_me():
    if 'username' in session:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute('SELECT username, name, email, profile_img, bio, is_private FROM users WHERE username = %s;', (session['username'],))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return jsonify({
                'logged_in': True,
                'username': row['username'],
                'name': row['username'],
                'email': row['email'] or '',
                'profile_img': row['profile_img'] or '',
                'bio': row['bio'] or '',
                'is_private': bool(row['is_private']),
                'is_admin': row['username'] == 'admin'
            })
    return jsonify({'logged_in': False, 'is_admin': False})

@app.route('/api/users/<username>', methods=['GET'])
def get_user_profile(username):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT username, name, email, profile_img, bio, is_private FROM users WHERE username = %s;', (username,))
    row = cursor.fetchone()

    if not row:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '사용자를 찾을 수 없습니다.'}), 404

    cursor.execute('SELECT COUNT(*) FROM follows WHERE following = %s;', (username,))
    follower_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM follows WHERE follower = %s;', (username,))
    following_count = cursor.fetchone()[0]

    is_following = False
    is_requested = False
    me = session.get('username')
    if me:
        cursor.execute('SELECT 1 FROM follows WHERE follower = %s AND following = %s;', (me, username))
        is_following = bool(cursor.fetchone())

        cursor.execute('SELECT 1 FROM follow_requests WHERE requester = %s AND target = %s;', (me, username))
        is_requested = bool(cursor.fetchone())

    cursor.close()
    conn.close()

    is_private = bool(row['is_private'])
    can_view_content = (not is_private) or (me == username) or is_following or (me == 'admin')

    return jsonify({
        'status': 'success',
        'user': {
            'username': row['username'],
            'name': row['username'],
            'email': row['email'] or '',
            'profile_img': row['profile_img'] or '',
            'bio': row['bio'] or '',
            'is_private': is_private,
            'follower_count': follower_count,
            'following_count': following_count,
            'is_following': is_following,
            'is_requested': is_requested,
            'can_view_content': can_view_content
        }
    })

@app.route('/api/profile-bio', methods=['POST'])
def update_profile_bio():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json or {}
    bio = data.get('bio', '').strip()
    if len(bio) > 30:
        return jsonify({'status': 'error', 'message': '자기소개는 30글자 이내로 입력해 주세요.'}), 400

    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET bio = %s WHERE username = %s;', (bio, username))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'bio': bio})

@app.route('/api/profile-image', methods=['POST'])
def update_profile_image():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': '파일이 없습니다.'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': '선택된 파일이 없습니다.'}), 400

    if file:
        try:
            upload_result = cloudinary.uploader.upload(file, folder="woongstagram/profiles")
            image_url = upload_result.get('secure_url')

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET profile_img = %s WHERE username = %s;', (image_url, session['username']))
            conn.commit()
            cursor.close()
            conn.close()
            return jsonify({'status': 'success', 'profile_img': image_url})
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'이미지 업로드 실패: {str(e)}'}), 500

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json or {}
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()

    if not username or not password or not email:
        return jsonify({'status': 'error', 'message': '필수 항목을 모두 입력해 주세요.'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO users (username, name, email, password) VALUES (%s, %s, %s, %s);', 
                       (username, username, email, password))
        conn.commit()
        cursor.close()
        conn.close()
        
        session.permanent = True
        session['username'] = username
        session['name'] = username
        return jsonify({'status': 'success', 'username': username})
    except psycopg2.IntegrityError:
        return jsonify({'status': 'error', 'message': '이미 사용 중인 아이디입니다.'}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT username, name FROM users WHERE username = %s AND password = %s;', (username, password))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if user:
        session.permanent = True
        session['username'] = user['username']
        session['name'] = user['username']
        return jsonify({'status': 'success', 'username': user['username']})
    return jsonify({'status': 'error', 'message': '아이디 또는 비밀번호가 올바르지 않습니다.'}), 400

@app.route('/api/change-password', methods=['POST'])
def change_password():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json or {}
    current_password = data.get('current_password', '').strip()
    new_password = data.get('new_password', '').strip()

    if not current_password or not new_password:
        return jsonify({'status': 'error', 'message': '현재 비밀번호와 새 비밀번호를 모두 입력해 주세요.'}), 400

    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username = %s AND password = %s;', (username, current_password))
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '현재 비밀번호가 올바르지 않습니다.'}), 400

    cursor.execute('UPDATE users SET password = %s WHERE username = %s;', (new_password, username))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/change-username', methods=['POST'])
def change_username():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    current_username = session['username']
    if current_username == 'admin':
        return jsonify({'status': 'error', 'message': '관리자 계정의 아이디는 변경할 수 없습니다.'}), 400

    data = request.json or {}
    new_username = data.get('new_username', '').strip()

    if not new_username:
        return jsonify({'status': 'error', 'message': '새로운 아이디를 입력해 주세요.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cursor.execute('SELECT 1 FROM users WHERE username = %s;', (new_username,))
    if cursor.fetchone():
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '이미 다른 사용자가 사용 중인 아이디입니다.'}), 400

    try:
        cursor.execute('UPDATE users SET username = %s, name = %s WHERE username = %s;', (new_username, new_username, current_username))
        cursor.execute('UPDATE posts SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE comments SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE stories SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE follows SET follower = %s WHERE follower = %s;', (new_username, current_username))
        cursor.execute('UPDATE follows SET following = %s WHERE following = %s;', (new_username, current_username))
        cursor.execute('UPDATE post_likes SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE bookmarks SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE direct_messages SET sender = %s WHERE sender = %s;', (new_username, current_username))
        cursor.execute('UPDATE direct_messages SET receiver = %s WHERE receiver = %s;', (new_username, current_username))
        cursor.execute('UPDATE notifications SET recipient = %s WHERE recipient = %s;', (new_username, current_username))
        cursor.execute('UPDATE notifications SET actor = %s WHERE actor = %s;', (new_username, current_username))

        conn.commit()
        session.permanent = True
        session['username'] = new_username
        session['name'] = new_username
        
        cursor.close()
        conn.close()
        return jsonify({'status': 'success', 'new_username': new_username})
    except Exception as e:
        conn.rollback()
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': f'아이디 변경 중 오류: {str(e)}'}), 500

@app.route('/api/delete-account', methods=['POST'])
def delete_account():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    username = session['username']
    if username == 'admin':
        return jsonify({'status': 'error', 'message': '관리자 계정은 삭제할 수 없습니다.'}), 400

    data = request.json or {}
    password = data.get('password', '').strip()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username = %s AND password = %s;', (username, password))
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '비밀번호가 올바르지 않습니다.'}), 400

    cursor.execute('DELETE FROM users WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM posts WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM comments WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM stories WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM follows WHERE follower = %s OR following = %s;', (username, username))
    cursor.execute('DELETE FROM follow_requests WHERE requester = %s OR target = %s;', (username, username))
    cursor.execute('DELETE FROM post_likes WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM bookmarks WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM direct_messages WHERE sender = %s OR receiver = %s;', (username, username))
    cursor.execute('DELETE FROM notifications WHERE recipient = %s OR actor = %s;', (username, username))
    conn.commit()
    cursor.close()
    conn.close()
    session.clear()

    return jsonify({'status': 'success'})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'status': 'success'})

# --- 🤝 Follow APIs ---
@app.route('/api/follow/<target_username>', methods=['POST'])
def toggle_follow(target_username):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    me = session['username']
    if me == target_username:
        return jsonify({'status': 'error', 'message': '자기 자신은 팔로우할 수 없습니다.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cursor.execute('SELECT is_private FROM users WHERE username = %s;', (target_username,))
    target_user = cursor.fetchone()
    is_target_private = target_user['is_private'] if target_user else False

    cursor.execute('SELECT 1 FROM follows WHERE follower = %s AND following = %s;', (me, target_username))
    is_following = bool(cursor.fetchone())

    cursor.execute('SELECT 1 FROM follow_requests WHERE requester = %s AND target = %s;', (me, target_username))
    is_requested = bool(cursor.fetchone())

    result_status = 'none'

    if is_following:
        cursor.execute('DELETE FROM follows WHERE follower = %s AND following = %s;', (me, target_username))
        result_status = 'unfollowed'
    elif is_requested:
        cursor.execute('DELETE FROM follow_requests WHERE requester = %s AND target = %s;', (me, target_username))
        result_status = 'canceled_request'
    else:
        if is_target_private:
            cursor.execute('INSERT INTO follow_requests (requester, target, created_at) VALUES (%s, %s, %s);', (me, target_username, get_kst_now()))
            create_notification(cursor, recipient=target_username, actor=me, notif_type='follow_request')
            result_status = 'requested'
        else:
            cursor.execute('INSERT INTO follows (follower, following) VALUES (%s, %s);', (me, target_username))
            create_notification(cursor, recipient=target_username, actor=me, notif_type='follow')
            result_status = 'following'

    conn.commit()

    cursor.execute('SELECT COUNT(*) FROM follows WHERE following = %s;', (target_username,))
    follower_count = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    return jsonify({
        'status': 'success',
        'result_status': result_status,
        'follower_count': follower_count
    })

# --- 🤖 Recommendation API ---
@app.route('/api/recommendations', methods=['GET'])
def get_recommendations():
    current_user = session.get('username')
    if current_user == 'admin':
        return jsonify({'status': 'success', 'recommendations': []})

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    recommendations = []

    if current_user:
        query = '''
            SELECT pl2.username, COUNT(*) as common_likes
            FROM post_likes pl1
            JOIN post_likes pl2 ON pl1.post_id = pl2.post_id
            WHERE pl1.username = %s AND pl2.username != %s AND pl2.username != 'admin'
            GROUP BY pl2.username
            ORDER BY common_likes DESC
            LIMIT 3;
        '''
        cursor.execute(query, (current_user, current_user))
        similar_users = cursor.fetchall()

        for u in similar_users:
            cursor.execute('SELECT profile_img FROM users WHERE username = %s;', (u['username'],))
            p_img = cursor.fetchone()
            recommendations.append({
                'username': u['username'],
                'reason': f"{current_user}님과 취향이 비슷함",
                'profile_img': p_img['profile_img'] if p_img and p_img['profile_img'] else ''
            })

    if len(recommendations) < 3:
        exclude_users = [current_user, 'admin'] if current_user else ['admin']
        exclude_users.extend([r['username'] for r in recommendations])
        placeholders = ', '.join(['%s'] * len(exclude_users)) if exclude_users else "''"
        
        cursor.execute(f'''
            SELECT username, name, profile_img FROM users 
            WHERE username NOT IN ({placeholders})
            LIMIT %s;
        ''', list(exclude_users) + [3 - len(recommendations)])
        general_users = cursor.fetchall()

        for u in general_users:
            recommendations.append({
                'username': u['username'],
                'reason': 'Woongstagram 추천 회원',
                'profile_img': u['profile_img'] or ''
            })

    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'recommendations': recommendations})

# --- 📸 Story APIs ---
@app.route('/api/stories', methods=['GET'])
def get_stories():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cutoff_time = get_kst_now() - timedelta(hours=24)
    cursor.execute('DELETE FROM stories WHERE created_at < %s;', (cutoff_time,))
    conn.commit()

    cursor.execute('SELECT s.id, s.username, s.title, s.desc_text, s.image_url, s.created_at, u.profile_img FROM stories s LEFT JOIN users u ON s.username = u.username ORDER BY s.id DESC;')
    rows = cursor.fetchall()

    user = session.get('username')
    stories = []
    for r in rows:
        story_id = r['id']
        is_viewed = False
        if user:
            cursor.execute('SELECT 1 FROM story_views WHERE story_id = %s AND username = %s;', (story_id, user))
            is_viewed = bool(cursor.fetchone())

        stories.append({
            'id': story_id,
            'username': r['username'],
            'title': r['title'] or '',
            'desc': r['desc_text'],
            'image_url': r['image_url'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else '',
            'profile_img': r['profile_img'] or '',
            'is_viewed': is_viewed
        })

    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'stories': stories})

@app.route('/api/stories', methods=['POST'])
def create_story():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json or {}
    desc = data.get('desc', '').strip()
    image_url = data.get('image_url', '').strip()

    if not desc and not image_url:
        return jsonify({'status': 'error', 'message': '스토리 내용이나 사진을 등록해 주세요.'}), 400

    try:
        username = session['username']
        now_kst = get_kst_now()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO stories (username, title, desc_text, image_url, created_at) VALUES (%s, %s, %s, %s, %s);', 
                       (username, '', desc, image_url, now_kst))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        print("Create Story Exception:\n", traceback.format_exc())
        return jsonify({'status': 'error', 'message': f'스토리 저장 실패: {str(e)}'}), 400

@app.route('/api/stories/<int:story_id>/view', methods=['POST'])
def mark_story_viewed(story_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO story_views (story_id, username) VALUES (%s, %s);', (story_id, username))
        conn.commit()
    except psycopg2.IntegrityError: pass
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

# --- 📝 Post APIs ---
@app.route('/api/posts', methods=['GET'])
def get_posts():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('''
        SELECT p.id, p.username, p.title, p.content, p.image_url, p.is_video, p.likes, p.created_at, u.profile_img, u.is_private 
        FROM posts p 
        LEFT JOIN users u ON p.username = u.username 
        ORDER BY p.id DESC;
    ''')
    rows = cursor.fetchall()

    user = session.get('username')
    posts = []
    for r in rows:
        post_id = r['id']
        liked = False
        bookmarked = False
        if user:
            cursor.execute('SELECT 1 FROM post_likes WHERE post_id = %s AND username = %s;', (post_id, user))
            liked = bool(cursor.fetchone())

            cursor.execute('SELECT 1 FROM bookmarks WHERE post_id = %s AND username = %s;', (post_id, user))
            bookmarked = bool(cursor.fetchone())

        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try: image_urls = json.loads(raw_img)
                except: image_urls = [raw_img]
            else: image_urls = [raw_img]

        posts.append({
            'id': post_id,
            'username': r['username'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'is_video': r['is_video'],
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else '',
            'profile_img': r['profile_img'] or '',
            'is_liked': liked,
            'is_bookmarked': bookmarked
        })
    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'posts': posts})

@app.route('/api/posts/<int:post_id>', methods=['GET'])
def get_single_post(post_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT p.id, p.username, p.title, p.content, p.image_url, p.is_video, p.likes, p.created_at, u.profile_img FROM posts p LEFT JOIN users u ON p.username = u.username WHERE p.id = %s;', (post_id,))
    r = cursor.fetchone()

    if not r:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '게시글을 찾을 수 없습니다.'}), 404

    user = session.get('username')
    liked = False
    bookmarked = False
    if user:
        cursor.execute('SELECT 1 FROM post_likes WHERE post_id = %s AND username = %s;', (post_id, user))
        liked = bool(cursor.fetchone())
        cursor.execute('SELECT 1 FROM bookmarks WHERE post_id = %s AND username = %s;', (post_id, user))
        bookmarked = bool(cursor.fetchone())

    cursor.close()
    conn.close()

    raw_img = r['image_url'] or ''
    image_urls = []
    if raw_img:
        if raw_img.startswith('['):
            try: image_urls = json.loads(raw_img)
            except: image_urls = [raw_img]
        else: image_urls = [raw_img]

    return jsonify({
        'status': 'success',
        'post': {
            'id': r['id'],
            'username': r['username'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'is_video': r['is_video'],
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else '',
            'profile_img': r['profile_img'] or '',
            'is_liked': liked,
            'is_bookmarked': bookmarked
        }
    })

@app.route('/api/posts/<int:post_id>', methods=['DELETE'])
def delete_post(post_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username FROM posts WHERE id = %s;', (post_id,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '게시글을 찾을 수 없습니다.'}), 404
    if row[0] != username and username != 'admin':
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403
    
    cursor.execute('DELETE FROM comments WHERE post_id = %s;', (post_id,))
    cursor.execute('DELETE FROM post_likes WHERE post_id = %s;', (post_id,))
    cursor.execute('DELETE FROM bookmarks WHERE post_id = %s;', (post_id,))
    cursor.execute('DELETE FROM notifications WHERE post_id = %s;', (post_id,))
    cursor.execute('DELETE FROM posts WHERE id = %s;', (post_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/posts', methods=['POST'])
def create_post():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json or {}
    content = data.get('content', '').strip()
    image_urls = data.get('image_urls', [])
    is_video = bool(data.get('is_video', False))

    if not image_urls and data.get('image_url'):
        image_urls = [data.get('image_url')]

    if not content and not image_urls:
        return jsonify({'status': 'error', 'message': '내용이나 사진/동영상을 등록해 주세요.'}), 400

    try:
        username = session['username']
        image_url_db = json.dumps(image_urls) if image_urls else ''
        now_kst = get_kst_now()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO posts (username, title, content, image_url, is_video, created_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;', 
                       (username, '', content, image_url_db, is_video, now_kst))
        new_post_id = cursor.fetchone()[0]

        parse_and_notify_mentions(cursor, content, username, new_post_id)

        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        print("Create Post Exception:\n", traceback.format_exc())
        return jsonify({'status': 'error', 'message': f'게시글 저장 실패: {str(e)}'}), 400

@app.route('/api/posts/<int:post_id>/like', methods=['POST'])
def toggle_like(post_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT username FROM posts WHERE id = %s;', (post_id,))
    post_owner_row = cursor.fetchone()
    post_owner = post_owner_row[0] if post_owner_row else None

    cursor.execute('SELECT 1 FROM post_likes WHERE post_id = %s AND username = %s;', (post_id, username))
    liked = cursor.fetchone()

    if liked:
        cursor.execute('DELETE FROM post_likes WHERE post_id = %s AND username = %s;', (post_id, username))
        cursor.execute('UPDATE posts SET likes = likes - 1 WHERE id = %s AND likes > 0;', (post_id,))
        is_liked = False
    else:
        cursor.execute('INSERT INTO post_likes (post_id, username) VALUES (%s, %s);', (post_id, username))
        cursor.execute('UPDATE posts SET likes = likes + 1 WHERE id = %s;', (post_id,))
        is_liked = True
        if post_owner:
            create_notification(cursor, recipient=post_owner, actor=username, notif_type='like', post_id=post_id)

    conn.commit()
    cursor.execute('SELECT likes FROM posts WHERE id = %s;', (post_id,))
    new_likes = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    return jsonify({'status': 'success', 'is_liked': is_liked, 'likes': new_likes})

# --- 💬 Comment APIs ---
@app.route('/api/comments/<int:post_id>', methods=['GET'])
def get_comments(post_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT c.id, c.username, c.content, c.created_at, u.profile_img FROM comments c LEFT JOIN users u ON c.username = u.username WHERE c.post_id = %s ORDER BY c.id ASC;', (post_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    comments = [{
        'id': r['id'],
        'username': r['username'],
        'content': r['content'],
        'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else '',
        'profile_img': r['profile_img'] or ''
    } for r in rows]

    return jsonify({'status': 'success', 'comments': comments})

@app.route('/api/comments', methods=['POST'])
def add_comment():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json or {}
    post_id = data.get('post_id')
    content = data.get('content', '').strip()

    if not content:
        return jsonify({'status': 'error', 'message': '댓글 내용을 입력해 주세요.'}), 400

    username = session['username']
    now_kst = get_kst_now()

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT username FROM posts WHERE id = %s;', (post_id,))
    post_owner_row = cursor.fetchone()
    post_owner = post_owner_row[0] if post_owner_row else None

    cursor.execute('INSERT INTO comments (post_id, username, content, created_at) VALUES (%s, %s, %s, %s);', 
                   (post_id, username, content, now_kst))

    if post_owner:
        create_notification(cursor, recipient=post_owner, actor=username, notif_type='comment', post_id=post_id)

    parse_and_notify_mentions(cursor, content, username, post_id)

    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
def delete_comment(comment_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    username = session['username']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username FROM comments WHERE id = %s;', (comment_id,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '댓글을 찾을 수 없습니다.'}), 404
    if row[0] != username and username != 'admin':
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403
    
    cursor.execute('DELETE FROM comments WHERE id = %s;', (comment_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/posts/user/<username>', methods=['GET'])
def get_user_posts(username):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT id, title, content, image_url, is_video, likes, created_at FROM posts WHERE username = %s ORDER BY id DESC;', (username,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    posts = []
    for r in rows:
        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try: image_urls = json.loads(raw_img)
                except: image_urls = [raw_img]
            else: image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'is_video': r['is_video'],
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else ''
        })
    return jsonify({'status': 'success', 'posts': posts})

@app.route('/api/users/<username>/liked-posts', methods=['GET'])
def get_user_liked_posts(username):
    if session.get('username') != username and not is_admin():
        return jsonify({'status': 'error', 'message': '비공개 정보입니다.'}), 403

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    query = '''
        SELECT p.id, p.title, p.content, p.image_url, p.is_video, p.likes, p.created_at 
        FROM posts p
        JOIN post_likes pl ON p.id = pl.post_id
        WHERE pl.username = %s
        ORDER BY pl.id DESC;
    '''
    cursor.execute(query, (username,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    posts = []
    for r in rows:
        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try: image_urls = json.loads(raw_img)
                except: image_urls = [raw_img]
            else: image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'is_video': r['is_video'],
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else ''
        })
    return jsonify({'status': 'success', 'posts': posts})

if __name__ == '__main__':
    app.run(debug=True)