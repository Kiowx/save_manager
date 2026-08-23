import unittest

import main


class FakeVariable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class UiLogicTests(unittest.TestCase):
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
