import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import Engine, ApiError, TransportError, ServerClock, parse_state

NOW = 1793100000

class Clock:
    def __init__(self):
        self.value = NOW

    def now(self):
        return self.value

class FakeApi:
    def __init__(self, left=10, right=20, balance=3000):
        self.clock = Clock()
        self.calls = []
        self.fail = None
        self.info = {
            'cuid': 'a',
            'total_click_count': 5000,
            'total_click_consume': 5000 - balance,
            'left_treasure_chest': (NOW + left) * 1000,
            'right_treasure_chest': (NOW + right) * 1000
        }

    def me(self):
        return dict(self.info)

    def open(self, slot, gross):
        self.calls.append((slot, gross))
        if self.fail:
            raise self.fail
        self.info['total_click_consume'] += 1000
        self.info[slot + '_treasure_chest'] = (self.clock.now() + 1800) * 1000
        return {
            'asset_id': 123,
            'asset_name': 'test_asset',
            'asset_type': 'character_skin',
            'rarity_type': 'common'
        }

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'journal.json'

    def tearDown(self):
        self.tmp.cleanup()

    def test_waits_until_after_server_deadline(self):
        api = FakeApi()
        engine = Engine(api, self.path)
        self.assertEqual(engine.step(), 11)
        self.assertEqual(api.calls, [])
        api.clock.value = NOW + 10
        engine.step()
        self.assertEqual(api.calls, [])
        api.clock.value = NOW + 11
        engine.step()
        self.assertEqual(api.calls, [('left', 5000)])

    def test_two_ready_slots_only_one_per_step(self):
        api = FakeApi(-1, -1)
        engine = Engine(api, self.path)
        engine.step()
        self.assertEqual(len(api.calls), 1)
        engine.step()
        self.assertEqual([x[0] for x in api.calls], ['left', 'right'])
        engine.step()
        self.assertEqual(len(api.calls), 2)

    def test_not_enough_clicks(self):
        api = FakeApi(-1, -1, 999)
        Engine(api, self.path).step()
        self.assertEqual(api.calls, [])

    def test_dry_run_and_pause_are_read_only(self):
        api = FakeApi(-1, -1)
        Engine(api, self.path).step(False)
        Engine(api, self.path, should_claim=lambda: False).step()
        self.assertEqual(api.calls, [])
        self.assertFalse(self.path.exists())

    def test_uncertain_request_is_not_immediately_retried_after_restart(self):
        api = FakeApi(-1, 100)
        api.fail = TransportError('timeout')
        engine = Engine(api, self.path)
        with self.assertRaises(TransportError):
            engine.step()
        api.clock.value += 10
        Engine(api, self.path).step()
        self.assertEqual(len(api.calls), 1)
        api.clock.value += 21
        api.fail = None
        Engine(api, self.path).step()
        self.assertEqual(len(api.calls), 2)

    def test_success_not_repeated_on_stale_snapshot(self):
        api = FakeApi(-1, 100)
        before = dict(api.info)
        engine = Engine(api, self.path)
        engine.step()
        api.info = before
        api.clock.value += 60
        Engine(api, self.path).step()
        self.assertEqual(len(api.calls), 1)

    def test_cooldown_rejection_backoff(self):
        api = FakeApi(-1, 100)
        api.fail = ApiError(70006)
        engine = Engine(api, self.path)
        self.assertEqual(engine.step(), 30)
        engine.step()
        self.assertEqual(len(api.calls), 1)

    def test_account_change_stops_claim(self):
        api = FakeApi(-1, -1)
        with self.assertRaises(ApiError):
            Engine(api, self.path, expected_cuid=lambda: 'b').step()
        self.assertEqual(api.calls, [])

    def test_missing_or_changed_deadline_format_fails_closed(self):
        api = FakeApi()
        del api.info['left_treasure_chest']
        with self.assertRaises(TransportError):
            parse_state(api.info)
        api.info['left_treasure_chest'] = NOW
        with self.assertRaises(TransportError):
            parse_state(api.info)

    def test_clock_uses_monotonic_progress(self):
        values = [20]
        clock = ServerClock(lambda: values[0])
        clock.update('Tue, 27 Oct 2026 12:00:00 GMT')
        base = clock.now()
        values[0] += 3
        self.assertEqual(clock.now(), base + 3)

if __name__ == '__main__':
    unittest.main(verbosity=2)
