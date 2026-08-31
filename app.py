from flask import Flask, render_template, request, jsonify, session, send_from_directory
import psycopg2
import psycopg2.extras
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key_for_woongstagram_app')

# 🔐 [영구 로그인 설정] 로그인 유지 기간을 365일(1년)로 설정
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)
app.config['SESSION_COOKIE_SECURE'] = True  # HTTPS(Render) 환경 쿠키 보안
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

DEFAULT_DB_URL = "postgresql://neondb_owner:YOUR_PASSWORD@ep-xyz.region.aws.neon.tech/neondb?sslmode=require"
DATABASE_URL = os.environ.get('DATABASE_URL', DEFAULT_DB_URL)

cloudinary.config(secure=True)

NEWS_CACHE = {
    'updated_at': None,
    'articles': []
}

def get_kst_now():
    return datetime.now(timezone.utc) + timedelta(hours=9)

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

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
                username_updated_at TIMESTAMP
            );
        ''')

        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS bio VARCHAR(30) DEFAULT '';
        ''')
        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS username_updated_at TIMESTAMP;
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                title VARCHAR(255) DEFAULT '',
                content TEXT NOT NULL,
                image_url TEXT,
                likes INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

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
            CREATE TABLE IF NOT EXISTS stories (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                title VARCHAR(255) DEFAULT '',
                desc_text TEXT NOT NULL,
                image_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

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
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                recipient VARCHAR(100) NOT NULL,
                actor VARCHAR(100) NOT NULL,
                type VARCHAR(20) NOT NULL,
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

# 🔔 알림 목록 조회 API
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
        LIMIT 20;
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

# 🔔 알림 읽음 처리 API
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

# 🔍 유저 & 게시글 통합 검색 API
@app.route('/api/search', methods=['GET'])
def search_all():
    query = request.args.get('q', '').strip()
    if not query or len(query) < 1:
        return jsonify({'status': 'success', 'users': [], 'posts': []})

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    search_pattern = f"%{query}%"

    cursor.execute('SELECT username, profile_img, bio FROM users WHERE username ILIKE %s ORDER BY id DESC LIMIT 5;', (search_pattern,))
    user_rows = cursor.fetchall()
    users = [{'username': r['username'], 'profile_img': r['profile_img'] or '', 'bio': r['bio'] or ''} for r in user_rows]

    cursor.execute('SELECT p.id, p.username, p.content, p.image_url, u.profile_img FROM posts p LEFT JOIN users u ON p.username = u.username WHERE p.content ILIKE %s ORDER BY p.id DESC LIMIT 5;', (search_pattern,))
    post_rows = cursor.fetchall()
    posts = []
    for r in post_rows:
        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try:
                    image_urls = json.loads(raw_img)
                except:
                    image_urls = [raw_img]
            else:
                image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'username': r['username'],
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'profile_img': r['profile_img'] or ''
        })

    cursor.close()
    conn.close()

    return jsonify({'status': 'success', 'users': users, 'posts': posts})

# --- 👑 Admin APIs ---
@app.route('/api/admin/stats', methods=['GET'])
def get_admin_stats():
    if not is_admin():
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM users;')
    user_cnt = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM posts;')
    post_cnt = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM comments;')
    comment_cnt = cursor.fetchone()[0]

    cutoff_time = get_kst_now() - timedelta(hours=24)
    cursor.execute('SELECT COUNT(*) FROM stories WHERE created_at >= %s;', (cutoff_time,))
    story_cnt = cursor.fetchone()[0]

    cursor.close()
    conn.close()
    return jsonify({
        'status': 'success',
        'stats': {
            'users': user_cnt,
            'posts': post_cnt,
            'comments': comment_cnt,
            'stories': story_cnt
        }
    })

@app.route('/api/admin/users', methods=['GET'])
def get_admin_users():
    if not is_admin():
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT id, username, email, profile_img FROM users ORDER BY id DESC;')
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    users = [{'id': r['id'], 'username': r['username'], 'email': r['email'], 'profile_img': r['profile_img'] or ''} for r in rows]
    return jsonify({'status': 'success', 'users': users})

