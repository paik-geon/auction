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
    'is_started': False,
    'round': 1,             # 1차 / 2차
}

def initialize_players():
    """
    티어 구분 없이 모든 선수를 가져와 완전히 무작위로 섞어 경매 순서를 설정.
    각 선수는 상태/status 를 포함한다.
    status: pending / sold / unsold / forced / unsold_final
    """
    all_players = []
    for tier, names in PLAYERS_DATA.items():
        for name in names:
            all_players.append({
                'tier': tier,
                'name': name,
                'status': 'pending',
                'price': 0,
                'owner_id': None,
            })

    random.shuffle(all_players)

    AUCTION_STATE['player_list'] = all_players
    AUCTION_STATE['round'] = 1

    if all_players:
        AUCTION_STATE['player_index'] = 0
        AUCTION_STATE['current_player'] = all_players[0]['name']
        AUCTION_STATE['current_tier'] = all_players[0]['tier']

# 서버 시작 시 1차 플레이어 리스트 준비
initialize_players()


# --- 2. 자동 귀속 (티어 1명 vs 팀장 1명) ---

def check_and_apply_autoclaim(tier: str) -> bool:
    """
    [자동 귀속 규칙]
    - 해당 티어의 선수가 '1명만' 남았고
    - 아직 그 티어 선수를 가져가지 못한 팀장도 '1명만' 남았을 때
      → 그 팀장에게 남은 1명을 자동 낙찰.
    """
    if AUCTION_STATE['player_index'] >= len(AUCTION_STATE['player_list']):
        return False

    # 현재 인덱스부터 끝까지, 이 티어에 남은 선수 수
    remaining_in_tier = sum(
        1
        for p in AUCTION_STATE['player_list'][AUCTION_STATE['player_index']:]
        if p['tier'] == tier
    )

    # 이 티어 선수를 아직 한 명도 못 가진 팀장 목록
    free_managers_otp = []
    for otp, manager in MANAGERS.items():
        if not any(p['tier'] == tier for p in manager['team'].values()):
            free_managers_otp.append(otp)

    if remaining_in_tier == 1 and len(free_managers_otp) == 1:
        manager_otp = free_managers_otp[0]
        manager = MANAGERS[manager_otp]

        player_info = AUCTION_STATE['player_list'][AUCTION_STATE['player_index']]

        # 팀에 선수 추가 (무료 강제 배정)
        manager['team'][player_info['name']] = {
            'tier': player_info['tier'],
            'name': player_info['name'],
            'price': 0,
            'round': AUCTION_STATE['round'],
            'forced': True,
        }

        player_info['status'] = 'forced'
        player_info['price'] = 0
        player_info['owner_id'] = manager['id']

        socketio.emit(
            'chat_message',
            {
                'name': '시스템',
                'message': f"[자동 귀속] [{manager['name']}] 팀에 {player_info['name']} ({tier} 티어) 선수가 강제 낙찰되었습니다!"
            }
        )

        # 다음 선수로 이동
        AUCTION_STATE['player_index'] += 1

        print(f"--- [자동 귀속] 티어 {tier}, 선수 {player_info['name']} → 팀장 {manager['name']} ---")
        return True

    return False


# --- 3. 2차 경매 & 최종 자동 배정 로직 ---

def team_has_tier(manager, tier: str) -> bool:
    return any(p['tier'] == tier for p in manager['team'].values())


def start_second_round():
    """1차 경매가 끝났을 때, 유찰된 선수만 모아 2차 경매 시작."""
    unsold = [p for p in AUCTION_STATE['player_list'] if p.get('status') == 'unsold']

    if not unsold:
        # 유찰 선수 없다면 바로 최종 처리
        finalize_unsold_players()
        return

    AUCTION_STATE['round'] = 2
    AUCTION_STATE['player_list'] = unsold
    AUCTION_STATE['player_index'] = 0

    first = unsold[0]
    AUCTION_STATE['current_player'] = first['name']
    AUCTION_STATE['current_tier'] = first['tier']
    AUCTION_STATE['current_price'] = 0
    AUCTION_STATE['leading_manager_id'] = None
    AUCTION_STATE['status'] = 'PAUSED'
    AUCTION_STATE['timer_end'] = time.time() + 5

    socketio.emit('chat_message', {
        'name': '시스템',
        'message': '[2차 경매] 1차에서 유찰된 선수들만 남은 코인으로 다시 경매합니다.'
    })
    emit_auction_state()
    emit_manager_data()


