"""Draft editing, async controller calls and native four-language alert layout."""
import copy
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.audio import SOUND_IDS
from aion2_redbag_map.desktop_alerts import AlertSettingsDialog
from aion2_redbag_map.desktop_text import text
from aion2_redbag_map.localization import SUPPORTED_LANGUAGES


class FakeController:
    def __init__(self, **changes):
        self.alert_settings = {"sound_profile": "chime", "sound_volume": 35,
                               "overlay_on_alert": False, **changes}
        self.saves, self.previews, self.worker_threads = [], [], []
        self.save_error = self.preview_error = None
        self.preview_result = None
        self.block = None

    def set_alert_settings(self, **values):
        self.worker_threads.append(threading.get_ident())
        if self.block is not None:
            self.block.wait(2)
        if self.save_error is not None:
            raise self.save_error
        self.saves.append(dict(values))
        self.alert_settings.update(values)

    def preview_sound(self, profile, volume):
        self.worker_threads.append(threading.get_ident())
        if self.block is not None:
            self.block.wait(2)
        if self.preview_error is not None:
            raise self.preview_error
        self.previews.append((profile, volume))
        return self.preview_result


@unittest.skipUnless(os.name == "nt", "Native Windows settings widgets and fonts")
class AlertDialogTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        from aion2_redbag_map.ui_theme import enable_dpi_awareness
        enable_dpi_awareness()
        try:
            self.parent = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.parent.attributes("-alpha", 0)
        self.parent.geometry("20x20+0+0")
        ttk.Style(self.parent).theme_use("clam")
        self.parent.update()
        self.controller = FakeController()
        self.dialogs = []
        original = tk.Toplevel

        def transparent(*args, **kwargs):
            window = original(*args, **kwargs)
            window.attributes("-alpha", 0)
            return window
        self.top_patch = patch("tkinter.Toplevel", side_effect=transparent)
        self.top_patch.start()

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
        self.top_patch.stop()
        self.parent.destroy()

    def create(self, language="zh-CN"):
        dialog = AlertSettingsDialog(self.parent, self.controller, language)
        self.dialogs.append(dialog)
        self.parent.update()
        return dialog

    def settle(self, dialog):
        deadline = time.monotonic() + 2
        while dialog.busy and time.monotonic() < deadline:
            self.parent.update()
            time.sleep(.01)
        self.assertFalse(dialog.busy, "Background action did not finish")
        self.parent.update()

    def choose(self, dialog, profile="bubble", volume=23, overlay=True):
        dialog.sound_select.current(SOUND_IDS.index(profile))
        dialog.volume_var.set(volume)
        dialog.overlay_var.set(overlay)

    def test_existing_preferences_are_drafts_until_saved(self):
        self.controller.alert_settings.update(sound_profile="alarm", sound_volume=54, overlay_on_alert=True)
        dialog = self.create()
        self.assertEqual(dialog.draft(), self.controller.alert_settings)
        self.choose(dialog)
        self.assertEqual(self.controller.alert_settings["sound_profile"], "alarm")
        self.assertEqual(self.controller.saves, [])
        dialog.save_button.invoke()
        self.settle(dialog)
        self.assertEqual(self.controller.saves, [{"sound_profile": "bubble", "sound_volume": 23, "overlay_on_alert": True}])
        self.assertEqual(dialog.feedback.cget("text"), text("alerts_saved"))
        self.assertTrue(all(thread != threading.get_ident() for thread in self.controller.worker_threads))
        self.assertEqual(self.controller.previews, [])

    def test_preview_uses_draft_but_never_saves(self):
        dialog = self.create("en")
        original = copy.deepcopy(self.controller.alert_settings)
        self.choose(dialog, "sonar", 17, True)
        dialog.preview_button.invoke()
        self.settle(dialog)
        self.assertEqual(self.controller.previews, [("sonar", 17)])
        self.assertEqual(self.controller.alert_settings, original)
        self.assertEqual(self.controller.saves, [])
        self.assertEqual(dialog.feedback.cget("text"), text("alerts_preview_sent", "en"))
        self.assertTrue(all(thread != threading.get_ident() for thread in self.controller.worker_threads))

    def test_zero_volume_preview_is_described_as_silent(self):
        dialog = self.create()
        self.choose(dialog, volume=0)
        self.controller.preview_result = False
        dialog.preview()
        self.settle(dialog)
        self.assertEqual(self.controller.previews, [("bubble", 0)])
        self.assertEqual(dialog.feedback.cget("text"), text("alerts_muted"))
        self.assertEqual(self.controller.saves, [])

    def test_failed_save_keeps_draft_visible_and_can_be_retried(self):
        dialog = self.create("ko")
        self.choose(dialog, "arcade", 42, True)
        self.controller.save_error = PermissionError("not writable")
        dialog.save()
        self.settle(dialog)
        self.assertEqual(dialog.feedback.cget("text"), text("alerts_failed", "ko"))
        self.assertEqual(dialog.draft(), {"sound_profile": "arcade", "sound_volume": 42, "overlay_on_alert": True})
        self.assertEqual(self.controller.saves, [])
        self.assertEqual(self.controller.alert_settings["sound_profile"], "chime")
        self.controller.save_error = None
        dialog.save()
        self.settle(dialog)
        self.assertEqual(dialog.feedback.cget("text"), text("alerts_saved", "ko"))
        self.assertEqual(len(self.controller.saves), 1)

    def test_rejected_preview_is_not_reported_as_sent(self):
        dialog = self.create("en")
        self.controller.preview_result = False
        dialog.preview()
        self.settle(dialog)
        self.assertEqual(dialog.feedback.cget("text"), text("alerts_failed", "en"))
        self.assertEqual(self.controller.saves, [])

    def test_async_work_leaves_ui_responsive_and_close_only_destroys_dialog(self):
        dialog = self.create()
        self.controller.block = threading.Event()
        dialog.preview()
        self.assertTrue(dialog.busy)
        self.assertEqual(str(dialog.preview_button.cget("state")), "disabled")
        self.assertEqual(str(dialog.sound_select.cget("state")), "disabled")
        self.parent.update()
        dialog.close()
        self.assertTrue(self.parent.winfo_exists())
        self.assertFalse(dialog.root.winfo_exists())
        self.controller.block.set()
        self.assertEqual(self.controller.saves, [])

    def test_closing_unsaved_draft_does_not_write_or_exit_parent(self):
        dialog = self.create()
        original = copy.deepcopy(self.controller.alert_settings)
        self.choose(dialog)
        dialog.close_button.invoke()
        self.assertEqual(self.controller.alert_settings, original)
        self.assertEqual(self.controller.saves, [])
        self.assertTrue(self.parent.winfo_exists())

    def test_four_language_labels_and_layout_preserve_draft(self):
        dialog = self.create()
        self.choose(dialog, "bell", 49, True)
        draft = dialog.draft()
        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                dialog.set_language(language)
                self.parent.update()
                self.assertEqual(dialog.draft(), draft)
                self.assertEqual(dialog.root.title(), text("alert_settings", language))
                self.assertEqual(dialog.sound_select.get(), text("sound_bell", language))
                self.assertEqual(dialog.volume_label.cget("text"), "49%")
                for widget, key in dialog.localized_widgets:
                    self.assertEqual(widget.cget("text"), text(key, language))
                    self.assertLessEqual(widget.winfo_reqwidth(), widget.winfo_width() + 2, (language, key, "clipped"))
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                         dialog.root.winfo_rooty() + dialog.root.winfo_height(), (language, key, "outside dialog"))
        self.assertEqual(self.controller.saves, [])
        self.assertEqual(self.controller.previews, [])


if __name__ == "__main__":
    unittest.main()