@app.route('/api/admin/users/<username>', methods=['DELETE'])
def delete_admin_user(username):
    if not is_admin():
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    if username == 'admin':
        return jsonify({'status': 'error', 'message': '관리자 계정은 삭제할 수 없습니다.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM users WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM posts WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM comments WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM stories WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM follows WHERE follower = %s OR following = %s;', (username, username))
    cursor.execute('DELETE FROM notifications WHERE recipient = %s OR actor = %s;', (username, username))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/api/admin/posts/<int:post_id>', methods=['DELETE'])
def delete_admin_post(post_id):
    if not is_admin():
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM posts WHERE id = %s;', (post_id,))
    cursor.execute('DELETE FROM comments WHERE post_id = %s;', (post_id,))
    cursor.execute('DELETE FROM post_likes WHERE post_id = %s;', (post_id,))
    cursor.execute('DELETE FROM notifications WHERE post_id = %s;', (post_id,))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/api/admin/stories/<int:story_id>', methods=['DELETE'])
def delete_admin_story(story_id):
    if not is_admin():
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM stories WHERE id = %s;', (story_id,))
    cursor.execute('DELETE FROM story_views WHERE story_id = %s;', (story_id,))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

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

# --- 🔐 Auth & User APIs ---
@app.route('/api/me', methods=['GET'])
def get_me():
    if 'username' in session:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute('SELECT username, name, email, profile_img, bio FROM users WHERE username = %s;', (session['username'],))
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
                'is_admin': row['username'] == 'admin'
            })
    return jsonify({'logged_in': False, 'is_admin': False})

@app.route('/api/users/<username>', methods=['GET'])
def get_user_profile(username):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT username, name, email, profile_img, bio FROM users WHERE username = %s;', (username,))
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
    me = session.get('username')
    if me:
        cursor.execute('SELECT 1 FROM follows WHERE follower = %s AND following = %s;', (me, username))
        is_following = bool(cursor.fetchone())

    cursor.close()
    conn.close()

    return jsonify({
        'status': 'success',
        'user': {
            'username': row['username'],
            'name': row['username'],
            'email': row['email'] or '',
            'profile_img': row['profile_img'] or '',
            'bio': row['bio'] or '',
            'follower_count': follower_count,
            'following_count': following_count,
            'is_following': is_following
        }
    })

