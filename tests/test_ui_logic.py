import unittest
import weakref

import main


class FakeVariable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class FakeStateWidget:
    def __init__(self, state):
        self.state = state

    def cget(self, key):
        if key != "state":
            raise KeyError(key)
        return self.state

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)

    def winfo_exists(self):
        return True


class UiLogicTests(unittest.TestCase):
    def test_flat_ui_icon_assets_are_nonempty(self):
        icon_names = (
            "archive", "archive-restore", "arrow-left", "chevron-left",
            "chevron-right", "circle-check", "copy", "database-backup",
            "ellipsis", "folder-open", "folder-search", "gamepad-2", "house",
            "minus", "plus", "refresh-cw", "scan-search", "settings", "star",
            "trash-2", "triangle-alert", "x",
        )
        for icon_name in icon_names:
            icon_path = main.UI_ICON_DIR / f"{icon_name}.png"
            self.assertTrue(icon_path.is_file(), icon_path)
            with main.Image.open(icon_path) as image:
                self.assertIsNotNone(image.convert("RGBA").getbbox(), icon_path)

    def test_home_actions_follow_available_content(self):
        app = object.__new__(main.SteamSaveManager)
        app.cfg = {"games": [], "sync_enabled": False, "sync_folder": ""}
        self.assertEqual(
            {"backup": False, "sync": False, "scan": True},
            app._home_action_availability(),
        )

        app.cfg["games"] = [{"name": "Example"}]
        self.assertTrue(app._home_action_availability()["backup"])

    def test_ui_text_truncation_preserves_short_values(self):
        truncate = main.SteamSaveManager._truncate_ui_text
        self.assertEqual("short", truncate("short", 8))
        self.assertEqual("long…", truncate("longer", 5))

    def test_busy_state_restores_each_widgets_previous_state(self):
        normal = FakeStateWidget("normal")
        unavailable = FakeStateWidget("disabled")
        app = object.__new__(main.SteamSaveManager)
        app._io_busy = False
        app._io_busy_action = None
        app._busy_widgets = weakref.WeakSet((normal, unavailable))
        app._busy_widget_states = weakref.WeakKeyDictionary()
        app._refresh_home_action_states = lambda: None

        app._set_io_busy(True, "backup")
        self.assertEqual((normal.state, unavailable.state), ("disabled", "disabled"))

        app._set_io_busy(False)
        self.assertEqual((normal.state, unavailable.state), ("normal", "disabled"))

    def test_responsive_width_uses_logical_pixels(self):
        window = type(
            "ScaledWindow",
            (),
            {"winfo_width": lambda _self: 1680, "_get_window_scaling": lambda _self: 1.5},
        )()

        self.assertEqual(1120, main.SteamSaveManager._logical_window_width(window))

    def test_question_dialog_prefers_safe_negative_actions(self):
        yes_no = [("Yes", True, "", ""), ("No", False, "", "")]
        yes_no_cancel = yes_no + [("Cancel", None, "", "")]

        self.assertIs(False, main.SteamSaveManager._modal_safe_value("question", yes_no))
        self.assertIsNone(main.SteamSaveManager._modal_safe_value("question", yes_no_cancel))
        self.assertEqual("ok", main.SteamSaveManager._modal_safe_value("info", []))

    def test_scan_selection_uses_checkbox_then_confidence_default(self):
        app = object.__new__(main.SteamSaveManager)
        app._scan_selected_vars = {"10": FakeVariable("off"), "20": FakeVariable("on")}

        self.assertFalse(app._is_scan_result_selected({"appid": "10"}))
        self.assertTrue(app._is_scan_result_selected({"appid": "20"}))
        self.assertTrue(app._is_scan_result_selected({
            "appid": "30", "save_candidates": [{"confidence": "medium"}],
        }))
        self.assertFalse(app._is_scan_result_selected({
            "appid": "40", "save_candidates": [{"confidence": "low"}],
        }))

    def test_backup_source_labels_are_classified_for_filters(self):
        classify = main.SteamSaveManager._backup_source_code

        self.assertEqual("sync", classify("同步前安全备份"))
        self.assertEqual("automatic", classify("游戏退出后自动备份"))
        self.assertEqual("automatic", classify("Scheduled backup"))
        self.assertEqual("manual", classify("手动备份"))


if __name__ == "__main__":
    unittest.main()
