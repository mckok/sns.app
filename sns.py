from flask import Flask, jsonify, request
import pymysql
from werkzeug.security import generate_password_hash, check_password_hash
import firebase_admin
from firebase_admin import credentials, storage, auth
from urllib.parse import unquote
# ✅ [추가] 필요한 라이브러리 import
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from flask_socketio import SocketIO, emit, join_room, leave_room

# ❗ Firebase Admin SDK
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred, {
    'storageBucket': 'snspj-f6514.appspot.com' 
})

app = Flask(__name__)
socketio = SocketIO(app)
sid_to_user = {}
def get_connection():
    return pymysql.connect(
        host='localhost', port=3306, user='root',
        password='1234', db='SNS', charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )


@socketio.on('disconnect')
def handle_disconnect():
    print(f'클라이언트 접속 끊김: {request.sid}')
    user_id = sid_to_user.pop(request.sid, None)
    if user_id:
        leave_room(user_id)
        print(f'사용자 {user_id} 가 자신의 방에서 나갔습니다.')
        
        # ✨ [수정] 접속 종료 시 DB 업데이트 및 상태 전파
        last_seen_time = datetime.now()
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                sql = "UPDATE users SET is_online = FALSE, last_seen = %s WHERE user_id = %s"
                cursor.execute(sql, (last_seen_time, user_id))
                conn.commit()
                print(f"사용자 {user_id} 오프라인 처리 완료.")
            
            # ✅ [추가] 모든 클라이언트에게 상태 변경 알림
            emit('user_status_changed', {
                'user_id': user_id,
                'is_online': False,
                'last_seen': last_seen_time.strftime('%Y-%m-%d %H:%M:%S')
            }, broadcast=True)

        except Exception as e:
            print(f"접속 상태 업데이트 오류: {e}")
        finally:
            if conn: conn.close()

# 안드로이드 앱에서 접속 직후, 자신의 user_id로 방에 들어오게 함
@socketio.on('join')
def on_join(data):
    user_id = None
    if isinstance(data, dict):
        user_id = data.get('user_id')
    elif isinstance(data, str):
        user_id = data

    if user_id:
        join_room(user_id)
        sid_to_user[request.sid] = user_id
        print(f'사용자 {user_id} 가 자신의 방에 입장했습니다. (sid: {request.sid})')
        
        # ✨ [수정] 접속 시 DB 업데이트 및 상태 전파
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                sql = "UPDATE users SET is_online = TRUE, last_seen = NOW() WHERE user_id = %s"
                cursor.execute(sql, (user_id,))
                conn.commit()

            # ✅ [추가] 모든 클라이언트에게 상태 변경 알림
            emit('user_status_changed', {
                'user_id': user_id,
                'is_online': True
            }, broadcast=True)
                
        except Exception as e:
            print(f"접속 상태 업데이트 오류: {e}")
        finally:
            if conn: conn.close()

