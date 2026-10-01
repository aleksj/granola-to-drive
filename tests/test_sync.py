import base64
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fetch_granola as fetch
import granola_auth as auth


def token(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "header." + body + ".signature"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auth.json"
        for target, value in [("AUTH_FILE", self.path), ("DESKTOP_FILE", Path(self.tmp.name) / "supabase.json")]:
            p = patch.object(auth, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_login_uses_pkce_and_does_not_request_calendar_access(self):
        url = auth.begin_login()
        params = auth.urllib.parse.parse_qs(auth.urllib.parse.urlparse(url).query)
        pending = json.loads(self.path.with_suffix(".pending.json").read_text())
        expected = base64.urlsafe_b64encode(auth.hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
        self.assertEqual(params["code_challenge"], [expected])
        self.assertEqual(params["request_calendar_permissions"], ["false"])

    def test_login_accepts_current_nested_tokens(self):
        auth.begin_login()
        tokens = {"access_token": token(time.time() + 3600), "refresh_token": "new"}
        with patch.object(auth, "auth_post", return_value={"tokens": tokens, "config": {}}):
            auth.complete_login("https://www.granola.ai/app-redirect?code=one-time-code")
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "new")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.path.with_suffix(".pending.json").exists())

    def test_mismatched_login_is_rejected_before_exchange(self):
        auth.begin_login()
        with patch.object(auth, "auth_post") as post:
            with self.assertRaises(auth.AuthError):
                auth.complete_login("granola://login-complete?code=test&signInClickId=wrong")
        post.assert_not_called()

    def test_expired_sync_token_is_refreshed_and_rotation_saved(self):
        auth.atomic_json(self.path, {"access_token": token(1), "refresh_token": "old"})
        renewed = {"access_token": token(time.time() + 3600), "refresh_token": "rotated"}
        with patch.object(auth, "auth_post", return_value=renewed) as post:
            self.assertEqual(auth.load_token(), renewed["access_token"])
        self.assertEqual(post.call_args.args[1], {"refresh_token": "old"})
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "rotated")

    def test_valid_token_uses_jwt_expiry(self):
        tokens = {"access_token": token(time.time() + 3600), "obtained_at": 1, "expires_in": 0}
        auth.atomic_json(self.path, tokens)
        with patch.object(auth, "auth_post") as post:
            self.assertEqual(auth.load_token(), tokens["access_token"])
        post.assert_not_called()

    def test_refresh_failure_preserves_credentials(self):
        original = {"access_token": token(1), "refresh_token": "original"}
        auth.atomic_json(self.path, original)
        with patch.object(auth, "auth_post", side_effect=auth.AuthError("HTTP 401")):
            with self.assertRaises(auth.AuthError):
                auth.load_token()
        self.assertEqual(json.loads(self.path.read_text()), original)

    def test_encrypted_desktop_store_has_actionable_error(self):
        auth.DESKTOP_FILE.with_suffix(".json.enc").write_bytes(b"encrypted")
        with patch.object(auth, "auth_post") as post:
            with self.assertRaisesRegex(auth.AuthError, "granola_auth.py login"):
                auth.load_token()
        post.assert_not_called()

    def test_stale_desktop_refresh_token_is_never_used(self):
        auth.DESKTOP_FILE.write_text(json.dumps({"workos_tokens": json.dumps({
            "access_token": token(1), "refresh_token": "desktop-owned",
        })}))
        with patch.object(auth, "auth_post") as post:
            with self.assertRaises(auth.AuthError):
                auth.load_token()
        post.assert_not_called()


class FetchTests(unittest.TestCase):
    def test_current_response(self):
        docs = [{"id": "a"}]
        with patch.object(fetch, "post", return_value=(200, {"docs": docs})):
            self.assertEqual(fetch.fetch_documents("test"), docs)

    def test_unrecognized_success_is_not_an_empty_export(self):
        with patch.object(fetch, "post", return_value=(200, {"error": "changed schema"})):
            with self.assertRaises(SystemExit):
                fetch.fetch_documents("test")

    def test_authorization_error_does_not_fall_back(self):
        with patch.object(fetch, "post", return_value=(401, {})) as post:
            with self.assertRaises(SystemExit):
                fetch.fetch_documents("test")
        self.assertEqual(post.call_count, 1)

    def test_repeated_page_is_not_silently_duplicated(self):
        docs = [{"id": str(i)} for i in range(1000)]
        with patch.object(fetch, "post", return_value=(200, {"docs": docs})), patch.object(fetch.time, "sleep"):
            with self.assertRaises(SystemExit):
                fetch.fetch_documents("test")

    def test_changed_corrupt_and_old_empty_caches_are_refetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting.json"
            current = {"updated_at": "new"}
            path.write_text('[{"text":"hello"}]')
            self.assertTrue(fetch.cache_current(path, current, current))
            self.assertFalse(fetch.cache_current(path, current, {"updated_at": "old"}))
            path.write_text('{')
            self.assertFalse(fetch.cache_current(path, current, current))
            path.write_text('[]')
            self.assertTrue(fetch.cache_current(path, current, current))
            os.utime(path, (1, 1))
            self.assertFalse(fetch.cache_current(path, current, current))

    def test_failed_fetch_preserves_document_index_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = [{"id": "a", "updated_at": "old"}]
            (root / "documents.json").write_text(json.dumps(original))
            with patch.object(fetch, "OUT", root), \
                 patch.object(fetch, "TRANSCRIPTS_DIR", root / "transcripts"), \
                 patch.object(fetch, "PANELS_DIR", root / "panels"), \
                 patch.object(fetch, "load_token", return_value="test"), \
                 patch.object(fetch, "fetch_documents", return_value=[{"id": "a", "updated_at": "new"}]), \
                 patch.object(fetch, "fetch_transcript", return_value=([{"text": "updated"}], None)), \
                 patch.object(fetch, "fetch_panels", return_value=(None, "HTTP 503")), \
                 patch.object(fetch.time, "sleep"):
                with self.assertRaises(SystemExit):
                    fetch.main()
            self.assertEqual(json.loads((root / "documents.json").read_text()), original)


class RefreshTests(unittest.TestCase):
    def test_env_file_controls_export_and_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "python"
            python.write_text('#!/bin/bash\nexit 0\n')
            python.chmod(0o700)
            rclone = root / "rclone"
            rclone.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/arguments"\n')
            rclone.chmod(0o700)
            (root / ".granola-to-drive.env").write_text(
                f'GRANOLA_PYTHON="{python}"\nRCLONE_BIN="{rclone}"\n'
                'GRANOLA_RCLONE_REMOTE="custom"\nGRANOLA_DRIVE_FOLDER="Meeting Notes"\n'
                f'GRANOLA_EXPORT_DIR="{root}/export"\n')
            result = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "refresh.sh")],
                                    env={**os.environ, "HOME": tmp}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = (root / "arguments").read_text().splitlines()
            self.assertEqual(args[:3], ["copy", str(root / "export/built"), "custom:Meeting Notes/"])


if __name__ == "__main__":
    unittest.main()