@app.route('/api/profile-bio', methods=['POST'])
def update_profile_bio():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json
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
    data = request.json
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
        
        # 🔐 영구 로그인 적용
        session.permanent = True
        session['username'] = username
        session['name'] = username
        return jsonify({'status': 'success', 'username': username})
    except psycopg2.IntegrityError:
        return jsonify({'status': 'error', 'message': '이미 사용 중인 아이디입니다.'}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT username, name FROM users WHERE username = %s AND password = %s;', (username, password))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if user:
        # 🔐 영구 로그인 적용 (365일간 앱을 꺼도 유지)
        session.permanent = True
        session['username'] = user['username']
        session['name'] = user['username']
        return jsonify({'status': 'success', 'username': user['username']})
    return jsonify({'status': 'error', 'message': '아이디 또는 비밀번호가 올바르지 않습니다.'}), 400

@app.route('/api/find-id', methods=['POST'])
def find_id():
    data = request.json
    email = data.get('email', '').strip()

    if not email:
        return jsonify({'status': 'error', 'message': '이메일 주소를 입력해 주세요.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT username FROM users WHERE email = %s;', (email,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if rows:
        usernames = [r['username'] for r in rows]
        return jsonify({'status': 'success', 'usernames': usernames})
    return jsonify({'status': 'error', 'message': '해당 이메일로 가입된 계정을 찾을 수 없습니다.'}), 404

@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    new_password = data.get('new_password', '').strip()

    if not username or not email or not new_password:
        return jsonify({'status': 'error', 'message': '모든 필수 항목을 입력해 주세요.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username = %s AND email = %s;', (username, email))
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '아이디와 이메일 정보가 일치하는 계정이 없습니다.'}), 404

    cursor.execute('UPDATE users SET password = %s WHERE username = %s AND email = %s;', (new_password, username, email))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/api/change-password', methods=['POST'])
def change_password():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json
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

    data = request.json
    new_username = data.get('new_username', '').strip()

    if not new_username:
        return jsonify({'status': 'error', 'message': '새로운 아이디를 입력해 주세요.'}), 400

    if new_username == current_username:
        return jsonify({'status': 'error', 'message': '현재 아이디와 동일합니다.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cursor.execute('SELECT 1 FROM users WHERE username = %s;', (new_username,))
    if cursor.fetchone():
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '이미 다른 사용자가 사용 중인 아이디입니다.'}), 400

    cursor.execute('SELECT username_updated_at FROM users WHERE username = %s;', (current_username,))
    user_row = cursor.fetchone()
    now_kst = get_kst_now()

    if user_row and user_row['username_updated_at']:
        last_updated = user_row['username_updated_at']
        if last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=timezone.utc) + timedelta(hours=9)
        
        time_diff = now_kst - last_updated
        if time_diff.days < 30:
            remaining_days = 30 - time_diff.days
            cursor.close()
            conn.close()
            return jsonify({
                'status': 'error', 
                'message': f'아이디는 30일에 한 번만 변경할 수 있습니다. (다시 변경 가능까지: 약 {remaining_days}일 남음)'
            }), 400

    try:
        cursor.execute('UPDATE users SET username = %s, name = %s, username_updated_at = %s WHERE username = %s;', (new_username, new_username, now_kst, current_username))
        cursor.execute('UPDATE posts SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE comments SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE stories SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE follows SET follower = %s WHERE follower = %s;', (new_username, current_username))
        cursor.execute('UPDATE follows SET following = %s WHERE following = %s;', (new_username, current_username))
        cursor.execute('UPDATE post_likes SET username = %s WHERE username = %s;', (new_username, current_username))
        cursor.execute('UPDATE story_views SET username = %s WHERE username = %s;', (new_username, current_username))
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
        return jsonify({'status': 'error', 'message': f'아이디 변경 중 오류가 발생했습니다: {str(e)}'}), 500

@app.route('/api/delete-account', methods=['POST'])
def delete_account():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    username = session['username']
    if username == 'admin':
        return jsonify({'status': 'error', 'message': '관리자 계정은 삭제할 수 없습니다.'}), 400

    data = request.json
    password = data.get('password', '').strip()

    if not password:
        return jsonify({'status': 'error', 'message': '비밀번호를 입력해 주세요.'}), 400

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
    cursor.execute('DELETE FROM post_likes WHERE username = %s;', (username,))
    cursor.execute('DELETE FROM story_views WHERE username = %s;', (username,))
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
    cursor = conn.cursor()

    cursor.execute('SELECT 1 FROM follows WHERE follower = %s AND following = %s;', (me, target_username))
    row = cursor.fetchone()

    if row:
        cursor.execute('DELETE FROM follows WHERE follower = %s AND following = %s;', (me, target_username))
        is_following = False
    else:
        cursor.execute('INSERT INTO follows (follower, following) VALUES (%s, %s);', (me, target_username))
        is_following = True
        create_notification(cursor, recipient=target_username, actor=me, notif_type='follow')

    conn.commit()

    cursor.execute('SELECT COUNT(*) FROM follows WHERE following = %s;', (target_username,))
    follower_count = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    return jsonify({'status': 'success', 'is_following': is_following, 'follower_count': follower_count})

@app.route('/api/users/<username>/followers', methods=['GET'])
def get_followers(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT follower FROM follows WHERE following = %s;', (username,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'users': [r[0] for r in rows]})

@app.route('/api/users/<username>/following', methods=['GET'])
def get_following(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT following FROM follows WHERE follower = %s;', (username,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'users': [r[0] for r in rows]})

# --- ☁️ Upload API ---
@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    if 'files' in request.files:
        files = request.files.getlist('files')
        image_urls = []
        for file in files:
            if file and file.filename != '':
                try:
                    upload_result = cloudinary.uploader.upload(file, folder="woongstagram/posts")
                    image_urls.append(upload_result.get('secure_url'))
                except Exception as e:
                    return jsonify({'status': 'error', 'message': f'클라우드 업로드 실패: {str(e)}'}), 500
        return jsonify({'status': 'success', 'image_urls': image_urls, 'image_url': image_urls[0] if image_urls else ''})

    if 'file' in request.files:
        file = request.files['file']
        if file and file.filename != '':
            try:
                upload_result = cloudinary.uploader.upload(file, folder="woongstagram/posts")
                image_url = upload_result.get('secure_url')
                return jsonify({'status': 'success', 'image_url': image_url, 'image_urls': [image_url]})
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'클라우드 업로드 실패: {str(e)}'}), 500

    return jsonify({'status': 'error', 'message': '선택된 파일이 없습니다.'}), 400

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
        query_general = f'''
            SELECT username, name, profile_img FROM users 
            WHERE username NOT IN ({placeholders})
            LIMIT %s;
        '''
        params = list(exclude_users) + [3 - len(recommendations)]
        cursor.execute(query_general, params)
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

    data = request.json
    title = data.get('title', '').strip()
    desc = data.get('desc', '').strip()
    image_url = data.get('image_url', '').strip()

    if not desc and not image_url:
        return jsonify({'status': 'error', 'message': '스토리 내용이나 사진을 등록해 주세요.'}), 400

    username = session['username']
    now_kst = get_kst_now()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO stories (username, title, desc_text, image_url, created_at) VALUES (%s, %s, %s, %s, %s);', 
                   (username, title, desc, image_url, now_kst))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/api/stories/<int:story_id>', methods=['DELETE'])
def delete_user_story(story_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username FROM stories WHERE id = %s;', (story_id,))
    row = cursor.fetchone()

    if not row:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '스토리를 찾을 수 없습니다.'}), 404

    if not is_admin() and row[0] != session['username']:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '삭제 권한이 없습니다.'}), 403

    cursor.execute('DELETE FROM stories WHERE id = %s;', (story_id,))
    cursor.execute('DELETE FROM story_views WHERE story_id = %s;', (story_id,))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

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
    except psycopg2.IntegrityError:
        pass

    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

