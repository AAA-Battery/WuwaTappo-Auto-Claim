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
        self.fail_seq = []
        self.bump_on_fail = 0
        self.stale = None
        self.info = {
            'cuid': 'a',
            'total_click_count': 5000,
            'total_click_consume': 5000 - balance,
            'left_treasure_chest': (NOW + left) * 1000,
            'right_treasure_chest': (NOW + right) * 1000
        }

    def me(self):
        # `stale` simulates /user/me still serving the pre-claim snapshot.
        return dict(self.stale if self.stale is not None else self.info)

    def open(self, slot, gross):
        self.calls.append((slot, gross))
        if self.fail_seq:
            # Simulate Tappo reporting more clicks while the claim was in flight.
            self.info['total_click_count'] += self.bump_on_fail
            raise self.fail_seq.pop(0)
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

    def test_sends_live_tappo_click_count_when_it_is_ahead_of_server(self):
        api = FakeApi(-1, 100)
        Engine(api, self.path, click_source=lambda: 5007).step()
        self.assertEqual(api.calls, [('left', 5007)])

    def test_ignores_unusable_live_click_counts(self):
        for live in (None, 4999, 5000 + 10_000_000, True, 5001.5, 'x'):
            with self.subTest(live=live):
                api = FakeApi(-1, 100)
                with tempfile.TemporaryDirectory() as tmp:
                    Engine(api, Path(tmp) / 'j.json', click_source=lambda live=live: live).step()
                self.assertEqual(api.calls, [('left', 5000)])

    def test_failing_click_source_falls_back_to_server_count(self):
        def broken():
            raise RuntimeError('cdp unavailable')
        api = FakeApi(-1, 100)
        Engine(api, self.path, click_source=broken).step()
        self.assertEqual(api.calls, [('left', 5000)])

    def test_invalid_click_count_is_retried_at_once_with_fresh_totals(self):
        api = FakeApi(-1, 100)
        api.fail_seq = [ApiError(70003)]
        api.bump_on_fail = 40
        sleeps = []
        engine = Engine(api, self.path, sleep=sleeps.append)
        self.assertEqual(engine.step(), 2)
        self.assertEqual(api.calls, [('left', 5000), ('left', 5040)])
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(engine.attempts['left']['outcome'], 'success')

    def test_invalid_click_count_gives_up_after_bounded_retries(self):
        api = FakeApi(-1, 100)
        api.fail = ApiError(70003)
        sleeps = []
        engine = Engine(api, self.path, sleep=sleeps.append)
        self.assertEqual(engine.step(), 30)
        self.assertEqual(len(api.calls), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(engine.attempts['left']['outcome'], 'rejected')
        engine.step()
        self.assertEqual(len(api.calls), 3)

    def test_other_rejections_are_not_retried_in_place(self):
        api = FakeApi(-1, 100)
        api.fail = ApiError(70006)
        sleeps = []
        Engine(api, self.path, sleep=sleeps.append).step()
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(sleeps, [])

    def _statuses(self, events):
        return [kw for name, kw in events if name == 'status']

    def test_countdown_runs_while_user_me_still_serves_pre_claim_deadline(self):
        api = FakeApi(-1, 100)
        events = []
        engine = Engine(api, self.path, lambda n, **kw: events.append((n, kw)))
        api.stale = dict(api.info)
        engine.step()
        self.assertEqual(api.calls, [('left', 5000)])
        api.clock.value += 2
        events.clear()
        engine.step()
        status = self._statuses(events)[0]
        self.assertAlmostEqual(status['left'], 1798, delta=1)
        self.assertAlmostEqual(status['right'], 98, delta=1)
        self.assertEqual(len(api.calls), 1)

    def test_countdown_survives_restart_while_user_me_is_stale(self):
        api = FakeApi(-1, 100)
        Engine(api, self.path).step()
        api.stale = {**api.info,
                     'left_treasure_chest': (NOW - 1) * 1000}
        api.clock.value += 5
        events = []
        Engine(api, self.path, lambda n, **kw: events.append((n, kw))).step()
        self.assertAlmostEqual(self._statuses(events)[0]['left'], 1795, delta=1)
        self.assertEqual(len(api.calls), 1)

    def test_fresh_user_me_deadline_replaces_the_estimate(self):
        api = FakeApi(-1, 100)
        engine = Engine(api, self.path)
        api.stale = dict(api.info)
        engine.step()
        api.stale = None
        api.info['left_treasure_chest'] = (api.clock.now() + 1790) * 1000
        events = []
        engine.emit = lambda n, **kw: events.append((n, kw))
        engine.step()
        self.assertAlmostEqual(self._statuses(events)[0]['left'], 1790, delta=1)

    def test_estimate_is_ignored_once_it_has_expired(self):
        api = FakeApi(-1, 100)
        engine = Engine(api, self.path)
        stale = dict(api.info)
        api.stale = stale
        engine.step()
        api.clock.value += 1801
        events = []
        engine.emit = lambda n, **kw: events.append((n, kw))
        engine.step()
        self.assertEqual(self._statuses(events)[0]['left'], 0)

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
