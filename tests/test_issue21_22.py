import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.daemon = False

    def start(self):
        return None

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class OneCycleStopEvent:
    def __init__(self):
        self.wait_count = 0

    def is_set(self):
        return self.wait_count >= 2

    def wait(self, _timeout=None):
        self.wait_count += 1
        return self.is_set()

    def set(self):
        self.wait_count = 2


class Issue21And22Tests(unittest.TestCase):
    @staticmethod
    def _game(save_dir: Path, **overrides):
        game = {
            "name": "Palworld",
            "appid": "1623730",
            "save_path": str(save_dir),
            "save_paths": [str(save_dir)],
            "auto_backup": True,
            "watch_backup": True,
            "backup_on_exit": False,
            "sync_enabled": False,
        }
        game.update(overrides)
        return game

    def test_file_change_is_deferred_until_game_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            (save_dir / "Level.sav").write_bytes(b"save")
            running = [True]
            handler = main.SaveChangeHandler(
                self._game(save_dir),
                cooldown=905,
                is_game_running=lambda _game: running[0],
            )
            handler._timer_factory = FakeTimer
            handler._quiet_delay = 0
            handler._running_retry_delay = 5
            handler._perform_backup_with_retry = mock.Mock(return_value=True)

            handler._schedule_backup()
            handler._timer.fire()

            self.assertTrue(handler._pending)
            self.assertTrue(handler._deferred_for_running)
            self.assertEqual(5, handler._timer.delay)
            handler._perform_backup_with_retry.assert_not_called()

            running[0] = False
            self.assertTrue(handler.on_game_stopped(force=False))
            handler._timer.fire()

            handler._perform_backup_with_retry.assert_called_once()
            self.assertFalse(handler._pending)
            self.assertEqual("游戏退出后自动备份", handler._pending_note)
            handler.close()

    def test_exit_mode_creates_backup_without_file_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            (save_dir / "slot-002.sav").write_bytes(b"save")
            handler = main.SaveChangeHandler(
                self._game(save_dir, backup_on_exit=True),
                cooldown=60,
                is_game_running=lambda _game: False,
            )
            handler._timer_factory = FakeTimer
            handler._quiet_delay = 0
            handler._perform_backup_with_retry = mock.Mock(return_value=True)

            self.assertTrue(handler.on_game_stopped(force=True))
            handler._timer.fire()

            handler._perform_backup_with_retry.assert_called_once()
            self.assertEqual("游戏退出后自动备份", handler._pending_note)
            handler.close()

    def test_exit_without_pending_work_or_exit_mode_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = main.SaveChangeHandler(self._game(Path(temp_dir)), cooldown=60)
            handler._timer_factory = FakeTimer

            self.assertFalse(handler.on_game_stopped(force=False))
            self.assertIsNone(handler._timer)
            handler.close()

    def test_process_monitor_tracks_games_even_when_sync_is_disabled(self):
        game = {
            "name": "Local Backup Only",
            "appid": "123",
            "sync_enabled": False,
            "auto_backup": True,
        }
        monitor = main.GameProcessMonitor({"games": [game]})

        self.assertEqual([(main.get_game_runtime_key(game), game)], monitor._get_tracked_games())

    def test_exit_callback_runs_without_sync_backend(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            game = self._game(Path(temp_dir), backup_on_exit=True)
            runtime_key = main.get_game_runtime_key(game)
            callback = mock.Mock()
            monitor = main.GameProcessMonitor(
                {
                    "games": [game],
                    "sync_enabled": False,
                    "sync_mode": "smart",
                    "sync_folder": "",
                },
                on_game_stopped=callback,
            )
            monitor._stop_event = OneCycleStopEvent()
            monitor._find_running_games = mock.Mock(side_effect=[{runtime_key}, set()])

            with mock.patch.object(main, "get_effective_sync_root", return_value=""):
                monitor._monitor_loop()

            callback.assert_called_once()
            self.assertEqual(game["appid"], callback.call_args.args[0]["appid"])

    def test_scheduled_backup_is_queued_instead_of_reading_live_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            game = self._game(Path(temp_dir))
            runtime_key = main.get_game_runtime_key(game)
            handler = mock.Mock()
            app = mock.Mock()
            app.cfg = {
                "games": [game],
                "auto_backup_interval": 1,
            }
            app._auto_stop_event = OneCycleStopEvent()
            app._game_monitor = mock.Mock()
            app._game_monitor._find_running_games.return_value = {runtime_key}
            app._watch_handlers_by_game = {runtime_key: handler}

            with mock.patch.object(main, "create_backup") as create_backup:
                main.SteamSaveManager._auto_loop(app)

            handler.request_scheduled_backup.assert_called_once()
            create_backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
