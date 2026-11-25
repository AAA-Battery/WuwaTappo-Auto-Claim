import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chest_autoclaim_ui as ui_module

GEAR = '⚙'


class UpdateUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest('no display available')
        self.root.withdraw()
        self.calls = []
        self.ui = ui_module.ChestAutoClaimUI(
            self.root,
            on_check_update=lambda: self.calls.append('check'),
            on_install_update=lambda: self.calls.append('install'),
        )

    def tearDown(self):
        try:
            # Let the dialog's pending after() callbacks run before the root goes away.
            self.root.update()
            time.sleep(0.15)
            self.root.update()
            self.root.destroy()
        except tk.TclError:
            pass

    def dialog(self):
        self.ui.open_settings()
        self.root.update()
        return self.ui._settings

    def test_row_states(self):
        expected = {
            'idle': ('Check', True),
            'checking': ('Checking...', False),
            'uptodate': ('Check', True),
            'available': ('Update now', True),
            'downloading': ('Updating...', False),
            'error': ('Check', True),
        }
        dialog = self.dialog()
        for state, (label, enabled) in expected.items():
            with self.subTest(state):
                self.ui.set_update_state(state, f'text for {state}')
                self.root.update()
                self.assertEqual(dialog._update_button.cget('text'), label)
                self.assertEqual(dialog._update_button._enabled, enabled)
                self.assertEqual(dialog._update_desc.cget('text'), f'text for {state}')

    def test_button_dispatch(self):
        dialog = self.dialog()
        self.ui.set_update_state('idle', 'x')
        dialog._update_button._command()
        self.assertEqual(self.calls, ['check'])
        self.ui.set_update_state('available', 'v1.3.0 is available')
        dialog._update_button._command()
        self.assertEqual(self.calls, ['check', 'install'])
        self.ui.set_update_state('error', 'boom')
        dialog._update_button._command()
        self.assertEqual(self.calls, ['check', 'install', 'check'])

    def test_state_survives_dialog_reopen(self):
        dialog = self.dialog()
        self.ui.set_update_state('error', 'X went wrong')
        dialog.close()
        self.root.update()
        reopened = self.dialog()
        self.assertEqual(reopened._update_desc.cget('text'), 'X went wrong')

    def test_state_set_before_dialog_exists(self):
        self.ui.set_update_state('available', 'v9.9.9 is available')
        dialog = self.dialog()
        self.assertEqual(dialog._update_button.cget('text'), 'Update now')
        self.assertEqual(dialog._update_desc.cget('text'), 'v9.9.9 is available')

    def test_gear_hint(self):
        self.assertEqual(self.ui._gear.cget('text'), GEAR)
        self.ui.set_update_state('available', 'v1.3.0 is available')
        self.assertEqual(self.ui._gear.cget('text'), GEAR + ' Update available')
        self.ui.set_update_state('uptodate', 'ok')
        self.assertEqual(self.ui._gear.cget('text'), GEAR)

    def test_existing_settings_rows_still_work(self):
        dialog = self.dialog()
        self.assertTrue(dialog._resync_button.winfo_exists())
        self.assertTrue(dialog._add_clicks_button.winfo_exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
