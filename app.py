# app.py

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import time
import json
import random
import os

# --- 1. 앱 초기 설정 및 데이터 구조 ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'auction_system_secret_key_2025' 
# 웹소켓을 위한 gevent, gevent-websocket 설치 권장 (호스팅 시 중요)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# 초기 팀장 데이터
MANAGERS = {
    'T-001': {'id': 'T01', 'name': '건우', 'coin': 1000, 'team': {}, 'is_online': False},
    'T-002': {'id': 'T02', 'name': '성무', 'coin': 1000, 'team': {}, 'is_online': False},
    'T-003': {'id': 'T03', 'name': '원교', 'coin': 1000, 'team': {}, 'is_online': False},
}
ADMIN_OTP = 'A-999'

# 경매 대상 선수 데이터
PLAYERS_DATA = {
    'A': ['경민', '대균', '호준'],
    'B': ['민재', '현준', '범수'],
    'C': ['성민', '태연', '선우'],
    'D': ['진호', '준석', '백건'],
}

# 경매 상태
AUCTION_STATE = {
    'status': 'READY',      # READY, BIDDING, PAUSED, ENDED
    'current_tier': '',
    'player_index': 0,      
    'current_player': '',
    'current_price': 0,
    'leading_manager_id': None, 
    'timer_end': 0,         
    'player_list': [],      
    'is_started': False
}

def initialize_players():
    """티어 구분 없이 모든 선수를 가져와 완전히 무작위로 섞어 경매 순서를 설정"""
    all_players = []
    for tier, names in PLAYERS_DATA.items():
        all_players.extend([{'tier': tier, 'name': name} for name in names])
    
    # 전체 목록을 무작위로 섞습니다.
    random.shuffle(all_players)
        
    AUCTION_STATE['player_list'] = all_players
    if all_players:
        AUCTION_STATE['current_player'] = all_players[0]['name']
        AUCTION_STATE['current_tier'] = all_players[0]['tier']

initialize_players()


# --- 2. 핵심 로직: 자동 귀속 시스템 ---
def check_and_apply_autoclaim(tier):
    """
    [자동 귀속 규칙]
    - 해당 티어의 선수가 '1명만' 남았고
    - 아직 그 티어 선수를 가져가지 못한 팀장도 '1명만' 남았을 때
      → 그 팀장에게 남은 1명을 자동 낙찰시킨다.
    """
    # 인덱스가 범위 밖이면 바로 종료
    if AUCTION_STATE['player_index'] >= len(AUCTION_STATE['player_list']):
        return False

    # 1. 현재 인덱스부터 끝까지, 이 티어에 남은 선수 수
    remaining_in_tier = sum(
        1
        for p in AUCTION_STATE['player_list'][AUCTION_STATE['player_index']:]
        if p['tier'] == tier
    )

    # 2. 이 티어 선수를 아직 한 명도 못 가진 팀장 목록
    free_managers_otp = []
    for otp, manager in MANAGERS.items():
        if not any(p['tier'] == tier for p in manager['team'].values()):
            free_managers_otp.append(otp)

    # ★ 자동 귀속 조건:
    #   남은 선수 = 1명, 아직 이 티어가 없는 팀장 = 1명
    if remaining_in_tier == 1 and len(free_managers_otp) == 1:
        manager_otp = free_managers_otp[0]

        # 현재 player_index 위치의 선수가
        # "이 티어에서 마지막으로 남은 선수"인 상황
        player_info = AUCTION_STATE['player_list'][AUCTION_STATE['player_index']]
        manager = MANAGERS[manager_otp]

        # 팀에 선수 추가
        manager['team'][player_info['name']] = player_info

        # 알림 메시지 전송
        socketio.emit(
            'chat_message',
            {
                'name': '시스템',
                'message': f"[자동 귀속] [{manager['name']}] 팀에 {player_info['name']} ({tier} 티어) 선수가 강제 낙찰되었습니다!"
            }
        )

        # 다음 선수로 넘어가도록 인덱스 +1
        AUCTION_STATE['player_index'] += 1

        print(f"--- [자동 귀속] 티어 {tier}, 선수 {player_info['name']} → 팀장 {manager['name']} ---")
        return True

    # 조건에 안 맞으면 아무것도 안 함
    return False


# --- 3. 경매 진행 함수 ---