def finalize_unsold_players():
    """
    2차 경매 후에도 남은 선수들을
      - 해당 티어가 없는 팀 중 코인이 가장 많이 남은 팀에 자동 귀속
      - 그래도 갈 곳 없으면 최종 유찰로 확정
    """
    remaining = [p for p in AUCTION_STATE['player_list'] if p.get('status') not in ('sold', 'forced')]

    for player in remaining:
        tier = player['tier']
        name = player['name']

        # 이 티어가 없는 팀들만 후보
        candidates = [
            (otp, m) for otp, m in MANAGERS.items()
            if not team_has_tier(m, tier)
        ]

        if candidates:
            otp, manager = max(candidates, key=lambda kv: kv[1]['coin'])

            manager['team'][name] = {
                'tier': tier,
                'name': name,
                'price': 0,
                'round': AUCTION_STATE['round'],
                'forced': True,
            }
            player['status'] = 'forced'
            player['price'] = 0
            player['owner_id'] = manager['id']

            socketio.emit('chat_message', {
                'name': '시스템',
                'message': f"[자동 귀속] {manager['name']} 팀이 {name} 선수({tier} 티어)를 배정받았습니다."
            })
        else:
            # 진짜 아무 팀도 받을 데 없으면 최종 유찰
            player['status'] = 'unsold_final'
            player['price'] = 0
            player['owner_id'] = None

            socketio.emit('chat_message', {
                'name': '시스템',
                'message': f"유찰 : {name} 선수({tier} 티어)"
            })

    AUCTION_STATE['status'] = 'ENDED'
    AUCTION_STATE['current_player'] = '경매 종료'
    AUCTION_STATE['current_tier'] = ''
    socketio.emit('chat_message', {
        'name': '시스템',
        'message': '모든 1·2차 경매와 자동 귀속 처리가 종료되었습니다.'
    })
    emit_auction_state()
    emit_manager_data()


# --- 4. 경매 진행 함수 ---

def reset_auction_for_next_player():
    """
    현재 경매 종료 후 다음 선수 경매 준비.
    player_index 는 이미 end_bid / 자동귀속에서 증가된 상태라고 가정.
    """
    # 아직 남은 선수가 있다면, 자동귀속 먼저 체크
    if AUCTION_STATE['player_index'] < len(AUCTION_STATE['player_list']):
        current_tier = AUCTION_STATE['player_list'][AUCTION_STATE['player_index']]['tier']
        check_and_apply_autoclaim(current_tier)

    # 자동귀속 후 더 이상 남은 선수가 없는 경우
    if AUCTION_STATE['player_index'] >= len(AUCTION_STATE['player_list']):
        if AUCTION_STATE['round'] == 1:
            # 1차 종료 → 2차 시작 (유찰 선수만)
            start_second_round()
        else:
            # 2차까지 종료 → 최종 자동 배정
            finalize_unsold_players()
        return

    # 다음 선수 경매 준비
    next_player = AUCTION_STATE['player_list'][AUCTION_STATE['player_index']]
    AUCTION_STATE['current_player'] = next_player['name']
    AUCTION_STATE['current_tier'] = next_player['tier']
    AUCTION_STATE['status'] = 'PAUSED'
    AUCTION_STATE['current_price'] = 0
    AUCTION_STATE['leading_manager_id'] = None
    AUCTION_STATE['timer_end'] = time.time() + 5  # 5초 준비 시간

    round_text = '1차' if AUCTION_STATE['round'] == 1 else '2차'
    socketio.emit('chat_message', {
        'name': '시스템',
        'message': f"[{round_text}] 잠시 후 다음 선수: {next_player['name']} ({next_player['tier']} 티어) 경매를 시작합니다."
    })

    emit_auction_state()
    emit_manager_data()