# --- 📝 Post APIs ---
@app.route('/api/posts', methods=['GET'])
def get_posts():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT p.id, p.username, p.title, p.content, p.image_url, p.likes, p.created_at, u.profile_img FROM posts p LEFT JOIN users u ON p.username = u.username ORDER BY p.id DESC;')
    rows = cursor.fetchall()

    user = session.get('username')
    posts = []
    for r in rows:
        post_id = r['id']
        liked = False
        if user:
            cursor.execute('SELECT 1 FROM post_likes WHERE post_id = %s AND username = %s;', (post_id, user))
            liked = bool(cursor.fetchone())

        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try:
                    image_urls = json.loads(raw_img)
                except:
                    image_urls = [raw_img]
            else:
                image_urls = [raw_img]

        posts.append({
            'id': post_id,
            'username': r['username'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else '',
            'profile_img': r['profile_img'] or '',
            'is_liked': liked
        })
    cursor.close()
    conn.close()
    return jsonify({'status': 'success', 'posts': posts})

@app.route('/api/posts/<int:post_id>', methods=['GET'])
def get_single_post(post_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT p.id, p.username, p.title, p.content, p.image_url, p.likes, p.created_at, u.profile_img FROM posts p LEFT JOIN users u ON p.username = u.username WHERE p.id = %s;', (post_id,))
    r = cursor.fetchone()

    if not r:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '게시글을 찾을 수 없습니다.'}), 404

    user = session.get('username')
    liked = False
    if user:
        cursor.execute('SELECT 1 FROM post_likes WHERE post_id = %s AND username = %s;', (post_id, user))
        liked = bool(cursor.fetchone())

    cursor.close()
    conn.close()

    raw_img = r['image_url'] or ''
    image_urls = []
    if raw_img:
        if raw_img.startswith('['):
            try:
                image_urls = json.loads(raw_img)
            except:
                image_urls = [raw_img]
        else:
            image_urls = [raw_img]

    return jsonify({
        'status': 'success',
        'post': {
            'id': r['id'],
            'username': r['username'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else '',
            'profile_img': r['profile_img'] or '',
            'is_liked': liked
        }
    })

@app.route('/api/posts/<int:post_id>', methods=['PUT'])
def update_user_post(post_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json
    title = data.get('title', '').strip()
    content = data.get('content', '').strip()

    if not content:
        return jsonify({'status': 'error', 'message': '내용을 입력해 주세요.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username FROM posts WHERE id = %s;', (post_id,))
    row = cursor.fetchone()

    if not row:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '게시글을 찾을 수 없습니다.'}), 404

    if not is_admin() and row[0] != session['username']:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '수정 권한이 없습니다.'}), 403

    cursor.execute('UPDATE posts SET title = %s, content = %s WHERE id = %s;', (title, content, post_id))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/api/posts/<int:post_id>', methods=['DELETE'])