def reset_auction_for_next_player():
    """현재 경매 종료 후 다음 선수 경매를 준비합니다."""
    
    # 1. 이전 경매 선수에 대한 인덱스 증가 (낙찰/유찰 후)
    # 자동 귀속 시에는 이미 AUCTION_STATE['player_index']가 증가되어 있으므로, 
    # 일반 낙찰/유찰의 경우에만 증가시킵니다.
    # 단, end_bid에서 이 함수를 호출하기 전에 인덱스 증가를 하지 않으므로 여기서 +1이 필요합니다.
    # end_bid에서 호출 시 player_index는 현재 낙찰된 선수의 인덱스입니다.
    # 만약 자동 귀속이 발생했다면, AUCTION_STATE['player_index']는 자동 귀속된 선수 수만큼 이미 증가되어 있습니다.
    
    # 2. 다음 경매 대상이 있다면 자동 귀속 체크
    if AUCTION_STATE['player_index'] < len(AUCTION_STATE['player_list']):
        current_tier = AUCTION_STATE['player_list'][AUCTION_STATE['player_index']]['tier']
        # 자동 귀속이 발생할 경우, AUCTION_STATE['player_index']가 이 함수 내에서 추가로 증가합니다.
        check_and_apply_autoclaim(current_tier)
    
    # 3. 최종 상태 설정
    if AUCTION_STATE['player_index'] >= len(AUCTION_STATE['player_list']):
        # 모든 경매 종료
        AUCTION_STATE['status'] = 'ENDED'
        AUCTION_STATE['current_player'] = '경매 종료'
        socketio.emit('chat_message', {'name': '시스템', 'message': "모든 경매가 종료되었습니다!"})
        print("--- 모든 경매가 종료되었습니다 ---")
    else:
        # 다음 선수 경매 준비 (PAUSED 상태로 진입)
        next_player = AUCTION_STATE['player_list'][AUCTION_STATE['player_index']]
        AUCTION_STATE['current_player'] = next_player['name']
        AUCTION_STATE['current_tier'] = next_player['tier']
        AUCTION_STATE['status'] = 'PAUSED' # <-- 딜레이 상태
        AUCTION_STATE['current_price'] = 0
        AUCTION_STATE['leading_manager_id'] = None
        AUCTION_STATE['timer_end'] = time.time() + 5 # 5초 딜레이
        
        socketio.emit('chat_message', {'name': '시스템', 'message': f"잠시 후 다음 선수: {next_player['name']} ({next_player['tier']} 티어) 경매를 시작합니다."})

    emit_auction_state()
    emit_manager_data()

def get_auction_data():
    """클라이언트에 전송할 경매 상태 데이터 취합"""
    data = {
        'state': AUCTION_STATE['status'],
        'player_name': AUCTION_STATE['current_player'],
        'player_tier': AUCTION_STATE['current_tier'],
        'player_index': AUCTION_STATE['player_index'], 
        'current_price': AUCTION_STATE['current_price'],
        'leading_manager_id': AUCTION_STATE['leading_manager_id'],
        'timer_remaining': max(0, int(AUCTION_STATE['timer_end'] - time.time())), # 남은 시간 초 단위로 전송
        'managers': {otp: {'id': m['id'], 'name': m['name'], 'coin': m['coin'], 'team': m['team'], 'is_online': m['is_online']} for otp, m in MANAGERS.items()},
        'player_list': AUCTION_STATE['player_list']
    }
    return data

def emit_auction_state():
    """모든 클라이언트에게 경매 상태를 브로드캐스트"""
    data = get_auction_data()
    socketio.emit('auction_update', data)
    
def emit_manager_data():
    """모든 클라이언트에게 매니저 데이터 브로드캐스트"""
    data = {'managers': {otp: {'id': m['id'], 'name': m['name'], 'coin': m['coin'], 'team': m['team'], 'is_online': m['is_online']} for otp, m in MANAGERS.items()}}
    socketio.emit('manager_data_update', data)

# --- 4. Flask 라우트 ---

@app.route('/')
def index():
    """기본 페이지: templates/index.html을 렌더링"""
    return render_template('index.html') 