def get_auction_data():
    """클라이언트에 전송할 경매 상태 데이터 취합"""
    timer_end = AUCTION_STATE.get('timer_end')
    if timer_end:
        timer_remaining = max(0, int(timer_end - time.time()))
    else:
        timer_remaining = 0

    data = {
        'state': AUCTION_STATE.get('status', 'INIT'),
        'current_player': AUCTION_STATE.get('current_player', ''),
        'player_tier': AUCTION_STATE.get('current_tier', ''),
        'player_index': AUCTION_STATE.get('player_index', -1),
        'current_price': AUCTION_STATE.get('current_price', 0),
        'leading_manager_id': AUCTION_STATE.get('leading_manager_id', None),
        'timer_remaining': timer_remaining,
        'round': AUCTION_STATE.get('round', 1),

        'managers': {
            otp: {
                'id': m['id'],
                'name': m['name'],
                'coin': m['coin'],
                'team': m['team'],
                'is_online': m['is_online'],
            }
            for otp, m in MANAGERS.items()
        },

        'player_list': AUCTION_STATE.get('player_list', []),
    }

    return data


def emit_auction_state():
    socketio.emit('auction_update', get_auction_data())


def emit_manager_data():
    data = {
        'managers': {
            otp: {
                'id': m['id'],
                'name': m['name'],
                'coin': m['coin'],
                'team': m['team'],
                'is_online': m['is_online'],
            }
            for otp, m in MANAGERS.items()
        }
    }
    socketio.emit('manager_data_update', data)


# --- 5. Flask 라우트 ---

@app.route('/')
def index():
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


# --- 6. Socket.IO 이벤트 ---

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
    # 간단 버전: 연결 끊길 때는 모두 오프라인으로 갱신 (세션 매핑이 없어서 완벽하진 않지만 현재 구조에서는 충분)
    for otp, manager in MANAGERS.items():
        if manager['is_online']:
            manager['is_online'] = False
    emit_manager_data()
    print("클라이언트 연결 해제")


@socketio.on('place_bid')
def handle_bid(data):
    """팀장이 입찰을 시도할 때 호출"""

    manager_otp = data.get('otp')
    bid_increment = int(data.get('amount', 0))

    if AUCTION_STATE['status'] != 'BIDDING':
        emit('bid_error', {'message': '현재 입찰 시간이 아닙니다.'})
        return

    manager = MANAGERS.get(manager_otp)
    if manager is None:
        emit('bid_error', {'message': '유효하지 않은 팀장입니다.'})
        return

    current_tier = AUCTION_STATE.get('current_tier')
    if current_tier:
        for _, player in manager['team'].items():
            if player['tier'] == current_tier:
                emit('bid_error', {
                    'message': f'이미 {current_tier} 티어 선수를 보유하고 있어 입찰할 수 없습니다.'
                }, room=manager_otp)
                return

    new_price = AUCTION_STATE['current_price'] + bid_increment

    if manager['coin'] < new_price:
        emit('bid_error', {
            'message': f'보유 코인({manager["coin"]})보다 큰 금액으로 입찰할 수 없습니다.'
        }, room=manager_otp)
        return

    # 최고 입찰 정보 갱신
    AUCTION_STATE['current_price'] = new_price
    AUCTION_STATE['leading_manager_id'] = manager['id']

    # 누가 입찰하면 항상 15초로 연장
    AUCTION_STATE['timer_end'] = time.time() + 15

    socketio.emit('chat_message', {
        'name': manager['name'],
        'message': f"{new_price} 코인!"
    })

    emit_auction_state()


@socketio.on('chat_message')
def handle_chat_message(data):
    if 'name' in data and 'message' in data:
        socketio.emit('chat_message', {'name': data['name'], 'message': data['message']})


# --- 7. 관리자 액션 ---