def delete_user_post(post_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username FROM posts WHERE id = %s;', (post_id,))
    row = cursor.fetchone()

    if not row:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '게시글을 찾을 수 없습니다.'}), 404

    if not is_admin() and row[0] != session['username']:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '삭제 권한이 없습니다.'}), 403

    cursor.execute('DELETE FROM posts WHERE id = %s;', (post_id,))
    cursor.execute('DELETE FROM comments WHERE post_id = %s;', (post_id,))
    cursor.execute('DELETE FROM post_likes WHERE post_id = %s;', (post_id,))
    cursor.execute('DELETE FROM notifications WHERE post_id = %s;', (post_id,))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/api/posts/user/<username>', methods=['GET'])
def get_user_posts(username):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute('SELECT id, title, content, image_url, likes, created_at FROM posts WHERE username = %s ORDER BY id DESC;', (username,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    posts = []
    for r in rows:
        raw_img = r['image_url'] or ''
        image_urls = []
        if raw_img:
            if raw_img.startswith('['):
                try:
                    image_urls = json.loads(raw_img)
                except:
                    image_urls = [raw_img]
            else:
                image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
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
        SELECT p.id, p.title, p.content, p.image_url, p.likes, p.created_at 
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
                try:
                    image_urls = json.loads(raw_img)
                except:
                    image_urls = [raw_img]
            else:
                image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else ''
        })

    return jsonify({'status': 'success', 'posts': posts})

@app.route('/api/users/<username>/commented-posts', methods=['GET'])
def get_user_commented_posts(username):
    if session.get('username') != username and not is_admin():
        return jsonify({'status': 'error', 'message': '비공개 정보입니다.'}), 403

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    query = '''
        SELECT DISTINCT p.id, p.title, p.content, p.image_url, p.likes, p.created_at 
        FROM posts p
        JOIN comments c ON p.id = c.post_id
        WHERE c.username = %s
        ORDER BY p.id DESC;
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
                try:
                    image_urls = json.loads(raw_img)
                except:
                    image_urls = [raw_img]
            else:
                image_urls = [raw_img]

        posts.append({
            'id': r['id'],
            'title': r['title'] or '',
            'content': r['content'],
            'image_url': image_urls[0] if image_urls else '',
            'image_urls': image_urls,
            'likes': r['likes'],
            'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S') if r['created_at'] else ''
        })

    return jsonify({'status': 'success', 'posts': posts})

@app.route('/api/posts', methods=['POST'])
def create_post():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.json
    title = data.get('title', '').strip()
    content = data.get('content', '').strip()
    image_urls = data.get('image_urls', [])

    if not image_urls and data.get('image_url'):
        image_urls = [data.get('image_url')]

    if not content:
        return jsonify({'status': 'error', 'message': '내용을 입력해 주세요.'}), 400

    username = session['username']
    image_url_db = json.dumps(image_urls) if image_urls else ''
    now_kst = get_kst_now()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO posts (username, title, content, image_url, created_at) VALUES (%s, %s, %s, %s, %s);', 
                   (username, title, content, image_url_db, now_kst))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

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

    data = request.json
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

    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
def delete_comment(comment_id):
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username FROM comments WHERE id = %s;', (comment_id,))
    row = cursor.fetchone()

    if not is_admin() and (not row or row[0] != session['username']):
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    cursor.execute('DELETE FROM comments WHERE id = %s;', (comment_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

if __name__ == '__main__':
    app.run(debug=True)