import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main
from updater import UpdateInfo

INFO = UpdateInfo('1.3.0', 'WuwaTappoAutoClaim-v1.3.0.zip', 'https://z', 'https://s', 'https://p')


class UpdateMessageTests(unittest.TestCase):
    def test_new_version_available(self):
        data = {'info': INFO, 'error': None, 'silent': False}
        self.assertEqual(main.update_message_to_ui('update_checked', data, '1.2.2'),
                         ('available', 'v1.3.0 is available'))

    def test_up_to_date(self):
        data = {'info': None, 'error': None, 'silent': False}
        self.assertEqual(main.update_message_to_ui('update_checked', data, '1.2.2'),
                         ('uptodate', "You're up to date (v1.2.2)"))

    def test_silent_up_to_date_still_resets_state(self):
        data = {'info': None, 'error': None, 'silent': True}
        self.assertEqual(main.update_message_to_ui('update_checked', data, '1.2.2'),
                         ('uptodate', "You're up to date (v1.2.2)"))

    def test_error_is_shown_for_manual_check_only(self):
        manual = {'info': None, 'error': 'GitHub rate limit reached; try again later.', 'silent': False}
        silent = dict(manual, silent=True)
        self.assertEqual(main.update_message_to_ui('update_checked', manual, '1.2.2'),
                         ('error', 'GitHub rate limit reached; try again later.'))
        self.assertIsNone(main.update_message_to_ui('update_checked', silent, '1.2.2'))

    def test_progress_and_failure(self):
        self.assertEqual(main.update_message_to_ui('update_progress', {'text': 'Downloading 12 / 38 MB'}, '1.2.2'),
                         ('downloading', 'Downloading 12 / 38 MB'))
        self.assertEqual(main.update_message_to_ui('update_failed', {'error': 'bad hash'}, '1.2.2'),
                         ('error', 'bad hash'))

    def test_unknown_event(self):
        self.assertIsNone(main.update_message_to_ui('claimed', {}, '1.2.2'))

    def test_progress_text(self):
        self.assertEqual(main.format_progress(12 * 1024 * 1024, 38 * 1024 * 1024), 'Downloading 12 / 38 MB')
        self.assertEqual(main.format_progress(5 * 1024 * 1024, None), 'Downloading 5 MB')


class ManualCheckDuringSilentCheckTests(unittest.TestCase):
    def test_manual_press_during_silent_check_still_shows_the_error(self):
        import logging
        import queue
        import threading
        import types
        import updater

        shown = []
        app = main.TrayApp.__new__(main.TrayApp)
        app.stopping = False
        app.update_busy = False
        app.update_silent = True
        app.messages = queue.Queue()
        app.log = logging.getLogger('test-update')
        app.ui = types.SimpleNamespace(set_update_state=lambda *args: shown.append(args))
        gate = threading.Event()

        def slow_failing_check(current):
            gate.wait(5)
            raise updater.UpdateError('Could not reach GitHub. Check your internet connection.')

        original = main.updater.check_for_update
        main.updater.check_for_update = slow_failing_check
        try:
            app.check_for_updates(silent=True)    # startup check, still running...
            app.check_for_updates(silent=False)   # ...user presses Check
            gate.set()
            event, data = app.messages.get(timeout=5)
        finally:
            main.updater.check_for_update = original
        self.assertEqual(event, 'update_checked')
        self.assertFalse(data['silent'])
        self.assertEqual(main.update_message_to_ui(event, data, '1.2.2'),
                         ('error', 'Could not reach GitHub. Check your internet connection.'))


class InstallDirTests(unittest.TestCase):
    def test_not_frozen_uses_the_source_folder(self):
        self.assertFalse(getattr(sys, 'frozen', False))
        self.assertEqual(main.install_dir(), Path(main.__file__).resolve().parent)

    def test_frozen_uses_the_executable_folder(self):
        original_exe = sys.executable
        sys.frozen = True
        sys.executable = str(Path(main.__file__).resolve().parent / 'WuwaTappoAutoClaim.exe')
        try:
            self.assertEqual(main.install_dir(), Path(main.__file__).resolve().parent)
        finally:
            del sys.frozen
            sys.executable = original_exe

    def test_version_is_1_3_0(self):
        self.assertEqual(main.VERSION, '1.3.0')


if __name__ == '__main__':
    unittest.main(verbosity=2)
