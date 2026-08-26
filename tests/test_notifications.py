import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music_organizer.models import RunResult
from music_organizer.notifications import (
    format_job_notification,
    magicpush_endpoint,
    send_magicpush,
)
from worker import OrganizerWorker


class MagicPushTests(unittest.TestCase):
    def test_magicpush_uses_bearer_header_and_text_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("test-secret-token\n", encoding="utf-8")
            response = mock.Mock()
            response.ok = True
            response.status_code = 200
            response.json.return_value = {"success": True}
            config = {
                "enabled": True,
                "base_url": "http://magicpush:818",
                "token_file": str(token_path),
                "timeout": 5,
            }

            with mock.patch(
                "music_organizer.notifications.requests.post",
                return_value=response,
            ) as post:
                result = send_magicpush(config, "整理完成", title="[成功] 整理结果")

            self.assertTrue(result["sent"])
            post.assert_called_once()
            args, kwargs = post.call_args
            self.assertEqual(args[0], "http://magicpush:818/api/push/")
            self.assertEqual(
                kwargs["headers"]["Authorization"], "Bearer test-secret-token"
            )
            self.assertEqual(
                kwargs["json"],
                {
                    "title": "[成功] 整理结果",
                    "content": "整理完成",
                    "type": "text",
                },
            )
            self.assertNotIn("test-secret-token", str(result))

    def test_full_endpoint_is_normalized_with_trailing_slash(self):
        self.assertEqual(
            magicpush_endpoint("http://magicpush:818/api/push/"),
            "http://magicpush:818/api/push/",
        )

    def test_job_message_contains_torrent_album_and_result(self):
        result = RunResult(
            scanned=8,
            organized=6,
            skipped=2,
            message="ok",
            details={
                "torrent_names": ["Artist - Album"],
                "album_names": ["Album"],
            },
        )

        title, content = format_job_notification(
            {"job_type": "qb_poll"}, result
        )

        self.assertIn("[成功]", title)
        self.assertIn("种子名称：Artist - Album", content)
        self.assertIn("专辑名称：Album", content)
        self.assertIn("整理成功：6", content)

    def test_qb_empty_poll_is_silent_but_manual_run_is_not(self):
        worker = OrganizerWorker.__new__(OrganizerWorker)
        worker.organizer = mock.Mock()
        worker.organizer.load_config.return_value = {
            "notifications": {
                "magicpush": {
                    "enabled": True,
                    "notify_no_changes": False,
                }
            }
        }
        worker.repository = mock.Mock()
        empty = RunResult(message="no new completed qb torrents")

        with mock.patch("worker.send_magicpush") as sender:
            worker.notify_job({"id": 1, "job_type": "qb_poll"}, empty)
            sender.assert_not_called()
            sender.return_value = {"sent": True}
            worker.notify_job({"id": 2, "job_type": "manual_scan"}, empty)
            sender.assert_called_once()


if __name__ == "__main__":
    unittest.main()