@socketio.on('admin_start_auction')
def start_auction(data=None):
    """
    READY / ENDED 상태에서 전체 리셋,
    또는 PAUSED 상태에서 강제 BIDDING 전환
    """
    if AUCTION_STATE['status'] in ('READY', 'PAUSED', 'ENDED'):

        if not AUCTION_STATE['is_started'] or AUCTION_STATE['status'] == 'ENDED':
            # 완전 새로 시작
            initialize_players()
            AUCTION_STATE['is_started'] = True
            AUCTION_STATE['status'] = 'PAUSED'
            AUCTION_STATE['current_price'] = 0
            AUCTION_STATE['leading_manager_id'] = None
            AUCTION_STATE['timer_end'] = time.time() + 5

            first = AUCTION_STATE['player_list'][0]
            socketio.emit('chat_message', {
                'name': '시스템',
                'message': f"[1차 경매] 잠시 후 첫 선수: {first['name']} ({first['tier']} 티어) 경매를 시작합니다."
            })
            emit_auction_state()
            return

        if AUCTION_STATE['status'] == 'PAUSED':
            AUCTION_STATE['status'] = 'BIDDING'
            AUCTION_STATE['timer_end'] = time.time() + 15
            socketio.emit('chat_message', {
                'name': '시스템',
                'message': f"관리자가 [{AUCTION_STATE['current_player']}] 선수 경매를 강제 재개했습니다!"
            })
            emit_auction_state()


@socketio.on('admin_end_bid')
def end_bid(data=None):
    """관리자가 현재 입찰을 강제 종료하거나, 타이머가 0이 되었을 때 호출"""
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
        winning_manager['team'][current_player_info['name']] = {
            'tier': current_player_info['tier'],
            'name': current_player_info['name'],
            'price': final_price,
            'round': AUCTION_STATE['round'],
        }

        current_player_info['status'] = 'sold'
        current_player_info['price'] = final_price
        current_player_info['owner_id'] = winning_manager['id']

        socketio.emit('chat_message', {
            'name': '시스템',
            'message': f"🎉 {winning_manager['name']} 팀이 {current_player_info['name']} 선수를 {final_price} 코인에 낙찰했습니다!"
        })

    else:
        # 유찰 처리
        current_player_info['status'] = 'unsold'
        current_player_info['price'] = 0
        current_player_info['owner_id'] = None

        socketio.emit('chat_message', {
            'name': '시스템',
            'message': f"❌ {current_player_info['name']} 선수가 유찰되었습니다."
        })

    # 다음 선수로 이동 후 준비
    AUCTION_STATE['player_index'] += 1
    AUCTION_STATE['current_price'] = 0
    AUCTION_STATE['leading_manager_id'] = None
    AUCTION_STATE['status'] = 'PAUSED'
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
        socketio.emit('chat_message', {
            'name': '시스템',
            'message': f"관리자가 [{MANAGERS[target_otp]['name']}] 팀장의 정보를 수정했습니다."
        })


# --- 8. 타이머 스레드 ---

def timer_thread():
    """백그라운드에서 타이머 및 경매 상태 관리"""
    while True:
        socketio.sleep(1)
        current_time = time.time()

        if AUCTION_STATE['status'] == 'BIDDING':
            if current_time >= AUCTION_STATE['timer_end']:
                with app.app_context():
                    end_bid()
            else:
                emit_auction_state()

        elif AUCTION_STATE['status'] == 'PAUSED':
            if current_time >= AUCTION_STATE['timer_end']:
                with app.app_context():
                    AUCTION_STATE['status'] = 'BIDDING'
                    AUCTION_STATE['timer_end'] = time.time() + 15
                    socketio.emit('chat_message', {
                        'name': '시스템',
                        'message': f"[{AUCTION_STATE['current_player']}] 선수 경매가 시작되었습니다! 입찰해 주세요."
                    })
                    emit_auction_state()
            else:
                emit_auction_state()


socketio.start_background_task(timer_thread)


# --- 9. 실행 ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("경매 서버 시작 중…")
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True,
    )