@app.route('/user/status/<user_id>', methods=['GET'])
def get_user_status(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT is_online, last_seen FROM users WHERE user_id = %s"
            cursor.execute(sql, (user_id,))
            user_status = cursor.fetchone()

            if user_status:
                if user_status.get('last_seen'):
                    user_status['last_seen'] = user_status['last_seen'].strftime('%Y-%m-%d %H:%M:%S')
                
                # is_online 값을 boolean으로 변환
                user_status['is_online'] = bool(user_status['is_online'])
                
                return jsonify({"success": True, "status": user_status})
            else:
                return jsonify({"success": False, "message": "사용자를 찾을 수 없습니다."}), 404
    except Exception as e:
        print(f"Error in /user/status: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn: conn.close()
# 1:1 메시지를 처리하는 핸들러
@socketio.on('private_message')
def handle_private_message(data):
    room_id = data.get('room_id')
    sender_id = data.get('sender_id')
    receiver_id = data.get('receiver_id')
    content = data.get('content')

    if not all([room_id, sender_id, receiver_id, content]):
        return

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 1. messages 테이블에 메시지 저장 (is_read는 기본값 FALSE로 저장됨)
            sql = "INSERT INTO messages (room_id, sender_id, receiver_id, M_content) VALUES (%s, %s, %s, %s)"
            cursor.execute(sql, (room_id, sender_id, receiver_id, content))

            new_message_id = cursor.lastrowid
            data['message_id'] = new_message_id
            # 2. chat_rooms 테이블에 마지막 메시지 정보 업데이트
            sql_update = "UPDATE chat_rooms SET last_message_content = %s, last_message_at = NOW() WHERE room_id = %s"
            cursor.execute(sql_update, (content, room_id))
            
            conn.commit()
            print("Message saved and room updated.")
        
        # 받는 사람의 채팅방(ChatRoomActivity)으로 실시간 메시지 전송
        emit('new_message', data, room=receiver_id)
        
        # ✨ [추가] 받는 사람의 채팅 목록(MessageActivity)에 업데이트 신호 전송
        emit('update_chat_list', {'room_id': room_id}, room=receiver_id)

        emit('update_unread_count', room=receiver_id)
        
    except Exception as e:
        if conn: conn.rollback()
        print(f"DB 저장 오류: {e}")
    finally:
        if conn: conn.close()

@app.route('/chat/unread-room-count/<user_id>', methods=['GET'])
def get_unread_room_count(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 사용자가 참여하고 있는 각 채팅방의 안 읽은 메시지 수를 계산한 후,
            # 그 수가 0보다 큰 방의 개수를 세는 쿼리
            sql = """
                SELECT COUNT(T.room_id) as unread_room_count
                FROM (
                    SELECT
                        cr.room_id,
                        (SELECT COUNT(*) FROM messages m
                         WHERE m.room_id = cr.room_id
                         AND m.receiver_id = %s
                         AND m.send_at > IF(cr.user1_id = %s, cr.user1_last_read_at, cr.user2_last_read_at)
                        ) as unread_messages
                    FROM chat_rooms cr
                    WHERE cr.user1_id = %s OR cr.user2_id = %s
                ) AS T
                WHERE T.unread_messages > 0
            """
            cursor.execute(sql, (user_id, user_id, user_id, user_id))
            result = cursor.fetchone()
            count = result['unread_room_count'] if result else 0
            return jsonify({"success": True, "unread_room_count": count})

    except Exception as e:
        print(f"Error in /chat/unread-room-count: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn: conn.close()


@socketio.on('message_read')
def handle_message_read(data):
    room_id = data.get('room_id')
    reader_id = data.get('reader_id') # 메시지를 읽은 사람 (수신자)
    sender_id = data.get('sender_id') # 메시지를 보낸 사람 (송신자)

    if not all([room_id, reader_id, sender_id]):
        return

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 1. 해당 채팅방에서 수신자가 아직 안 읽은 모든 메시지를 '읽음'으로 변경
            sql = "UPDATE messages SET is_read = TRUE WHERE room_id = %s AND receiver_id = %s AND is_read = FALSE"
            cursor.execute(sql, (room_id, reader_id))
            
            # ✨ [수정] chat_rooms 테이블의 마지막 읽은 시간도 지금 시간으로 갱신
            # 이 코드가 실시간 대화 후 '안읽음'이 남는 문제를 해결합니다.
            cursor.execute("SELECT user1_id FROM chat_rooms WHERE room_id = %s", (room_id,))
            room_info = cursor.fetchone()
            if room_info and room_info['user1_id'] == reader_id:
                update_sql = "UPDATE chat_rooms SET user1_last_read_at = NOW() WHERE room_id = %s"
            else:
                update_sql = "UPDATE chat_rooms SET user2_last_read_at = NOW() WHERE room_id = %s"
            cursor.execute(update_sql, (room_id,))
            
            conn.commit()
            
        # 2. 메시지를 보냈던 원조 송신자에게 "네 메시지 상대가 다 읽었어" 라고 알려줌
        emit('messages_were_read', {'room_id': room_id}, room=sender_id)
        print(f"사용자 {reader_id}가 채팅방 {room_id}의 메시지를 읽었습니다. {sender_id}에게 알림 전송.")

    except Exception as e:
        if conn: conn.rollback()
        print(f"읽음 처리 DB 오류: {e}")
    finally:
        if conn: conn.close()

@app.route('/chat/start', methods=['POST'])
def start_or_get_chat_room():
    data = request.get_json()
    my_id = data.get('my_id')
    other_id = data.get('other_id')

    if not my_id or not other_id:
        return jsonify({"success": False, "message": "사용자 ID가 필요합니다."}), 400

    # 항상 알파벳 순으로 정렬하여 user1, user2를 결정
    if my_id < other_id:
        user1 = my_id
        user2 = other_id
    else:
        user1 = other_id
        user2 = my_id
    
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 기존에 채팅방이 있는지 확인
            sql = "SELECT room_id FROM chat_rooms WHERE user1_id = %s AND user2_id = %s"
            cursor.execute(sql, (user1, user2))
            room = cursor.fetchone()
            
            if room:
                room_id = room['room_id']
            else:
                # 없으면 새로 생성
                sql = "INSERT INTO chat_rooms (user1_id, user2_id, last_message_at) VALUES (%s, %s, NOW())"
                cursor.execute(sql, (user1, user2))
                conn.commit()
                room_id = cursor.lastrowid

            return jsonify({"success": True, "room_id": room_id})
    except Exception as e:
        print(f"Error in /chat/start: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn: conn.close()

# [추가] 2. 특정 사용자의 모든 채팅방 목록을 가져오는 API
@app.route('/chat/rooms/<user_id>', methods=['GET'])
def get_chat_rooms(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # ✨ [수정] SQL 쿼리에 unread_count 계산 로직 추가
            # messages 테이블에서 is_read가 FALSE이고, 받는 사람이 나(user_id)인 메시지의 수를 셉니다.
            sql = """
                SELECT
                    cr.room_id,
                    cr.last_message_content,
                    cr.last_message_at,
                    IF(cr.user1_id = %s, u2.nickname, u1.nickname) AS other_user_nickname,
                    IF(cr.user1_id = %s, u2.profile_image_url, u1.profile_image_url) AS other_user_profile_url,
                    IF(cr.user1_id = %s, u2.user_id, u1.user_id) AS other_user_id,
                    IF(cr.user1_id = %s, u2.is_online, u1.is_online) AS other_user_is_online,
                    IF(cr.user1_id = %s, u2.last_seen, u1.last_seen) AS other_user_last_seen,
                    (
                        SELECT COUNT(*) FROM messages m 
                        WHERE m.room_id = cr.room_id 
                        AND m.receiver_id = %s 
                        AND m.send_at > IF(cr.user1_id = %s, cr.user1_last_read_at, cr.user2_last_read_at)
                    ) as unread_count
                FROM chat_rooms cr
                JOIN users u1 ON cr.user1_id = u1.user_id
                JOIN users u2 ON cr.user2_id = u2.user_id
                WHERE cr.user1_id = %s OR cr.user2_id = %s
                ORDER BY cr.last_message_at DESC
            """
            cursor.execute(sql, (user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id))
            rooms = cursor.fetchall()

            for room in rooms:
                if room.get('last_message_at'):
                    room['last_message_at'] = room['last_message_at'].strftime('%Y-%m-%d %H:%M:%S')
                if room.get('other_user_last_seen'):
                    room['other_user_last_seen'] = room['other_user_last_seen'].strftime('%Y-%m-%d %H:%M:%S')

            return jsonify({"success": True, "rooms": rooms})
    except Exception as e:
        print(f"Error in /chat/rooms: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn: conn.close()

# [추가] 3. 특정 채팅방의 모든 메시지를 가져오는 API
@app.route('/chat/rooms/<int:room_id>/read', methods=['POST'])
def mark_as_read(room_id):
    data = request.get_json()
    viewer_id = data.get('user_id')

    if not viewer_id:
        return jsonify({"success": False, "message": "user_id가 필요합니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user1_id FROM chat_rooms WHERE room_id = %s", (room_id,))
            room_info = cursor.fetchone()
            if not room_info:
                return jsonify({"success": False, "message": "채팅방이 없습니다."}), 404
            
            if room_info['user1_id'] == viewer_id:
                update_sql = "UPDATE chat_rooms SET user1_last_read_at = NOW() WHERE room_id = %s"
            else:
                update_sql = "UPDATE chat_rooms SET user2_last_read_at = NOW() WHERE room_id = %s"
            
            cursor.execute(update_sql, (room_id,))
            conn.commit()
            return jsonify({"success": True, "message": "모든 메시지를 읽음 처리했습니다."})
    except Exception as e:
        if conn: conn.rollback()
        print(f"읽음 처리 오류: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn: conn.close()
@app.route('/chat/messages/<int:room_id>', methods=['GET'])
def get_messages_by_room(room_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 해당 채팅방의 모든 메시지를 시간 순으로 정렬해서 가져옵니다.
            sql = "SELECT * FROM messages WHERE room_id = %s ORDER BY send_at ASC"
            cursor.execute(sql, (room_id,))
            messages = cursor.fetchall()
            
            # 안드로이드에서 다루기 쉽도록 날짜(datetime)를 문자열로 변환합니다.
            for msg in messages:
                if msg.get('send_at'):
                    msg['send_at'] = msg['send_at'].strftime('%Y-%m-%d %H:%M:%S')
                    
                if 'is_read' in msg:
                    msg['is_read'] = bool(msg['is_read'])

            return jsonify({"success": True, "messages": messages})
            
    except Exception as e:
        print(f"Error in /chat/messages/{room_id}: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn: conn.close()
# ✨ [추가] 3. 특정 채팅방의 메시지를 모두 '읽음'으로 처리하는 API
# ✨ [수정] 3. 특정 채팅방의 메시지를 모두 '읽음'으로 처리하는 API (DB 스키마에 맞게 수정)

# @app.route('/messages/<user1_id>/<user2_id>', methods=['GET'])
# def get_messages(user1_id, user2_id):
#     conn = None
#     try:
#         conn = get_connection()
#         with conn.cursor() as cursor:
#             # 두 사람 사이의 모든 메시지를 시간 순으로 정렬하여 가져옵니다.
#             sql = """
#                 SELECT sender_id, receiver_id, M_content, send_at
#                 FROM messages
#                 WHERE (sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)
#                 ORDER BY send_at ASC
#             """
#             cursor.execute(sql, (user1_id, user2_id, user2_id, user1_id))
#             messages = cursor.fetchall()
            
#             # 날짜 형식을 문자열로 변환 (안드로이드에서 다루기 쉽도록)
#             for msg in messages:
#                 if msg.get('send_at'):
#                     msg['send_at'] = msg['send_at'].strftime('%Y-%m-%d %H:%M:%S')

#             return jsonify({"success": True, "messages": messages})
#     except Exception as e:
#         print(f"Error fetching messages: {e}")
#         return jsonify({"success": False, "message": "메시지를 불러오는 중 오류 발생"}), 500
#     finally:
#         if conn: conn.close()


# @app.route('/users', methods=['POST'])
# def login():
#     data = request.get_json()
#     user_id = data.get("email")
#     password = data.get("password")
#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             sql = "SELECT * FROM users WHERE user_id=%s"
#             cursor.execute(sql, (user_id,))
#             user = cursor.fetchone()
#         if user and check_password_hash(user['password'], password):
#             return jsonify({"success": True, "message": "로그인 성공"})
#         else:
#             return jsonify({"success": False, "message": "아이디 또는 비밀번호가 일치하지 않습니다."})
#     except Exception as e:
#         print(e)
#         return jsonify({"success": False, "message": "서버 오류 발생"}), 500
#     finally:
#         conn.close()

@app.route('/users', methods=['POST'])
def login():
    # ▼▼▼ [수정] .get_json() 대신 .form 으로 데이터를 받습니다. ▼▼▼
    data = request.form
    user_id = data.get("email")
    password = data.get("password")
    # ▲▲▲ [수정] ▲▲▲

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT user_id, password, nickname, profile_image_url FROM users WHERE user_id=%s"
            cursor.execute(sql, (user_id,))
            user = cursor.fetchone()
        
        if user and check_password_hash(user['password'], password):
            return jsonify({
                "success": True,
                "message": "로그인 성공",
                "user_id": user['user_id'],
                "nickname": user['nickname'],
                "profile_image_url": user['profile_image_url']
            })
        else:
            return jsonify({"success": False, "message": "아이디 또는 비밀번호가 일치하지 않습니다."})
            
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()

@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    user_id = data.get("user_id")
    password = data.get("password")
    birth_date = data.get("birth_date")
    phone = data.get("phone")
    name = data.get("name")
    if not all([user_id, password, birth_date, phone, name]):
        return jsonify({"success": False, "message": "모든 필수 항목을 입력해주세요."}), 400
    hashed_password = generate_password_hash(password)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            if cursor.fetchone():
                return jsonify({"success": False, "message": "이미 존재하는 이메일입니다."})
            sql = "INSERT INTO users (user_id, password, birth_date, phone, name) VALUES (%s, %s, %s, %s, %s)"
            cursor.execute(sql, (user_id, hashed_password, birth_date, phone, name))
            conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()
    
@app.route('/updateNickname', methods=['POST'])
def update_nickname():
    data = request.get_json()
    user_id = data.get("user_id")
    nickname = data.get("nickname")
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE users SET nickname = %s WHERE user_id = %s"
            cursor.execute(sql, (nickname, user_id))
            conn.commit()
        return jsonify({"success": True, "message": "닉네임이 업데이트 되었습니다."})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": str(e)})
    finally:
        conn.close()

@app.route('/forgot-password', methods=['POST'])
def forgot_password():
    data = request.get_json()
    user_info = data.get("userInfo")
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT user_id FROM users WHERE user_id = %s OR phone = %s"
            cursor.execute(sql, (user_info, user_info))
            result = cursor.fetchone()
        if result:
            return jsonify({"success": True, "message": "사용자 확인됨"})
        else:
            return jsonify({"success": False, "message": "계정을 찾을 수 없습니다."})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()

@app.route('/reset_password', methods=['POST'])
def reset_password():
    data = request.get_json()
    user_info = data.get("userInfo")
    new_password = data.get("newPassword")
    if not user_info or not new_password:
        return jsonify({"success": False, "message": "필수 정보가 누락되었습니다."}), 400
    hashed_password = generate_password_hash(new_password)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE users SET password = %s WHERE user_id = %s OR phone = %s"
            result = cursor.execute(sql, (hashed_password, user_info, user_info))
            conn.commit()
        if result > 0:
            return jsonify({"success": True, "message": "비밀번호가 성공적으로 변경되었습니다."})
        else:
            return jsonify({"success": False, "message": "해당 사용자를 찾을 수 없습니다."})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()

@app.route('/profile/<user_id>', methods=['GET'])
def get_profile(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT name, nickname, gender, profile_image_url FROM users WHERE user_id = %s"
            cursor.execute(sql, (user_id,))
            user_data = cursor.fetchone()
            if not user_data:
                return jsonify({"success": False, "message": "사용자를 찾을 수 없습니다."}), 404
            cursor.execute("SELECT COUNT(*) as count FROM posts WHERE user_id = %s AND is_deleted = FALSE", (user_id,))
            post_count = cursor.fetchone()['count']
            
            cursor.execute("SELECT COUNT(*) as count FROM follows WHERE following_id = %s", (user_id,))
            follower_count = cursor.fetchone()['count']
            cursor.execute("SELECT COUNT(*) as count FROM follows WHERE follower_id = %s", (user_id,))
            following_count = cursor.fetchone()['count']
            profile_info = {
                "name": user_data.get('name'),
                "nickname": user_data.get('nickname'),
                "gender": user_data.get('gender'),
                "post_count": post_count,
                "follower_count": follower_count,
                "following_count": following_count,
                "profile_image_url": user_data.get('profile_image_url')
            }
            return jsonify({"success": True, "data": profile_info})
    except Exception as e:
        print(f"프로필 조회 오류: {e}")
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()

@app.route('/profile/image', methods=['POST'])
def update_profile_image():
    data = request.get_json()
    user_id = data.get("user_id")
    image_url = data.get("image_url")
    if not user_id or not image_url:
        return jsonify({"success": False, "message": "필수 정보가 누락되었습니다."}), 400
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE users SET profile_image_url = %s WHERE user_id = %s"
            cursor.execute(sql, (image_url, user_id))
        conn.commit()
        return jsonify({"success": True, "message": "프로필 이미지가 업데이트되었습니다."})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()

@app.route('/profile/update', methods=['POST'])
def update_profile():
    data = request.get_json()
    
    # ✅ 1. 어떤 데이터가 들어왔는지 터미널에 자세히 출력
    print("--- 프로필 업데이트 요청 받음 ---")
    print(f"수신 데이터: {data}")
    
    user_id = data.get("user_id")
    name = data.get("name")
    nickname = data.get("nickname")
    gender = data.get("gender")
    profile_image_url = data.get("profile_image_url")

    if not user_id:
        return jsonify({"success": False, "message": "사용자 ID가 없습니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE users SET name = %s, nickname = %s, gender = %s, profile_image_url = %s WHERE user_id = %s"
            
            # ✅ 2. 어떤 SQL문이 실행되는지 터미널에 출력
            print(f"실행될 SQL: {cursor.mogrify(sql, (name, nickname, gender, profile_image_url, user_id))}")
            
            cursor.execute(sql, (name, nickname, gender, profile_image_url, user_id))
        conn.commit()
        print("--- 프로필 업데이트 성공 ---")
        return jsonify({"success": True, "message": "프로필이 업데이트되었습니다."})
    except Exception as e:
        print(f"DB 오류 발생: {e}")
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()


@app.route('/check-user', methods=['POST'])
def check_user_exists():
    data = request.get_json()
    email = data.get('email')

    if not email:
        return jsonify({"success": False, "message": "이메일이 필요합니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT user_id FROM users WHERE user_id = %s"
            cursor.execute(sql, (email,))
            result = cursor.fetchone()
        
        if result:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()
@app.route('/posts', methods=['POST'])
def create_post():
    data = request.get_json()
    user_id = data.get('user_id')
    content = data.get('caption')
    image_urls = data.get('image_urls')
    if not user_id or not image_urls:
        return jsonify({"success": False, "message": "사용자 정보와 이미지는 필수입니다."}), 400
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "INSERT INTO posts (user_id, P_content) VALUES (%s, %s)"
            cursor.execute(sql, (user_id, content))
            post_id = cursor.lastrowid
            if image_urls:
                image_data = [(post_id, url, index) for index, url in enumerate(image_urls)]
                sql_images = "INSERT INTO post_images (post_id, image_url, image_order) VALUES (%s, %s, %s)"
                cursor.executemany(sql_images, image_data)
        conn.commit()
        return jsonify({"success": True, "message": "게시물이 성공적으로 작성되었습니다."})
    except Exception as e:
        conn.rollback()
        print(e)
        return jsonify({"success": False, "message": "게시물 작성 중 오류 발생"}), 500
    finally:
        conn.close()

# @app.route('/posts/<user_id>', methods=['GET'])
# def get_user_posts(user_id):
#     current_user_id = request.args.get('current_user_id')
#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             # ✅ users 테이블과 JOIN하여 닉네임과 프로필 이미지 URL을 함께 가져오도록 SQL 수정
#             sql = """
#                 SELECT 
#                     p.post_id, p.P_content, p.P_created_at,
#                     u.nickname, u.profile_image_url
#                 FROM posts p
#                 JOIN users u ON p.user_id = u.user_id
#                 WHERE p.user_id = %s 
#                 ORDER BY p.P_created_at DESC
#             """
#             cursor.execute(sql, (user_id,))
#             posts = cursor.fetchall()
#             for post in posts:
#                 # ✅ 각 게시물의 좋아요 수와 현재 사용자 좋아요 여부 계산
#                 post_id = post['post_id']
#                 cursor.execute("SELECT COUNT(*) as count FROM likes WHERE post_id = %s", (post_id,))
#                 post['like_count'] = cursor.fetchone()['count']
#                 cursor.execute("SELECT * FROM likes WHERE post_id = %s AND user_id = %s", (post_id, current_user_id))
#                 post['is_liked_by_user'] = True if cursor.fetchone() else False
#             # 각 게시물에 해당하는 이미지들과 좋아요 수를 찾아서 추가
#             for post in posts:
#                 if post.get('P_created_at'):
#                     post['P_created_at'] = post['P_created_at'].strftime('%Y-%m-%d %H:%M:%S')
#                 sql_images = "SELECT image_url FROM post_images WHERE post_id = %s ORDER BY image_order ASC"
#                 cursor.execute(sql_images, (post['post_id'],))
#                 images = cursor.fetchall()
#                 post['images'] = [image['image_url'] for image in images]

#                 # TODO: 실제 좋아요 수 계산 로직 추가
                

#             return jsonify({"success": True, "posts": posts})
#     except Exception as e:
#         print(e)
#         return jsonify({"success": False, "message": "서버 오류 발생"}), 500
#     finally:
#         conn.close()
@app.route('/posts/<user_id>', methods=['GET'])
def get_user_posts(user_id):
    current_user_id = request.args.get('current_user_id')
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT 
                    p.post_id, p.P_content, p.P_created_at,
                    u.nickname, u.profile_image_url
                FROM posts p
                JOIN users u ON p.user_id = u.user_id
                WHERE p.user_id = %s 
                AND p.is_deleted = FALSE
                ORDER BY p.P_created_at DESC
            """
            cursor.execute(sql, (user_id,))
            posts = cursor.fetchall()

            for post in posts:
                post_id = post['post_id']
                
                # 날짜 및 이미지 정보 (수정 없음)
                if post.get('P_created_at'):
                    post['P_created_at'] = post['P_created_at'].strftime('%Y-%m-%d %H:%M:%S')

                sql_images = "SELECT image_url FROM post_images WHERE post_id = %s ORDER BY image_order ASC"
                cursor.execute(sql_images, (post_id,))
                images = cursor.fetchall()
                post['images'] = [image['image_url'] for image in images]

                # 좋아요 정보 조회 (수정됨)
                try:
                    cursor.execute("SELECT COUNT(*) as count FROM likes WHERE post_id = %s", (post_id,))
                    like_result = cursor.fetchone()
                    # ✅ [수정] 좋아요가 0개일 때(None)를 대비한 안전장치 추가
                    post['like_count'] = like_result['count'] if like_result else 0
                    
                    if current_user_id:
                        cursor.execute("SELECT 1 FROM likes WHERE post_id = %s AND user_id = %s", (post_id, current_user_id))
                        post['is_liked_by_user'] = True if cursor.fetchone() else False
                    else:
                        post['is_liked_by_user'] = False
                except Exception as like_error:
                    post['like_count'] = 0
                    post['is_liked_by_user'] = False
                
                # ✅ [수정] 댓글 개수 계산 로직을 올바른 위치로 이동하고 안전장치 추가
                cursor.execute("SELECT COUNT(*) as count FROM comments WHERE post_id = %s", (post_id,))
                comment_result = cursor.fetchone()
                post['comment_count'] = comment_result['count'] if comment_result else 0

            return jsonify({"success": True, "posts": posts})
    except Exception as e:
        print(f"Error in get_user_posts: {e}")
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()

# app.py

# app.py

# @app.route('/posts/detail/<int:post_id>', methods=['GET'])
# def get_post_detail(post_id):
#     current_user_id = request.args.get('user_id')
#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             # likes_hidden 컬럼을 함께 조회
#             sql = """
#                 SELECT
#                     p.post_id, p.P_content, p.P_created_at, p.comments_disabled, p.likes_hidden,
#                     u.user_id, u.nickname, u.profile_image_url
#                 FROM posts p
#                 JOIN users u ON p.user_id = u.user_id
#                 WHERE p.post_id = %s
#             """
#             cursor.execute(sql, (post_id,))
#             post_data = cursor.fetchone()

#             if not post_data:
#                 return jsonify({"success": False, "message": "게시물을 찾을 수 없습니다."}), 404
#             cursor.execute("SELECT COUNT(*) as count FROM likes WHERE post_id = %s", (post_id,))
#             post_data['like_count'] = cursor.fetchone()['count']

#             cursor.execute("SELECT * FROM likes WHERE post_id = %s AND user_id = %s", (post_id, current_user_id))
#             post_data['is_liked_by_user'] = True if cursor.fetchone() else False
            

#             # ✅ 숫자(0/1)를 boolean(false/true)으로 변환하는 코드 추가
#             if 'comments_disabled' in post_data:
#                 post_data['comments_disabled'] = bool(post_data['comments_disabled'])
#             if 'likes_hidden' in post_data:
#                 post_data['likes_hidden'] = bool(post_data['likes_hidden'])

#             # (날짜 변환, 이미지 목록 가져오기 등 기존 로직은 동일)
#             if post_data.get('P_created_at'):
#                 post_data['P_created_at'] = post_data['P_created_at'].strftime('%Y-%m-%d %H:%M:%S')

#             sql_images = "SELECT image_url FROM post_images WHERE post_id = %s ORDER BY image_order ASC"
#             cursor.execute(sql_images, (post_id,))
#             images = cursor.fetchall()
#             post_data['images'] = [image['image_url'] for image in images]
            
            

#             return jsonify({"success": True, "post": post_data})
#     except Exception as e:
#         print(e)
#         return jsonify({"success": False, "message": "서버 오류"}), 500
#     finally:
#         conn.close()
@app.route('/posts/detail/<int:post_id>', methods=['GET'])
def get_post_detail(post_id):
    is_deleted_from_request = request.args.get('isDeleted', 'false').lower() == 'true'
    current_user_id = request.args.get('user_id')
    view_deleted = request.args.get('view_deleted', 'false').lower() == 'true'
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT
                    p.post_id, p.P_content, p.P_created_at, p.comments_disabled, p.likes_hidden,
                    u.user_id, u.nickname, u.profile_image_url
                FROM posts p
                JOIN users u ON p.user_id = u.user_id
                WHERE p.post_id = %s
                AND p.is_deleted = FALSE
            """
            cursor.execute(sql, (post_id,))
            post_data = cursor.fetchone()

            if not post_data:
                return jsonify({"success": False, "message": "게시물을 찾을 수 없습니다."}), 404

            try:
                cursor.execute("SELECT COUNT(*) as count FROM likes WHERE post_id = %s", (post_id,))
                like_result = cursor.fetchone()
                # ✅ [수정] 좋아요가 0개일 때를 대비한 안전장치 추가
                post_data['like_count'] = like_result['count'] if like_result else 0
                
                if current_user_id:
                    cursor.execute("SELECT 1 FROM likes WHERE post_id = %s AND user_id = %s", (post_id, current_user_id))
                    post_data['is_liked_by_user'] = True if cursor.fetchone() else False
                else:
                    post_data['is_liked_by_user'] = False
            except Exception as like_error:
                post_data['like_count'] = 0
                post_data['is_liked_by_user'] = False

            cursor.execute("SELECT COUNT(*) as count FROM comments WHERE post_id = %s", (post_id,))
            comment_result = cursor.fetchone()
            # ✅ [수정] 댓글이 0개일 때를 대비한 안전장치 추가
            post_data['comment_count'] = comment_result['count'] if comment_result else 0

            post_data['comments_disabled'] = bool(post_data.get('comments_disabled', 0))
            post_data['likes_hidden'] = bool(post_data.get('likes_hidden', 0))
            
            if post_data.get('P_created_at'):
                post_data['P_created_at'] = post_data['P_created_at'].strftime('%Y-%m-%d %H:%M:%S')

            sql_images = "SELECT image_url FROM post_images WHERE post_id = %s ORDER BY image_order ASC"
            cursor.execute(sql_images, (post_id,))
            images = cursor.fetchall()
            post_data['images'] = [image['image_url'] for image in images]
            
            return jsonify({"success": True, "post": post_data})
    except Exception as e:
        print(f"Error in get_post_detail: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

@app.route('/posts/<int:post_id>/soft-delete', methods=['POST'])
def soft_delete_post(post_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # Instead of DELETE, use UPDATE to set the is_deleted flag and timestamp
            sql = "UPDATE posts SET is_deleted = TRUE, deleted_at = NOW() WHERE post_id = %s"
            cursor.execute(sql, (post_id,))
        conn.commit()
        return jsonify({"success": True, "message": "게시물이 삭제 처리되었습니다."})
    except Exception as e:
        conn.rollback()
        print(f"Error in soft_delete_post: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()
@app.route('/posts/<int:post_id>', methods=['DELETE'])
def delete_post(post_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql_get_images = "SELECT image_url FROM post_images WHERE post_id = %s"
            cursor.execute(sql_get_images, (post_id,))
            images_to_delete = cursor.fetchall()

            # ✅ 버킷 이름을 명시적으로 지정
            bucket = storage.bucket('snsPJ-f6514.appspot.com')
            for image in images_to_delete:
                if image['image_url']:
                    try:
                        full_url = image['image_url']
                        start_key = "/o/"
                        end_key = "?alt=media"
                        start_index = full_url.find(start_key)
                        end_index = full_url.find(end_key)
                        if start_index != -1 and end_index != -1:
                            encoded_path = full_url[start_index + len(start_key) : end_index]
                            file_path = unquote(encoded_path)
                            blob = bucket.blob(file_path)
                            if blob.exists():
                                blob.delete()
                                print(f"Firebase Storage에서 {file_path} 삭제 성공")
                            else:
                                print(f"Firebase Storage에 파일이 없음: {file_path}")
                        else:
                            print(f"URL 형식이 예상과 다름: {full_url}")
                    except Exception as e:
                        print(f"Firebase 파일 삭제 중 오류 발생: {e}")

            cursor.execute("DELETE FROM post_images WHERE post_id = %s", (post_id,))
            rows_deleted = cursor.execute("DELETE FROM posts WHERE post_id = %s", (post_id,))
            
        conn.commit()

        if rows_deleted > 0:
            return jsonify({"success": True, "message": "게시물이 삭제되었습니다."})
        else:
            return jsonify({"success": False, "message": "삭제할 게시물이 없습니다."})
    except Exception as e:
        conn.rollback()
        print(e)
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()

@app.route('/posts/recently-deleted/<user_id>', methods=['GET'])
def get_recently_deleted_posts(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # is_deleted가 TRUE인 게시물만 선택하고, 남은 기간(days_left)도 계산합니다.
            sql = """
                SELECT 
                    post_id, 
                    (SELECT image_url FROM post_images WHERE post_id = p.post_id ORDER BY image_order ASC LIMIT 1) as thumbnail_url, 
                    DATEDIFF(deleted_at + INTERVAL 30 DAY, NOW()) as days_left 
                FROM posts p 
                WHERE user_id = %s AND is_deleted = TRUE 
                ORDER BY deleted_at DESC
            """
            cursor.execute(sql, (user_id,))
            deleted_posts = cursor.fetchall()
            return jsonify({"success": True, "posts": deleted_posts})
    except Exception as e:
        print(f"Error in get_recently_deleted_posts: {e}")
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()

# ✅ [수정됨] 계정 삭제 API
@app.route('/delete-account', methods=['POST'])
def delete_account():
    data = request.get_json()
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"success": False, "message": "사용자 ID가 필요합니다."}), 400
    try:
        user_to_delete = auth.get_user_by_email(user_id)
        auth.delete_user(user_to_delete.uid)
        
        conn = get_connection()
        with conn.cursor() as cursor:
            sql_get_posts = "SELECT post_id FROM posts WHERE user_id = %s"
            cursor.execute(sql_get_posts, (user_id,))
            posts = cursor.fetchall()
            # ✅ 버킷 이름을 명시적으로 지정
            bucket = storage.bucket('snsPJ-f6514.appspot.com')
            for post in posts:
                sql_get_images = "SELECT image_url FROM post_images WHERE post_id = %s"
                cursor.execute(sql_get_images, (post['post_id'],))
                images = cursor.fetchall()
                for image in images:
                    if image['image_url']:
                        try:
                            full_url = image['image_url']
                            start_key = "/o/"
                            end_key = "?alt=media"
                            start_index = full_url.find(start_key)
                            end_index = full_url.find(end_key)
                            if start_index != -1 and end_index != -1:
                                encoded_path = full_url[start_index + len(start_key) : end_index]
                                file_path = unquote(encoded_path)
                                blob = bucket.blob(file_path)
                                if blob.exists():
                                    blob.delete()
                        except Exception as e:
                            print(f"Firebase 파일 삭제 오류: {e}")
            
            cursor.execute("DELETE FROM post_images WHERE post_id IN (SELECT post_id FROM posts WHERE user_id = %s)", (user_id,))
            cursor.execute("DELETE FROM posts WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM follows WHERE follower_id = %s OR following_id = %s", (user_id, user_id))
            cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "계정이 성공적으로 삭제되었습니다."})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "계정 삭제 중 오류 발생"}), 500

# app.py

@app.route('/posts/<int:post_id>', methods=['PUT'])
def update_post(post_id):
    data = request.get_json()
    
    # ✅ 1. 어떤 데이터가 들어왔는지 터미널에 출력
    print("--- 게시물 수정 요청 받음 ---")
    print(f"Post ID: {post_id}")
    print(f"수신 데이터: {data}")
    print("--------------------------")

    content = data.get('caption')
    image_urls = data.get('image_urls')

    if content is None or image_urls is None:
        return jsonify({"success": False, "message": "필수 정보가 누락되었습니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 2. 게시물 내용(P_content) 업데이트
            sql = "UPDATE posts SET P_content = %s WHERE post_id = %s"
            cursor.execute(sql, (content, post_id))

            # 3. 기존 이미지 목록을 모두 삭제
            sql = "DELETE FROM post_images WHERE post_id = %s"
            cursor.execute(sql, (post_id,))

            # 4. 새로 전달받은 이미지 목록을 다시 저장
            if image_urls:
                image_data = [(post_id, url, index) for index, url in enumerate(image_urls)]
                sql = "INSERT INTO post_images (post_id, image_url, image_order) VALUES (%s, %s, %s)"
                cursor.executemany(sql, image_data)
        
        conn.commit()
        return jsonify({"success": True, "message": "게시물이 수정되었습니다."})
    except Exception as e:
        conn.rollback()
        print(f"DB 오류 발생: {e}") # ✅ DB 오류 발생 시 터미널에 출력
        return jsonify({"success": False, "message": "게시물 수정 중 오류 발생"}), 500
    finally:
        conn.close()

@app.route('/posts/toggle-comments/<int:post_id>', methods=['POST'])
def toggle_comments(post_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 현재 상태를 가져와서 반대로 뒤집음 (true -> false, false -> true)
            sql = "UPDATE posts SET comments_disabled = NOT comments_disabled WHERE post_id = %s"
            cursor.execute(sql, (post_id,))
        conn.commit()
        return jsonify({"success": True, "message": "댓글 기능 상태가 변경되었습니다."})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

@app.route('/posts/toggle-likes/<int:post_id>', methods=['POST'])
def toggle_likes(post_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 현재 상태를 가져와서 반대로 뒤집음
            sql = "UPDATE posts SET likes_hidden = NOT likes_hidden WHERE post_id = %s"
            cursor.execute(sql, (post_id,))
        conn.commit()
        return jsonify({"success": True, "message": "좋아요 수 숨김 상태가 변경되었습니다."})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

# @app.route('/posts/like/<int:post_id>', methods=['POST'])
# def toggle_like(post_id):
#     data = request.get_json()
#     user_id = data.get('user_id')

#     if not user_id:
#         return jsonify({"success": False, "message": "사용자 정보가 필요합니다."}), 400

#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             # 먼저 사용자가 이미 좋아요를 눌렀는지 확인
#             sql = "SELECT * FROM likes WHERE post_id = %s AND user_id = %s"
#             cursor.execute(sql, (post_id, user_id))
#             is_liked = cursor.fetchone()

#             if is_liked:
#                 # 이미 좋아요를 눌렀다면 -> 좋아요 취소 (DELETE)
#                 sql = "DELETE FROM likes WHERE post_id = %s AND user_id = %s"
#                 cursor.execute(sql, (post_id, user_id))
#                 message = "좋아요를 취소했습니다."
#             else:
#                 # 좋아요를 누르지 않았다면 -> 좋아요 추가 (INSERT)
#                 sql = "INSERT INTO likes (post_id, user_id) VALUES (%s, %s)"
#                 cursor.execute(sql, (post_id, user_id))
#                 message = "게시물을 좋아합니다."
        
#         conn.commit()
#         return jsonify({"success": True, "message": message})
#     except Exception as e:
#         conn.rollback()
#         print(e)
#         return jsonify({"success": False, "message": "서버 오류"}), 500
#     finally:
#         conn.close()

@app.route('/posts/like/<int:post_id>', methods=['POST'])
def toggle_like(post_id):
    data = request.get_json()
    user_id = data.get('user_id')

    if not user_id:
        return jsonify({"success": False, "message": "사용자 정보가 필요합니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT * FROM likes WHERE post_id = %s AND user_id = %s"
            cursor.execute(sql, (post_id, user_id))
            is_liked = cursor.fetchone()

            if is_liked:
                sql = "DELETE FROM likes WHERE post_id = %s AND user_id = %s"
                cursor.execute(sql, (post_id, user_id))
                message = "좋아요를 취소했습니다."
            else:
                sql = "INSERT INTO likes (post_id, user_id) VALUES (%s, %s)"
                cursor.execute(sql, (post_id, user_id))
                message = "게시물을 좋아합니다."

                # ✅ [알림 기능 추가] 'like' 타입 알림 생성
                # 1. 게시물 작성자 ID 조회
                cursor.execute("SELECT user_id FROM posts WHERE post_id = %s", (post_id,))
                post_author_id = cursor.fetchone()['user_id']
                # 2. 자기 게시물이 아닐 때만 알림 생성
                if post_author_id != user_id:
                    sql_notify = "INSERT INTO alarms (user_id, actor_id, alarm_type, post_id) VALUES (%s, %s, 'like', %s)"
                    cursor.execute(sql_notify, (post_author_id, user_id, post_id))

        conn.commit()
        return jsonify({"success": True, "message": message})
    except Exception as e:
        conn.rollback()
        print(e)
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

# app.py

# ✅ [수정됨] 팔로우 기반으로 동작하는 최종 버전의 홈 피드 API
@app.route('/feed', methods=['GET'])
def get_feed():
    current_user_id = request.args.get('current_user_id')
    if not current_user_id:
        return jsonify({"success": False, "message": "사용자 정보가 필요합니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 팔로우/내 게시물 가져오는 SQL (수정 없음)
            sql = """
                SELECT 
                    p.post_id, p.P_content, p.P_created_at,
                    u.user_id as author_id, u.nickname, u.profile_image_url
                FROM posts p
                JOIN users u ON p.user_id = u.user_id
                WHERE (p.user_id IN (
                    SELECT following_id FROM follows WHERE follower_id = %s
                ) OR p.user_id = %s)
                  AND p.is_deleted = FALSE
                ORDER BY p.P_created_at DESC
            """
            cursor.execute(sql, (current_user_id, current_user_id))
            posts = cursor.fetchall()

            for post in posts:
                post_id = post['post_id']
                
                # 날짜 및 이미지 정보 (수정 없음)
                if post.get('P_created_at'):
                    post['P_created_at'] = post['P_created_at'].strftime('%Y-%m-%d %H:%M:%S')

                sql_images = "SELECT image_url FROM post_images WHERE post_id = %s ORDER BY image_order ASC"
                cursor.execute(sql_images, (post_id,))
                images = cursor.fetchall()
                post['images'] = [image['image_url'] for image in images]

                # 좋아요 정보 조회 (수정됨)
                try:
                    cursor.execute("SELECT COUNT(*) as count FROM likes WHERE post_id = %s", (post_id,))
                    like_result = cursor.fetchone()
                    # ✅ [수정] 좋아요가 0개일 때(None)를 대비한 안전장치 추가
                    post['like_count'] = like_result['count'] if like_result else 0
                    
                    cursor.execute("SELECT 1 FROM likes WHERE post_id = %s AND user_id = %s", (post_id, current_user_id))
                    post['is_liked_by_user'] = True if cursor.fetchone() else False
                except Exception as like_error:
                    post['like_count'] = 0
                    post['is_liked_by_user'] = False
                
                # ✅ [수정] 댓글 개수 계산 로직을 올바른 위치로 이동하고 안전장치 추가
                cursor.execute("SELECT COUNT(*) as count FROM comments WHERE post_id = %s", (post_id,))
                comment_result = cursor.fetchone()
                post['comment_count'] = comment_result['count'] if comment_result else 0
            
            return jsonify({"success": True, "posts": posts})
    except Exception as e:
        print(f"Error in get_feed: {e}")
        return jsonify({"success": False, "message": "피드 정보를 가져오는 중 오류 발생"}), 500
    finally:
        conn.close()

# @app.route('/users/<user_id>/likes', methods=['GET'])
# def get_liked_posts(user_id):
#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             # ✅ [수정된 SQL] 더 명확하고 안정적인 JOIN 쿼리 사용
#             sql = """
#                 SELECT 
#                     p.post_id, 
#                     p.P_content, 
#                     p.P_created_at,
#                     u.user_id as author_id, 
#                     u.nickname, 
#                     u.profile_image_url,
#                     (SELECT image_url FROM post_images WHERE post_id = p.post_id ORDER BY image_order ASC LIMIT 1) as thumbnail_url
#                 FROM likes l
#                 JOIN posts p ON l.post_id = p.post_id
#                 JOIN users u ON p.user_id = u.user_id
#                 WHERE l.user_id = %s AND p.is_deleted = FALSE
#                 ORDER BY l.L_created_at DESC
#             """
#             cursor.execute(sql, (user_id,))
#             posts = cursor.fetchall()

#             print(f"--- 좋아요 API ---")
#             print(f"사용자 '{user_id}'가 좋아요 누른 게시물 {len(posts)}개 발견")
            
#             for post in posts:
#                 post_id = post['post_id']
                
#                 if post.get('P_created_at'):
#                     post['P_created_at'] = post['P_created_at'].strftime('%Y-%m-%d %H:%M:%S')

#                 sql_images = "SELECT image_url FROM post_images WHERE post_id = %s ORDER BY image_order ASC"
#                 cursor.execute(sql_images, (post_id,))
#                 images = cursor.fetchall()
#                 post['images'] = [image['image_url'] for image in images]

#                 try:
#                     cursor.execute("SELECT COUNT(*) as count FROM likes WHERE post_id = %s", (post_id,))
#                     post['like_count'] = cursor.fetchone()['count']
#                     post['is_liked_by_user'] = True
#                 except Exception as like_error:
#                     post['like_count'] = 0
#                     post['is_liked_by_user'] = True # 좋아요 목록이므로 기본값 true
            
#             return jsonify({"success": True, "posts": posts})
#     except Exception as e:
#         # ✅ 이 오류가 Flask 터미널에 출력됩니다.
#         print(f"Error in get_liked_posts: {e}") 
#         return jsonify({"success": False, "message": "좋아요한 게시물을 가져오는 중 오류 발생"}), 500
#     finally:
#         conn.close()
@app.route('/likes/<user_id>', methods=['GET'])
def get_liked_posts(user_id):
    # 함수 내용은 이전과 동일합니다. 경로만 수정되었습니다.
    sort_order = request.args.get('sort', 'newest')
    date_filter = request.args.get('date', 'all')
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            base_sql = """
                SELECT 
                    p.post_id, p.P_content, p.P_created_at,
                    u.user_id as author_id, u.nickname, u.profile_image_url,
                    (SELECT image_url FROM post_images WHERE post_id = p.post_id ORDER BY image_order ASC LIMIT 1) as thumbnail_url
                FROM likes l
                JOIN posts p ON l.post_id = p.post_id
                JOIN users u ON p.user_id = u.user_id
                WHERE l.user_id = %s AND p.is_deleted = FALSE
            """
            params = [user_id]
            if date_filter == 'week':
                base_sql += " AND l.L_created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
            elif date_filter == 'month':
                base_sql += " AND l.L_created_at >= DATE_SUB(NOW(), INTERVAL 1 MONTH)"
            
            if sort_order == 'oldest':
                base_sql += " ORDER BY l.L_created_at ASC"
            else: 
                base_sql += " ORDER BY l.L_created_at DESC"
            
            cursor.execute(base_sql, params)
            posts = cursor.fetchall()
            
            for post in posts:
                post_id = post['post_id']
                if post.get('P_created_at'):
                    post['P_created_at'] = post['P_created_at'].strftime('%Y-%m-%d %H:%M:%S')
                sql_images = "SELECT image_url FROM post_images WHERE post_id = %s ORDER BY image_order ASC"
                cursor.execute(sql_images, (post_id,))
                images = cursor.fetchall()
                post['images'] = [image['image_url'] for image in images]
                try:
                    cursor.execute("SELECT COUNT(*) as count FROM likes WHERE post_id = %s", (post_id,))
                    post['like_count'] = cursor.fetchone()['count']
                    post['is_liked_by_user'] = True 
                except Exception as like_error:
                    post['like_count'] = 0
                    post['is_liked_by_user'] = True
            
            return jsonify({"success": True, "posts": posts})
    except Exception as e:
        print(f"Error in get_liked_posts: {e}") 
        return jsonify({"success": False, "message": "좋아요한 게시물을 가져오는 중 오류 발생"}), 500
    finally:
        conn.close()

@app.route('/likes/batch-delete', methods=['POST'])
def unlike_posts_batch():
    data = request.get_json()
    user_id = data.get('user_id')
    post_ids = data.get('post_ids')

    if not user_id or not post_ids:
        return jsonify({"success": False, "message": "필수 정보가 누락되었습니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # post_ids 리스트를 SQL의 IN 절에서 사용할 수 있도록 포맷팅
            # post_ids가 (1, 2, 3) 형태가 되도록 함
            placeholders = ','.join(['%s'] * len(post_ids))
            sql = f"DELETE FROM likes WHERE user_id = %s AND post_id IN ({placeholders})"
            
            # user_id와 post_ids 리스트를 합쳐서 쿼리 파라미터로 전달
            params = [user_id] + post_ids
            cursor.execute(sql, params)
        conn.commit()
        return jsonify({"success": True, "message": "선택한 게시물의 좋아요를 취소했습니다."})
    except Exception as e:
        conn.rollback()
        print(f"Error in unlike_posts_batch: {e}")
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()   

@app.route('/posts/<int:post_id>/comments', methods=['GET'])
def get_comments(post_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 사용자 DB의 컬럼명(C_content, C_created_at)으로 SQL 쿼리 수정
            sql = """
                SELECT 
                    c.comment_id, c.user_id, c.C_content AS content, c.C_created_at AS created_at,
                    u.nickname, u.profile_image_url
                FROM comments c
                JOIN users u ON c.user_id = u.user_id
                WHERE c.post_id = %s
                ORDER BY c.C_created_at ASC
            """
            cursor.execute(sql, (post_id,))
            comments = cursor.fetchall()
            for comment in comments:
                if comment.get('created_at'):
                    comment['created_at'] = comment['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            return jsonify({"success": True, "comments": comments})
    except Exception as e:
        print(f"Error in get_comments: {e}")
        return jsonify({"success": False, "message": "댓글 오류"}), 500
    finally:
        conn.close()

# [추가] 특정 게시물에 새 댓글을 추가하는 API
# @app.route('/posts/<int:post_id>/comments', methods=['POST'])
# def add_comment(post_id):
#     data = request.get_json()
#     user_id = data.get('user_id')
#     content = data.get('content')
#     if not user_id or not content:
#         return jsonify({"success": False, "message": "필수 정보 누락"}), 400
#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             # 사용자 DB의 컬럼명(C_content)으로 SQL 쿼리 수정
#             sql = "INSERT INTO comments (post_id, user_id, C_content) VALUES (%s, %s, %s)"
#             cursor.execute(sql, (post_id, user_id, content))
#         conn.commit()
#         return jsonify({"success": True, "message": "댓글이 등록되었습니다."})
#     except Exception as e:
#         conn.rollback()
#         print(f"Error in add_comment: {e}")
#         return jsonify({"success": False, "message": "댓글 등록 오류"}), 500
#     finally:
#         conn.close()

@app.route('/posts/<int:post_id>/comments', methods=['POST'])
def add_comment(post_id):
    data = request.get_json()
    user_id = data.get('user_id')
    content = data.get('content')
    if not user_id or not content:
        return jsonify({"success": False, "message": "필수 정보 누락"}), 400
    
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "INSERT INTO comments (post_id, user_id, C_content) VALUES (%s, %s, %s)"
            cursor.execute(sql, (post_id, user_id, content))

            # ✅ [알림 기능 추가] 'comment' 타입 알림 생성
            # 1. 게시물 작성자 ID 조회
            cursor.execute("SELECT user_id FROM posts WHERE post_id = %s", (post_id,))
            post_author_id = cursor.fetchone()['user_id']
            # 2. 자기 게시물이 아닐 때만 알림 생성
            if post_author_id != user_id:
                sql_notify = "INSERT INTO alarms (user_id, actor_id, alarm_type, post_id) VALUES (%s, %s, 'comment', %s)"
                cursor.execute(sql_notify, (post_author_id, user_id, post_id))

        conn.commit()
        return jsonify({"success": True, "message": "댓글이 등록되었습니다."})
    except Exception as e:
        conn.rollback()
        print(f"Error in add_comment: {e}")
        return jsonify({"success": False, "message": "댓글 등록 오류"}), 500
    finally:
        conn.close()

@app.route('/posts/<int:post_id>/restore', methods=['POST'])
def restore_post(post_id):
    """게시물을 복원 상태로 변경하는 API"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE posts SET is_deleted = FALSE, deleted_at = NULL WHERE post_id = %s"
            rows_affected = cursor.execute(sql, (post_id,))
        conn.commit()
        if rows_affected > 0:
            return jsonify({"success": True, "message": "게시물이 복원되었습니다."})
        else:
            return jsonify({"success": False, "message": "복원할 게시물을 찾을 수 없습니다."}), 404
    except Exception as e:
        conn.rollback()
        print(f"Error in restore_post: {e}")
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()


def _execute_permanent_delete(post_id, conn):
    """한 개의 게시물을 영구 삭제하는 내부 로직"""
    try:
        with conn.cursor() as cursor:
            # 1. Firebase Storage에서 관련 이미지들 삭제
            sql_get_images = "SELECT image_url FROM post_images WHERE post_id = %s"
            cursor.execute(sql_get_images, (post_id,))
            images_to_delete = cursor.fetchall()

            bucket = storage.bucket()
            for image in images_to_delete:
                if image['image_url']:
                    try:
                        full_url = image['image_url']
                        start_key = "/o/"
                        end_key = "?alt=media"
                        start_index = full_url.find(start_key)
                        end_index = full_url.find(end_key)
                        if start_index != -1 and end_index != -1:
                            encoded_path = full_url[start_index + len(start_key) : end_index]
                            file_path = unquote(encoded_path)
                            blob = bucket.blob(file_path)
                            if blob.exists():
                                blob.delete()
                    except Exception as e:
                        print(f"Firebase 파일 삭제 중 오류 (무시): {e}")

            # 2. 데이터베이스에서 관련 데이터 삭제 (순서가 중요!)
            cursor.execute("DELETE FROM likes WHERE post_id = %s", (post_id,))
            cursor.execute("DELETE FROM comments WHERE post_id = %s", (post_id,))
            cursor.execute("DELETE FROM post_images WHERE post_id = %s", (post_id,))
            rows_affected = cursor.execute("DELETE FROM posts WHERE post_id = %s", (post_id,))
            return rows_affected > 0
    except Exception as e:
        print(f"영구 삭제 함수(_execute_permanent_delete) 오류: {e}")
        return False


@app.route('/posts/<int:post_id>/permanent-delete', methods=['DELETE'])
def permanent_delete_post(post_id):
    """게시물을 데이터베이스에서 영구적으로 삭제하는 API"""
    conn = get_connection()
    try:
        success = _execute_permanent_delete(post_id, conn)
        conn.commit()
        if success:
            return jsonify({"success": True, "message": "게시물이 영구적으로 삭제되었습니다."})
        else:
            return jsonify({"success": False, "message": "삭제할 게시물을 찾을 수 없습니다."}), 404
    except Exception as e:
        conn.rollback()
        print(f"Error in permanent_delete_post: {e}")
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500
    finally:
        conn.close()


def auto_delete_old_posts_job():
    """소프트 삭제된 지 30일이 지난 게시물을 찾아 영구 삭제하는 스케줄링 작업"""
    print(f"[{datetime.now()}] === 자동 삭제 작업 시작 ===")
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            # 삭제된 지 30일이 지난 게시물 ID 목록을 찾습니다.
            sql_find_old = "SELECT post_id FROM posts WHERE is_deleted = TRUE AND deleted_at < NOW() - INTERVAL 30 DAY"
            cursor.execute(sql_find_old)
            posts_to_delete = cursor.fetchall()
            
            if not posts_to_delete:
                print("삭제할 오래된 게시물이 없습니다.")
                return

            print(f"자동 삭제 대상 게시물: {[p['post_id'] for p in posts_to_delete]}")

            # 각 게시물에 대해 영구 삭제 함수를 실행합니다.
            for post in posts_to_delete:
                post_id = post['post_id']
                _execute_permanent_delete(post_id, conn)
        
        conn.commit()
        print("=== 자동 삭제 작업 완료 ===")
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"자동 삭제 작업 중 오류 발생: {e}")
    finally:
        if conn:
            conn.close()

# @app.route('/comments/<user_id>', methods=['GET'])
# def get_user_comments(user_id):
#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             # 사용자가 작성한 댓글을 게시물 썸네일과 함께 가져오기
#             sql = """
#                 SELECT 
#                     c.comment_id, c.C_content AS content, c.C_created_at AS created_at,
#                     p.post_id, u.nickname, u.profile_image_url,
#                     (SELECT image_url FROM post_images WHERE post_id = p.post_id ORDER BY image_order ASC LIMIT 1) as post_thumbnail_url
#                 FROM comments c
#                 JOIN posts p ON c.post_id = p.post_id
#                 JOIN users u ON c.user_id = u.user_id
#                 WHERE c.user_id = %s
#                 ORDER BY c.C_created_at DESC
#             """
#             cursor.execute(sql, (user_id,))
#             comments = cursor.fetchall()

#             # 날짜 형식 변환
#             for comment in comments:
#                 if comment.get('C_created_at'):
#                     comment['C_created_at'] = comment['C_created_at'].strftime('%Y-%m-%d %H:%M:%S')

#             return jsonify({"success": True, "comments": comments})
#     except Exception as e:
#         print(e)
#         return jsonify({"success": False, "message": "서버 오류"}), 500
#     finally:
#         conn.close()
@app.route('/comments/user/<user_id>', methods=['GET'])
def get_user_comments(user_id):
    # 함수 내용은 이전과 동일합니다. 경로만 수정되었습니다.
    sort_order = request.args.get('sort', 'newest')
    date_filter = request.args.get('date', 'all')
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            base_sql = """
                SELECT 
                    c.comment_id, c.C_content AS content, c.C_created_at AS created_at,
                    p.post_id, u.nickname, u.profile_image_url,
                    (SELECT image_url FROM post_images WHERE post_id = p.post_id ORDER BY image_order ASC LIMIT 1) as post_thumbnail_url
                FROM comments c
                JOIN posts p ON c.post_id = p.post_id
                JOIN users u ON c.user_id = u.user_id
                WHERE c.user_id = %s
            """
            params = [user_id]
            if date_filter == 'week':
                base_sql += " AND c.C_created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
            elif date_filter == 'month':
                base_sql += " AND c.C_created_at >= DATE_SUB(NOW(), INTERVAL 1 MONTH)"
            
            if sort_order == 'oldest':
                base_sql += " ORDER BY c.C_created_at ASC"
            else:
                base_sql += " ORDER BY c.C_created_at DESC"
            
            cursor.execute(base_sql, params)
            comments = cursor.fetchall()

            for comment in comments:
                if comment.get('created_at'):
                    comment['created_at'] = comment['created_at'].strftime('%Y-%m-%d %H:%M:%S')

            return jsonify({"success": True, "comments": comments})
    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

@app.route('/stories', methods=['POST'])
def create_story():
    """새로운 스토리를 DB에 추가하는 API"""
    data = request.get_json()
    user_id = data.get('userId')
    image_url = data.get('image_url')

    if not user_id or not image_url:
        return jsonify({"success": False, "message": "필수 정보가 누락되었습니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # users.user_id는 VARCHAR, stories.stories_id는 BIGINT 이므로
            # stories 테이블의 user_id도 VARCHAR로 생성해야 합니다.
            sql = "INSERT INTO stories (user_id, image_url) VALUES (%s, %s)"
            cursor.execute(sql, (user_id, image_url))
        conn.commit()
        return jsonify({"success": True, "message": "스토리가 성공적으로 저장되었습니다."}), 201
    except Exception as e:
        conn.rollback()
        print(f"Error in create_story: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

@app.route('/feed/stories', methods=['GET'])
def get_feed_stories():
    """홈 피드 상단에 표시될 스토리 목록을 가져오는 API (내 스토리 포함)"""
    current_user_id = request.args.get('current_user_id')
    if not current_user_id:
        return jsonify({"success": False, "message": "사용자 정보가 필요합니다."}), 400

    conn = get_connection()
    final_story_list = []

    try:
        with conn.cursor() as cursor:
            # ✅ 1단계: '내 스토리' 정보만 가져오는 간단한 쿼리 실행
            sql_my_story = """
                SELECT
                    u.user_id,
                    '내 스토리' AS username,
                    u.profile_image_url,
                    CAST(IF(COUNT(DISTINCT CASE WHEN sv.viewer_id IS NULL THEN s.story_id END) > 0, 1, 0) AS JSON) as hasUnseenStory
                FROM users u
                LEFT JOIN stories s ON u.user_id = s.user_id AND s.created_at >= NOW() - INTERVAL 1 DAY
                LEFT JOIN story_views sv ON s.story_id = sv.story_id AND sv.viewer_id = u.user_id
                WHERE u.user_id = %s
                GROUP BY u.user_id, u.profile_image_url
            """
            cursor.execute(sql_my_story, (current_user_id,))
            my_story = cursor.fetchone()
            if my_story:
                final_story_list.append(my_story)

            # ✅ 2단계: '친구 스토리' 정보만 가져오는 간단한 쿼리 실행
            sql_friends_stories = """
                SELECT
                    u.user_id,
                    u.nickname AS username,
                    u.profile_image_url,
                    CAST(IF(COUNT(DISTINCT CASE WHEN sv.viewer_id IS NULL THEN s.story_id END) > 0, 1, 0) AS JSON) as hasUnseenStory
                FROM users u
                JOIN follows f ON u.user_id = f.following_id
                JOIN stories s ON u.user_id = s.user_id
                LEFT JOIN story_views sv ON s.story_id = sv.story_id AND sv.viewer_id = %s
                WHERE f.follower_id = %s AND s.created_at >= NOW() - INTERVAL 1 DAY
                GROUP BY u.user_id, u.nickname, u.profile_image_url
            """
            cursor.execute(sql_friends_stories, (current_user_id, current_user_id))
            friends_stories = cursor.fetchall()
            if friends_stories:
                final_story_list.extend(friends_stories)
            
            # ✅ 3단계: Python에서 두 결과를 합쳐서 최종 응답 생성
            return jsonify({"success": True, "stories": final_story_list})

    except Exception as e:
        print(f"Error in get_feed_stories: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

@app.route('/users/<user_id>/stories', methods=['GET'])
def get_user_stories(user_id):
    """특정 사용자의 24시간 내 스토리 목록과 '시청 시작 인덱스'를 가져오는 API"""
    
    # ✅ [수정] '누가' 보는지 viewer_id를 파라미터로 받습니다.
    viewer_id = request.args.get('viewer_id')

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 24시간 내 유효한 스토리 목록을 시간 순으로 가져오는 것은 동일합니다.
            sql = """
                SELECT
                    s.story_id AS id, s.image_url, s.created_at,
                    u.nickname AS username, u.profile_image_url
                FROM stories s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.user_id = %s AND s.created_at >= NOW() - INTERVAL 1 DAY
                ORDER BY s.created_at ASC
            """
            cursor.execute(sql, (user_id,))
            stories = cursor.fetchall()

            start_index = 0 # 기본 시작 위치는 0

            # ✅ [추가] viewer_id가 있고, 스토리 목록이 있을 경우에만 계산
            if viewer_id and stories:
                story_ids = [s['id'] for s in stories]
                
                # viewer가 본 스토리 ID 목록을 DB에서 가져옵니다.
                format_strings = ','.join(['%s'] * len(story_ids))
                sql_seen = f"SELECT story_id FROM story_views WHERE viewer_id = %s AND story_id IN ({format_strings})"
                cursor.execute(sql_seen, (viewer_id, *story_ids))
                
                seen_story_ids = {row['story_id'] for row in cursor.fetchall()}
                
                # 전체 스토리 목록을 순회하며, 아직 보지 않은 첫 스토리의 인덱스를 찾습니다.
                for i, story in enumerate(stories):
                    if story['id'] not in seen_story_ids:
                        start_index = i
                        break 
                else:
                    # 모든 스토리를 다 봤다면 그냥 처음부터 보여줍니다.
                    start_index = 0
            
            # 날짜 형식 변환 (기존 코드 유지)
            for story in stories:
                if story.get('created_at'):
                    story['created_at'] = story['created_at'].strftime('%Y-%m-%d %H:%M:%S')

            # ✅ [수정] 최종적으로 'stories' 목록과 'startIndex'를 함께 반환합니다.
            return jsonify({"success": True, "stories": stories, "startIndex": start_index})

    except Exception as e:
        print(f"Error in get_user_stories: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

@app.route('/stories/<int:story_id>/view', methods=['POST'])
def mark_story_as_seen(story_id):
    """스토리를 '봤음'으로 기록하는 API"""
    data = request.get_json()
    viewer_id = data.get('viewerId')

    if not viewer_id:
        return jsonify({"success": False, "message": "viewerId가 필요합니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "INSERT IGNORE INTO story_views (story_id, viewer_id) VALUES (%s, %s)"
            cursor.execute(sql, (story_id, viewer_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        print(f"Error in mark_story_as_seen: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

@app.route('/stories/<int:story_id>', methods=['DELETE'])
def delete_story(story_id):
    """특정 스토리를 데이터베이스에서 삭제하는 API"""
    conn = None # 먼저 None으로 초기화
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            # 먼저, story_views 테이블의 관련 데이터를 삭제 (외래 키 제약이 있는 경우)
            cursor.execute("DELETE FROM story_views WHERE story_id = %s", (story_id,))
            
            # stories 테이블에서 해당 스토리 삭제
            rows_affected = cursor.execute("DELETE FROM stories WHERE story_id = %s", (story_id,))
            
        conn.commit()
        
        if rows_affected > 0:
            return jsonify({"success": True, "message": "스토리가 삭제되었습니다."})
        else:
            # 삭제 대상이 없었을 경우, 오류는 아니지만 클라이언트에 알림
            return jsonify({"success": False, "message": "삭제할 스토리를 찾을 수 없습니다."}), 404

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error in delete_story: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn:
            conn.close()

@app.route('/stories/<int:story_id>/viewers', methods=['GET'])
def get_story_viewers(story_id):
    """특정 스토리를 본 사용자들의 목록을 가져오는 API"""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            # story_views 테이블과 users 테이블을 JOIN하여 뷰어의 정보를 가져옴
            sql = """
                SELECT
                    u.user_id,
                    u.nickname,
                    u.profile_image_url
                FROM story_views sv
                JOIN users u ON sv.viewer_id = u.user_id
                WHERE sv.story_id = %s
                ORDER BY sv.viewed_at DESC
            """
            cursor.execute(sql, (story_id,))
            viewers = cursor.fetchall()
            
            return jsonify({"success": True, "viewers": viewers})

    except Exception as e:
        print(f"Error in get_story_viewers: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn:
            conn.close()

@app.route('/posts/explore', methods=['GET'])
def get_explore_posts():
    current_user_id = request.args.get('current_user_id')
    if not current_user_id:
        return jsonify([]), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # ▼▼▼ [수정] "OR p.user_id = %s" 조건을 추가하여 내 게시물도 포함시킵니다. ▼▼▼
            sql = """
                SELECT 
                    p.post_id,
                    (SELECT image_url FROM post_images WHERE post_id = p.post_id ORDER BY image_order ASC LIMIT 1) as thumbnail_url
                FROM posts p
                WHERE (p.user_id IN (
                    SELECT following_id FROM follows WHERE follower_id = %s
                ) OR p.user_id = %s) AND p.is_deleted = FALSE
                ORDER BY RAND() 
            """
            # ▼▼▼ 파라미터도 2개로 늘어납니다. ▼▼▼
            cursor.execute(sql, (current_user_id, current_user_id))
            posts = cursor.fetchall()
            return jsonify(posts)
    except Exception as e:
        print(f"Error in get_explore_posts: {e}")
        return jsonify([]), 500
    finally:
        conn.close()

# ✅ [추가] 사용자 검색 API
@app.route('/search/users', methods=['GET'])
def search_users():
    query = request.args.get('query', '')
    if not query:
        return jsonify({"success": True, "users": []})

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 닉네임 또는 이름에 검색어가 포함된 사용자를 찾음
            sql = """
                SELECT user_id, name, nickname, profile_image_url 
                FROM users 
                WHERE nickname LIKE %s OR name LIKE %s
            """
            search_query = f"%{query}%"
            cursor.execute(sql, (search_query, search_query))
            users = cursor.fetchall()
            return jsonify({"success": True, "users": users})
    except Exception as e:
        print(f"Error in search_users: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()          

@app.route('/follow-status', methods=['GET'])
def get_follow_status():
    # 안드로이드에서 보낸 두 개의 ID를 받음
    # follower_id: 현재 프로필을 보고 있는 사람 (나)
    # following_id: 보여지고 있는 프로필의 주인 (상대방)
    follower_id = request.args.get('follower_id')
    following_id = request.args.get('following_id')

    if not follower_id or not following_id:
        return jsonify({"success": False, "message": "필수 정보가 누락되었습니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 1. 내가 상대방을 팔로우하는지 확인 (isFollowing)
            sql_is_following = "SELECT COUNT(*) as count FROM follows WHERE follower_id = %s AND following_id = %s"
            cursor.execute(sql_is_following, (follower_id, following_id))
            is_following = cursor.fetchone()['count'] > 0
            
            # ▼▼▼ [추가] 2. 상대방이 나를 팔로우하는지 확인 (isFollowedBy) ▼▼▼
            sql_is_followed_by = "SELECT COUNT(*) as count FROM follows WHERE follower_id = %s AND following_id = %s"
            cursor.execute(sql_is_followed_by, (following_id, follower_id)) # ID 순서 바뀜
            is_followed_by = cursor.fetchone()['count'] > 0

            # ▼▼▼ [수정] 이제 두 가지 정보를 함께 응답으로 보내줌 ▼▼▼
            return jsonify({
                "success": True, 
                "isFollowing": is_following,
                "isFollowedBy": is_followed_by 
            })
            
    except Exception as e:
        print(f"Error in get_follow_status: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()


# 2. 팔로우 또는 언팔로우를 처리하는 API
# @app.route('/toggle-follow', methods=['POST'])
# def toggle_follow():
#     data = request.get_json()
#     follower_id = data.get('followerId')
#     following_id = data.get('followingId')

#     if not follower_id or not following_id:
#         return jsonify({"success": False, "message": "필수 정보가 누락되었습니다."}), 400

#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             # 먼저 이미 팔로우 중인지 확인
#             sql = "SELECT follow_id FROM follows WHERE follower_id = %s AND following_id = %s"
#             cursor.execute(sql, (follower_id, following_id))
#             is_following = cursor.fetchone()

#             if is_following:
#                 # 이미 팔로우 중이면 -> 언팔로우 (DELETE)
#                 sql_delete = "DELETE FROM follows WHERE follower_id = %s AND following_id = %s"
#                 cursor.execute(sql_delete, (follower_id, following_id))
#                 new_status = False
#             else:
#                 # 팔로우 중이 아니면 -> 팔로우 (INSERT)
#                 sql_insert = "INSERT INTO follows (follower_id, following_id) VALUES (%s, %s)"
#                 cursor.execute(sql_insert, (follower_id, following_id))
#                 new_status = True
            
#             conn.commit()

#             # 변경 후의 팔로워 수를 다시 계산해서 안드로이드에 보내줌
#             sql_count = "SELECT COUNT(*) as count FROM follows WHERE following_id = %s"
#             cursor.execute(sql_count, (following_id,))
#             follower_count = cursor.fetchone()['count']

#             return jsonify({
#                 "success": True, 
#                 "isFollowing": new_status,
#                 "followerCount": follower_count
#             })

#     except Exception as e:
#         conn.rollback()
#         print(f"Error in toggle_follow: {e}")
#         return jsonify({"success": False, "message": "서버 오류"}), 500
#     finally:
#         conn.close()

@app.route('/toggle-follow', methods=['POST'])
def toggle_follow():
    data = request.get_json()
    follower_id = data.get('followerId')
    following_id = data.get('followingId')

    if not follower_id or not following_id:
        return jsonify({"success": False, "message": "필수 정보가 누락되었습니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT follow_id FROM follows WHERE follower_id = %s AND following_id = %s"
            cursor.execute(sql, (follower_id, following_id))
            is_following = cursor.fetchone()

            if is_following:
                sql_delete = "DELETE FROM follows WHERE follower_id = %s AND following_id = %s"
                cursor.execute(sql_delete, (follower_id, following_id))
                new_status = False
            else:
                sql_insert = "INSERT INTO follows (follower_id, following_id) VALUES (%s, %s)"
                cursor.execute(sql_insert, (follower_id, following_id))
                new_status = True
                
                # ✅ [알림 기능 추가] 'follow' 타입 알림 생성
                sql_notify = "INSERT INTO alarms (user_id, actor_id, alarm_type) VALUES (%s, %s, 'follow')"
                cursor.execute(sql_notify, (following_id, follower_id))
            
            conn.commit()

            sql_count = "SELECT COUNT(*) as count FROM follows WHERE following_id = %s"
            cursor.execute(sql_count, (following_id,))
            follower_count = cursor.fetchone()['count']

            return jsonify({
                "success": True, 
                "isFollowing": new_status,
                "followerCount": follower_count
            })

    except Exception as e:
        conn.rollback()
        print(f"Error in toggle_follow: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

# @app.route('/notifications', methods=['GET'])
# def get_notifications():
#     current_user_id = request.args.get('user_id')
#     if not current_user_id:
#         return jsonify({"success": False, "message": "사용자 정보가 필요합니다."}), 400

#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             # 쿼리는 기존과 동일합니다.
#             sql = """
#                 SELECT
#                     a.alarm_id, a.alarm_type, a.alarmed_at,
#                     actor.user_id as actor_id,
#                     actor.nickname as actor_nickname,
#                     actor.profile_image_url as actor_profile_image,
#                     p.post_id,
#                     (SELECT image_url FROM post_images WHERE post_id = p.post_id ORDER BY image_order ASC LIMIT 1) as post_thumbnail_url,
#                     EXISTS(SELECT 1 FROM follows WHERE follower_id = %s AND following_id = actor.user_id) as is_following
#                 FROM alarms a
#                 JOIN users actor ON a.actor_id = actor.user_id
#                 LEFT JOIN posts p ON a.post_id = p.post_id
#                 WHERE a.user_id = %s
#                 ORDER BY a.alarmed_at DESC
#             """
#             cursor.execute(sql, (current_user_id, current_user_id))
#             notifications = cursor.fetchall()

#             # ▼▼▼ [수정] 날짜 형식 변환 및 boolean 값 변환을 추가하여 안정성을 높입니다. ▼▼▼
#             for notif in notifications:
#                 if notif.get('alarmed_at'):
#                     notif['alarmed_at'] = notif['alarmed_at'].strftime('%Y-%m-%d %H:%M:%S')
#                 # MySQL의 0/1 값을 Python의 False/True로 변환
#                 notif['is_following'] = bool(notif['is_following'])

#             return jsonify({"success": True, "notifications": notifications})
            
#     except Exception as e:
#         print(f"Error in get_notifications: {e}")
#         return jsonify({"success": False, "message": "서버 오류"}), 500
#     finally:
#         conn.close()
@app.route('/notifications', methods=['GET'])
def get_notifications():
    current_user_id = request.args.get('user_id')
    if not current_user_id:
        return jsonify({"success": False, "message": "사용자 정보가 필요합니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 1. 먼저 '안 읽음' 상태가 포함된 알림 목록을 가져옵니다.
            sql_select = """
                SELECT
                    a.alarm_id, a.alarm_type, a.alarmed_at, a.is_read,
                    actor.user_id as actor_id,
                    actor.nickname as actor_nickname,
                    actor.profile_image_url as actor_profile_image,
                    p.post_id,
                    (SELECT image_url FROM post_images WHERE post_id = p.post_id ORDER BY image_order ASC LIMIT 1) as post_thumbnail_url,
                    EXISTS(SELECT 1 FROM follows WHERE follower_id = %s AND following_id = actor.user_id) as is_following
                FROM alarms a
                JOIN users actor ON a.actor_id = actor.user_id
                LEFT JOIN posts p ON a.post_id = p.post_id
                WHERE a.user_id = %s
                ORDER BY a.alarmed_at DESC
            """
            cursor.execute(sql_select, (current_user_id, current_user_id))
            notifications = cursor.fetchall()

            for notif in notifications:
                if notif.get('alarmed_at'):
                    notif['alarmed_at'] = notif['alarmed_at'].strftime('%Y-%m-%d %H:%M:%S')
                notif['is_following'] = bool(notif['is_following'])
                notif['is_read'] = bool(notif['is_read'])

            # 2. 알림 목록을 가져간 후, 해당 유저의 모든 알림을 '읽음' 상태로 업데이트합니다.
            sql_update = "UPDATE alarms SET is_read = TRUE WHERE user_id = %s AND is_read = FALSE"
            cursor.execute(sql_update, (current_user_id,))
            conn.commit()

            return jsonify({"success": True, "notifications": notifications})

    except Exception as e:
        if conn: conn.rollback()
        print(f"Error in get_notifications: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        if conn: conn.close()


@app.route('/notifications/check-new', methods=['GET'])
def check_new_notifications():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"success": False, "message": "사용자 ID가 필요합니다."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # is_read가 FALSE인 알림이 하나라도 있는지 확인
            sql = "SELECT EXISTS(SELECT 1 FROM alarms WHERE user_id = %s AND is_read = FALSE) as has_new"
            cursor.execute(sql, (user_id,))
            result = cursor.fetchone()
            has_new = bool(result['has_new'])
            return jsonify({"success": True, "has_new": has_new})
    except Exception as e:
        print(f"Error in check_new_notifications: {e}")
        return jsonify({"success": False, "message": "서버 오류"}), 500
    finally:
        conn.close()

# @app.route('/notifications/read', methods=['POST'])
# def read_notification():
#     data = request.get_json()
    
#     # ▼▼▼ [핵심 수정] 'notification_id'를 'alarm_id'로 변경 ▼▼▼
#     alarm_id = data.get('alarm_id')
#     # ▲▲▲ [핵심 수정] ▲▲▲
    
#     user_id = data.get('user_id')

#     if not alarm_id or not user_id:
#         return jsonify({"success": False, "message": "알림 ID와 사용자 ID가 필요합니다."}), 400

#     conn = get_connection()
#     try:
#         with conn.cursor() as cursor:
#             sql = "UPDATE alarms SET is_read = TRUE WHERE alarm_id = %s AND user_id = %s"
#             cursor.execute(sql, (alarm_id, user_id))
#             conn.commit()
#             if cursor.rowcount > 0:
#                 return jsonify({"success": True})
#             else:
#                 return jsonify({"success": False, "message": "해당 알림을 찾을 수 없거나 사용자에게 권한이 없습니다."}), 404
#     except Exception as e:
#         if conn: conn.rollback()
#         print(f"Error in read_notification: {e}")
#         return jsonify({"success": False, "message": "서버 오류"}), 500
#     finally:
#         if conn: conn.close()
# 테스트용 경로
@app.route('/test', methods=['GET'])
def test_route():
    return "Flask server is running and updated!"

# 서버 실행
if __name__ == '__main__':
    # 백그라운드 스케줄러 생성
    scheduler = BackgroundScheduler(daemon=True)
    
    # 'auto_delete_old_posts_job' 함수를 매일 03시 00분에 실행하도록 등록
    scheduler.add_job(auto_delete_old_posts_job, 'cron', hour=0, minute=0)
    
    # 스케줄러 시작
    scheduler.start()
    
    # Flask 앱 실행
    print("Flask 서버와 Socket.IO 서버를 함께 시작합니다. 자동 삭제는 매일 자정 0시에 실행됩니다.")
    # app.run(host='0.0.0.0', port=5050, debug=True)
    socketio.run(app, host='0.0.0.0', port=5050, debug=True)
# if __name__ == '__main__':
#     scheduler = BackgroundScheduler(daemon=True)
#     # ✅ 테스트를 위해 1분마다 실행되도록 변경
#     scheduler.add_job(auto_delete_old_posts_job, 'interval', minutes=1)
#     scheduler.start()
#     print("Flask 서버와 자동 삭제 스케줄러를 시작합니다. ")
#     app.run(host='0.0.0.0', port=5050, debug=True)