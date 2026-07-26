import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_ORIGINAL_ARGV0 = sys.argv[0]
sys.argv[0] = str(ROOT / "main.py")
try:
    import main
finally:
    sys.argv[0] = _ORIGINAL_ARGV0


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class FakeObserver:
    def __init__(self):
        self.schedules = []
        self.started = False
        self.stopped = False

    def schedule(self, handler, path, recursive):
        self.schedules.append((handler, path, recursive))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        return None


class Issue19Tests(unittest.TestCase):
    @staticmethod
    def _game(save_dir: Path, *, name="Test Game"):
        return {
            "name": name,
            "appid": "123",
            "save_path": str(save_dir),
            "save_paths": [str(save_dir)],
            "auto_backup": True,
        }

    def test_metadata_wait_requires_consecutive_stable_samples(self):
        first = (("slot.sav", 1),)
        stable = (("slot.sav", 2),)
        signatures = iter([first, stable, stable, stable])
        result = main.wait_for_save_metadata_stable(
            [{"base": "unused"}],
            stable_samples=3,
            interval=0,
            timeout=1,
            signature_func=lambda _specs: next(signatures),
        )
        self.assertEqual(stable, result)

    def test_cooldown_keeps_latest_pending_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            (save_dir / "slot.sav").write_bytes(b"save")
            handler = main.SaveChangeHandler(self._game(save_dir), cooldown=60)
            now = [120.0]
            handler._clock = lambda: now[0]
            handler._timer_factory = FakeTimer
            handler._quiet_delay = 0
            handler._last_backup = 100.0
            handler._perform_backup_with_retry = mock.Mock(return_value=True)

            handler._schedule_backup()
            handler._timer.fire()
            first_cooldown_timer = handler._timer
            self.assertTrue(handler._pending)
            self.assertEqual(40.0, first_cooldown_timer.delay)

            now[0] = 130.0
            handler._schedule_backup()
            self.assertTrue(first_cooldown_timer.cancelled)
            handler._timer.fire()
            final_timer = handler._timer
            self.assertEqual(30.0, final_timer.delay)

            now[0] = 160.0
            final_timer.fire()
            handler._perform_backup_with_retry.assert_called_once_with()
            self.assertFalse(handler._pending)
            self.assertEqual(160.0, handler._last_backup)
            handler.close()

    def test_cancelled_timer_callback_cannot_replace_latest_timer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            (save_dir / "slot.sav").write_bytes(b"save")
            handler = main.SaveChangeHandler(self._game(save_dir), cooldown=60)
            handler._clock = lambda: 120.0
            handler._timer_factory = FakeTimer
            handler._quiet_delay = 0
            handler._last_backup = 100.0

            handler._schedule_backup()
            stale_timer = handler._timer
            handler._schedule_backup()
            latest_timer = handler._timer
            self.assertTrue(stale_timer.cancelled)

            stale_timer.callback()
            self.assertIs(latest_timer, handler._timer)
            self.assertFalse(latest_timer.cancelled)
            handler.close()

    def test_transient_errors_retry_whole_backup_and_update_cooldown_on_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            (save_dir / "slot.sav").write_bytes(b"save")
            callback = mock.Mock()
            handler = main.SaveChangeHandler(
                self._game(save_dir),
                cooldown=60,
                on_backup_created=callback,
            )
            handler._clock = lambda: 200.0
            handler._backup_attempts = 3
            handler._retry_backoff = (0, 0)
            handler._pending = True
            handler._last_event_at = 0

            attempts = []

            def flaky_backup(*_args, **_kwargs):
                attempts.append(handler._last_backup)
                if len(attempts) < 3:
                    raise PermissionError("locked")
                return "backup.zip"

            with mock.patch.object(
                    main, "wait_for_save_metadata_stable", return_value=(("stable",),)), \
                    mock.patch.object(main, "create_backup", side_effect=flaky_backup):
                handler._try_backup()

            self.assertEqual([0, 0, 0], attempts)
            self.assertEqual(200.0, handler._last_backup)
            callback.assert_called_once()
            handler.close()

    def test_exhausted_retries_do_not_advance_cooldown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            (save_dir / "slot.sav").write_bytes(b"save")
            handler = main.SaveChangeHandler(self._game(save_dir), cooldown=0)
            handler._clock = lambda: 200.0
            handler._timer_factory = FakeTimer
            handler._backup_attempts = 3
            handler._retry_backoff = (0, 0)
            handler._failure_retry_delay = 30
            handler._pending = True
            handler._last_backup = 25.0

            with mock.patch.object(
                    main, "wait_for_save_metadata_stable", return_value=(("stable",),)), \
                    mock.patch.object(
                        main,
                        "create_backup",
                        side_effect=PermissionError("still locked"),
                    ), \
                    self.assertLogs(main.logger, level="WARNING"):
                handler._try_backup()

            self.assertEqual(25.0, handler._last_backup)
            self.assertTrue(handler._pending)
            self.assertIsNotNone(handler._timer)
            self.assertEqual(30.0, handler._timer.delay)
            handler.close()

    def test_programming_error_is_not_retried_forever(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            (save_dir / "slot.sav").write_bytes(b"save")
            handler = main.SaveChangeHandler(self._game(save_dir), cooldown=0)
            handler._pending = True
            handler._perform_backup_with_retry = mock.Mock(
                side_effect=ValueError("unexpected")
            )

            with self.assertLogs(main.logger, level="ERROR"):
                handler._try_backup()

            self.assertFalse(handler._pending)
            self.assertIsNone(handler._timer)
            handler.close()

    def test_close_after_settle_prevents_backup_from_starting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            (save_dir / "slot.sav").write_bytes(b"save")
            handler = main.SaveChangeHandler(self._game(save_dir), cooldown=0)

            def settle_then_close(*_args, **_kwargs):
                handler._stop_event.set()
                return (("stable",),)

            with mock.patch.object(
                    main,
                    "wait_for_save_metadata_stable",
                    side_effect=settle_then_close,
                    ), mock.patch.object(main, "create_backup") as create_mock:
                self.assertFalse(handler._perform_backup_with_retry())

            create_mock.assert_not_called()
            self.assertFalse(handler._retryable_failure)
            handler.close()

    def test_source_change_discards_archive_before_publish(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_dir = root / "save"
            backup_dir = root / "backups"
            save_dir.mkdir()
            source = save_dir / "slot.sav"
            source.write_bytes(b"before")
            game = self._game(save_dir)
            specs = main.get_game_save_specs(game, existing_only=True)
            expected = main.save_specs_metadata_signature(specs)
            original_write = main._zip_write_file

            def write_then_change(zf, file_path, arcname):
                original_write(zf, file_path, arcname)
                source.write_bytes(b"changed-after-read")

            with mock.patch.object(main, "BACKUP_ROOT", backup_dir), \
                    mock.patch.object(main, "_is_network_path", return_value=False), \
                    mock.patch.object(main, "_zip_write_file", side_effect=write_then_change), \
                    mock.patch.object(main, "enforce_backup_limits"):
                with self.assertRaises(main.SaveSourceChangedError):
                    main.create_backup(game, expected_source_signature=expected)

            self.assertEqual([], list(backup_dir.rglob("*.zip")))
            self.assertEqual([], list(backup_dir.rglob("*.tmp")))

    def test_network_target_builds_locally_then_commits_to_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_dir = root / "save"
            backup_dir = root / "network-target"
            local_temp = root / "local-temp"
            save_dir.mkdir()
            (save_dir / "slot.sav").write_bytes(b"network-stage")
            opened_paths = []
            original_open = main._open_zip_for_write

            def capture_open(path):
                opened_paths.append(Path(path))
                return original_open(path)

            with mock.patch.object(main, "BACKUP_ROOT", backup_dir), \
                    mock.patch.object(main, "_is_network_path", return_value=True), \
                    mock.patch.object(main.tempfile, "gettempdir", return_value=str(local_temp)), \
                    mock.patch.object(main, "_open_zip_for_write", side_effect=capture_open), \
                    mock.patch.object(main, "enforce_backup_limits"):
                result = main.create_backup(self._game(save_dir))

            result_path = Path(result)
            self.assertTrue(result_path.is_file())
            self.assertTrue(result_path.with_suffix(".meta.json").is_file())
            self.assertTrue(result_path.is_relative_to(backup_dir))
            self.assertTrue(opened_paths[0].is_relative_to(local_temp))
            self.assertFalse(opened_paths[0].is_relative_to(backup_dir))
            self.assertEqual([], list(backup_dir.rglob("*.tmp")))
            self.assertEqual([], list(local_temp.rglob("*.tmp")))
            with zipfile.ZipFile(result_path, "r") as archive:
                self.assertEqual(b"network-stage", archive.read("slot.sav"))

    def test_failed_network_publish_removes_published_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_dir = root / "save"
            backup_dir = root / "network-target"
            local_temp = root / "local-temp"
            save_dir.mkdir()
            (save_dir / "slot.sav").write_bytes(b"network-stage")

            with mock.patch.object(main, "BACKUP_ROOT", backup_dir), \
                    mock.patch.object(main, "_is_network_path", return_value=True), \
                    mock.patch.object(main.tempfile, "gettempdir", return_value=str(local_temp)), \
                    mock.patch.object(
                        main,
                        "_commit_staged_zip",
                        side_effect=OSError("network disconnected"),
                    ), \
                    mock.patch.object(main, "enforce_backup_limits"):
                with self.assertRaises(OSError):
                    main.create_backup(self._game(save_dir))

            self.assertEqual([], list(backup_dir.rglob("*.zip")))
            self.assertEqual([], list(backup_dir.rglob("*.json")))
            self.assertEqual([], list(backup_dir.rglob("*.tmp")))
            self.assertEqual([], list(local_temp.rglob("*.tmp")))

    def test_local_target_keeps_same_directory_atomic_temp_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_dir = root / "save"
            backup_dir = root / "backups"
            save_dir.mkdir()
            (save_dir / "slot.sav").write_bytes(b"local")
            opened_paths = []
            original_open = main._open_zip_for_write

            def capture_open(path):
                opened_paths.append(Path(path))
                return original_open(path)

            with mock.patch.object(main, "BACKUP_ROOT", backup_dir), \
                    mock.patch.object(main, "_is_network_path", return_value=False), \
                    mock.patch.object(main, "_open_zip_for_write", side_effect=capture_open), \
                    mock.patch.object(main, "enforce_backup_limits"):
                result = main.create_backup(self._game(save_dir))

            self.assertEqual(Path(result).parent, opened_paths[0].parent)
            self.assertEqual([], list(backup_dir.rglob("*.tmp")))

    def test_multiple_save_paths_share_one_handler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            game = self._game(first)
            game["save_paths"] = [str(first), str(second)]

            class FakeApp:
                def __init__(self):
                    self.cfg = {"games": [game], "watch_cooldown": 60}
                    self._watchers = []
                    self._watch_handlers = []

                def _stop_watchers(self):
                    self._watchers.clear()
                    self._watch_handlers.clear()

                def after(self, _delay, callback):
                    return callback

                def _on_backups_changed(self, _game):
                    return None

            app = FakeApp()
            with mock.patch.object(main, "HAS_WATCHDOG", True), \
                    mock.patch.object(main, "Observer", FakeObserver):
                main.SteamSaveManager._start_watchers(app)

            self.assertEqual(1, len(app._watch_handlers))
            self.assertEqual(1, len(app._watchers))
            observer = app._watchers[0]
            self.assertEqual(2, len(observer.schedules))
            self.assertIs(observer.schedules[0][0], observer.schedules[1][0])
            main.SteamSaveManager._stop_watchers(app)


if __name__ == "__main__":
    unittest.main()