@app.route('/auth', methods=['POST'])
def authenticate():
    """OTP 인증 처리"""
    otp = request.form.get('otp')
    if otp in MANAGERS:
        session_data = {'type': 'manager', 'otp': otp, 'id': MANAGERS[otp]['id'], 'name': MANAGERS[otp]['name']}
        return jsonify({"success": True, "access_type": "manager", "session": session_data})
    elif otp == ADMIN_OTP:
        session_data = {'type': 'admin', 'otp': otp, 'name': '관리자'}
        return jsonify({"success": True, "access_type": "admin", "session": session_data})
    else:
        session_data = {'type': 'viewer', 'otp': None, 'name': '참관인'}
        return jsonify({"success": True, "access_type": "viewer", "session": session_data})

# --- 5. SocketIO 이벤트 핸들러 ---

@socketio.on('connect')
def handle_connect():
    print(f"클라이언트 연결됨: {request.sid}")
    emit_auction_state()

@socketio.on('authenticate')
def handle_authentication(data):
    otp = data.get('otp')
    if otp in MANAGERS:
        manager = MANAGERS[otp]
        manager['is_online'] = True
        join_room(manager['id']) 
        join_room('managers')
        print(f"팀장 접속: {manager['name']}")
        emit_manager_data()
    elif otp == ADMIN_OTP:
        join_room('admin')
        print("관리자 접속")
    
    join_room('public') 

@socketio.on('disconnect')
def handle_disconnect():
    for otp, manager in MANAGERS.items():
        if manager['is_online'] and request.sid in socketio.server.rooms(request.sid):
            manager['is_online'] = False
            print(f"팀장 연결 해제: {manager['name']}")
            emit_manager_data()

@socketio.on('handle_bid')
def handle_bid(data):
    otp = data.get('otp')
    bid = int(data.get('bid', 0))

    if otp not in MANAGERS:
        return

    manager = MANAGERS[otp]

    # ① 코인 부족 체크
    if manager['coin'] < bid:
        emit('chat_message', {
            'name': '시스템',
            'message': '보유 코인보다 많이 입찰할 수 없습니다.'
        }, room=otp)
        return

    # ② ★ 티어 중복 입찰 방지 (추가된 코드) ★
    current_tier = AUCTION_STATE.get('player_tier')
    if current_tier:
        # 이미 해당 티어 선수를 소유한 경우
        if any(info['tier'] == current_tier for info in manager['team'].values()):
            emit('chat_message', {
                'name': '시스템',
                'message': f'이미 {current_tier} 티어 선수를 보유하고 있어서 입찰할 수 없습니다.'
            }, room=otp)
            return
    # ② 여기까지

    # ③ 최고 입찰 갱신
    if bid > AUCTION_STATE['current_bid']:
        AUCTION_STATE['current_bid'] = bid
        AUCTION_STATE['current_bidder'] = otp
        socketio.emit('auction_state', get_auction_data())


    # 입찰 성공
    AUCTION_STATE['current_price'] = new_price
    AUCTION_STATE['leading_manager_id'] = manager['id']
    AUCTION_STATE['timer_end'] = time.time() + 10 # 입찰 시 타이머 갱신 (10초)
    
    socketio.emit('chat_message', {'name': manager['name'], 'message': f"입찰: {new_price} 코인!"})

    emit_auction_state()

@socketio.on('chat_message')
def handle_chat_message(data):
    if 'name' in data and 'message' in data:
        socketio.emit('chat_message', {'name': data['name'], 'message': data['message']})


# --- 6. 관리자 기능 (Admin Only) ---

@socketio.on('admin_start_auction')
def start_auction(data=None):
    """관리자가 경매를 시작하거나 재개할 때"""
    if AUCTION_STATE['status'] == 'READY' or AUCTION_STATE['status'] == 'PAUSED' or AUCTION_STATE['status'] == 'ENDED':
        
        if not AUCTION_STATE['is_started'] or AUCTION_STATE['status'] == 'ENDED':
            # 처음 시작 또는 종료 후 재시작 (여기서 플레이어 목록을 다시 무작위로 섞음)
            initialize_players() 
            AUCTION_STATE['is_started'] = True
            # 다음 선수 인덱스를 0부터 시작하기 위해 -1에서 시작하여 reset_auction_for_next_player에서 +1 되도록 조정
            AUCTION_STATE['player_index'] = -1 
            
            # 다음 선수 경매 준비 (index가 0으로 증가하고 PAUSED 상태 진입)
            AUCTION_STATE['player_index'] += 1 
            reset_auction_for_next_player()
            return

        # PAUSED 상태에서 강제 재개 시 바로 BIDDING 상태로 전환
        if AUCTION_STATE['status'] == 'PAUSED':
            AUCTION_STATE['status'] = 'BIDDING'
            AUCTION_STATE['timer_end'] = time.time() + 10
            socketio.emit('chat_message', {'name': '시스템', 'message': f"관리자가 [{AUCTION_STATE['current_player']}] 선수 경매를 강제 재개했습니다!"})
            emit_auction_state()

