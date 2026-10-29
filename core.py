"""Cooldown-aware claim engine. Never fabricates clicks or changes system time."""
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import threading
import time
import uuid
import requests

API_HOST = 'https://api-wutheringwaves-wuwatappo.kurogames-global.com/'
AUTH_CODES = {10002, 10003, 90002}
SAFE_COUNT = 9007199254740991

class ApiError(Exception):
    def __init__(self, code, message=''):
        self.code = int(code)
        super().__init__(message or f'API {code}')

class TransportError(Exception):
    pass

class ServerClock:
    def __init__(self, monotonic=time.monotonic):
        self.monotonic = monotonic
        self.base = None
        self.anchor = None

    def update(self, header):
        if not header:
            raise TransportError('Server did not provide time header; claim paused.')
        try:
            stamp = parsedate_to_datetime(header).timestamp()
        except (TypeError, ValueError, OverflowError):
            raise TransportError('Invalid server time.') from None
        self.base, self.anchor = stamp, self.monotonic()

    def now(self):
        if self.base is None:
            raise TransportError('Server clock not synchronized.')
        return self.base + self.monotonic() - self.anchor

class ApiClient:
    def __init__(self, token, clock=None):
        self.clock = clock or ServerClock()
        self.session = requests.Session()
        # Avoid inheriting arbitrary proxy configuration for the app session.
        self.session.trust_env = False
        self.session.headers.update({
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 WuwaTappoAutoClaim/1.0',
            'Origin': 'http://tauri.localhost'
        })

    def request(self, method, path, payload=None):
        if path not in ('api/frontend/user/me', 'api/frontend/chests/open'):
            raise ValueError('Endpoint not allowed')
        try:
            response = self.session.request(
                method, API_HOST + path, json=payload,
                headers={'X-BLM-FRID': uuid.uuid4().hex}, timeout=(5, 12), allow_redirects=False
            )
            if response.status_code in (401, 403):
                raise ApiError(10002, 'Session expired or invalid. Please re-pair.')
            if response.status_code != 200:
                raise TransportError(f'HTTP {response.status_code}; will retry later.')
            self.clock.update(response.headers.get('Date'))
            obj = response.json()
            if not isinstance(obj, dict) or 'code' not in obj:
                raise TransportError('API response in invalid format.')
            code = int(obj['code'])
            if code not in (0, 200):
                raise ApiError(code)
            if not isinstance(obj.get('data'), dict):
                raise TransportError('API response missing chest data.')
            return obj['data']
        except requests.RequestException:
            raise TransportError('Connection lost or timeout; will check status again.') from None
        except (ValueError, TypeError):
            raise TransportError('Invalid API response.') from None

    def me(self):
        return self.request('GET', 'api/frontend/user/me')

    def open(self, slot, gross):
        if slot not in ('left', 'right') or type(gross) is not int or not 0 <= gross <= SAFE_COUNT:
            raise ValueError('Invalid claim')
        return self.request('POST', 'api/frontend/chests/open', {'place': slot, 'click_num': gross})

    def close(self):
        self.session.close()

@dataclass
class AccountState:
    gross: int
    consumed: int
    deadlines: dict
    cuid: str

    @property
    def balance(self):
        return max(0, self.gross - self.consumed)

def parse_state(info):
    def count(key):
        value = info.get(key)
        if isinstance(value, bool) or value is None:
            raise TransportError('Missing click count from server.')
        try:
            number = float(value)
        except (ValueError, TypeError):
            raise TransportError('Invalid click count.') from None
        if not math.isfinite(number) or number < 0 or number > SAFE_COUNT or number != int(number):
            raise TransportError('Click count out of supported range.')
        return int(number)

    deadlines = {}
    for slot in ('left', 'right'):
        key = slot + '_treasure_chest'
        if key not in info or info[key] is None or isinstance(info[key], bool):
            raise TransportError('Missing chest deadline; auto-claim paused.')
        try:
            value = float(info[key])
        except (ValueError, TypeError):
            raise TransportError('Invalid chest deadline.') from None
        if not math.isfinite(value) or value < 0:
            raise TransportError('Invalid chest deadline.')
        # Installed 1.0.1 client uses epoch milliseconds, not seconds.
        if 0 < value < 100_000_000_000:
            raise TransportError('Chest timestamp format changed; tool update required.')
        deadlines[slot] = value / 1000

    return AccountState(
        count('total_click_count'),
        count('total_click_consume'),
        deadlines,
        str(info.get('cuid', '')).strip()
    )

class Engine:
    def __init__(self, api, journal_path, emit=lambda event, **kw: None, expected_cuid=lambda: None, should_claim=lambda: True):
        self.api = api
        self.path = Path(journal_path)
        self.emit = emit
        self.expected_cuid = expected_cuid
        self.should_claim = should_claim
        self.attempts = {}
        self.last_state = None
        self.account = ''
        try:
            obj = json.loads(self.path.read_text('utf8'))
            self.attempts = obj.get('attempts', {})
            self.account = obj.get('account', '')
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'attempts': self.attempts, 'account': self.account}), encoding='utf8')
        tmp.replace(self.path)

    def inspect(self):
        state = parse_state(self.api.me())
        local_cuid = self.expected_cuid()
        if local_cuid and state.cuid != local_cuid:
            raise ApiError(10002, 'Account in app has changed. Please re-pair.')
        if self.account != state.cuid:
            self.attempts = {}
            self.account = state.cuid
        self.last_state = state
        return state

    def step(self, allow_claim=True):
        state = self.inspect()
        now = self.api.clock.now()
        self.emit('status', balance=state.balance, gross=state.gross, consumed=state.consumed,
                  left=max(0, state.deadlines['left'] - now),
                  right=max(0, state.deadlines['right'] - now))
        if state.balance < 1000:
            self.emit('need_clicks', balance=state.balance)
            return 30
        due = sorted(state.deadlines, key=state.deadlines.get)
        for slot in due:
            deadline = state.deadlines[slot]
            # One second beyond the server timestamp avoids second-resolution Date rounding.
            if now < deadline + 1:
                continue
            last = self.attempts.get(slot, {})
            if last.get('cycle') == deadline:
                if last.get('outcome') == 'success' or now < last.get('retry_after', 0):
                    continue
            if not allow_claim or not self.should_claim():
                self.emit('ready', slot=slot)
                continue
            self.attempts[slot] = {'cycle': deadline, 'outcome': 'pending', 'retry_after': now + 30}
            self.save()
            try:
                reward = self.api.open(slot, state.gross)
            except ApiError as exc:
                self.attempts[slot]['outcome'] = 'rejected'
                self.save()
                self.emit('claim_rejected', slot=slot, code=exc.code)
                if exc.code in AUTH_CODES:
                    raise
                return 30 if exc.code in (70003, 70004, 70005, 70006) else 120
            except TransportError:
                self.emit('claim_uncertain', slot=slot)
                # Keep pending on disk. Next attempt must re-read account state.
                raise
            self.attempts[slot]['outcome'] = 'success'
            self.save()
            self.emit('claimed', slot=slot, asset_id=reward.get('asset_id'),
                      asset_name=str(reward.get('asset_name', ''))[:150],
                      asset_type=str(reward.get('asset_type', ''))[:50],
                      rarity=str(reward.get('rarity_type', ''))[:50])
            return 2
        future = [stamp + 1 - now for stamp in state.deadlines.values() if stamp + 1 > now]
        return max(1, min(30, min(future))) if future else 30