@socketio.on('admin_end_bid')
def end_bid(data=None):
    """관리자가 현재 입찰을 강제 종료하고 낙찰 처리"""
    if AUCTION_STATE['status'] != 'BIDDING':
        return

    leading_id = AUCTION_STATE['leading_manager_id']
    final_price = AUCTION_STATE['current_price']
    
    if AUCTION_STATE['player_index'] >= len(AUCTION_STATE['player_list']):
        return

    current_player_info = AUCTION_STATE['player_list'][AUCTION_STATE['player_index']]

    if leading_id:
        # 낙찰 처리
        winning_manager_otp = next(otp for otp, m in MANAGERS.items() if m['id'] == leading_id)
        winning_manager = MANAGERS[winning_manager_otp]
        
        winning_manager['coin'] -= final_price
        winning_manager['team'][current_player_info['name']] = current_player_info
        
        socketio.emit('chat_message', {'name': '시스템', 'message': f"🎉 {winning_manager['name']} 팀이 {current_player_info['name']} 선수를 {final_price} 코인에 낙찰했습니다!"})
        
        # 낙찰 후 다음 선수로 인덱스 이동
        AUCTION_STATE['player_index'] += 1
        reset_auction_for_next_player()
        
    else:
        # 유찰 처리
        socketio.emit('chat_message', {'name': '시스템', 'message': f"❌ {current_player_info['name']} 선수가 유찰되었습니다."})
        
        # 유찰 후 다음 선수로 인덱스 이동
        AUCTION_STATE['player_index'] += 1
        reset_auction_for_next_player()

@socketio.on('admin_update_manager')
def admin_update_manager(data):
    """관리자가 팀장의 코인, 이름 등을 수정"""
    target_otp = data.get('otp')
    if target_otp in MANAGERS:
        if 'coin' in data:
            MANAGERS[target_otp]['coin'] = int(data.get('coin'))
        if 'name' in data:
            MANAGERS[target_otp]['name'] = data.get('name')
        
        emit_manager_data()
        emit_auction_state()
        socketio.emit('chat_message', {'name': '시스템', 'message': f"관리자가 [{MANAGERS[target_otp]['name']}] 팀장의 정보를 수정했습니다."})


# --- 7. 타이머 및 메인 루프 (딜레이 로직) ---

def timer_thread():
    """백그라운드에서 타이머 및 경매 상태 관리"""
    while True:
        socketio.sleep(1)
        
        current_time = time.time()
        
        if AUCTION_STATE['status'] == 'BIDDING':
            if current_time >= AUCTION_STATE['timer_end']:
                # BIDDING 타이머 종료 -> 자동 낙찰 처리
                with app.app_context():
                    end_bid()
            emit_auction_state()
            
        elif AUCTION_STATE['status'] == 'PAUSED':
            if current_time >= AUCTION_STATE['timer_end']:
                # PAUSED 타이머 종료 -> BIDDING 상태로 전환
                with app.app_context():
                    AUCTION_STATE['status'] = 'BIDDING'
                    AUCTION_STATE['timer_end'] = time.time() + 10 # 10초 입찰 타이머 시작
                    socketio.emit('chat_message', {'name': '시스템', 'message': f"[{AUCTION_STATE['current_player']}] 선수 경매가 시작되었습니다! 입찰해 주세요."})
                    emit_auction_state()
            else:
                # PAUSED 상태에서도 타이머를 보여주기 위해 업데이트
                emit_auction_state()


# 서버 시작 시 백그라운드 스레드 시작
socketio.start_background_task(timer_thread)

# --- 8. 실행 ---

if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 5000))

    print("경매 서버 시작 중…")

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True  # ← 이거 추가
    )

