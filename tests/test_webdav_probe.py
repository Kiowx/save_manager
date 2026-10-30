import contextlib
import io
import unittest
from pathlib import PurePosixPath
from unittest import mock
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

import main

if main.HAS_WEBDAV:
    import requests
    from webdav3.client import Client
    from webdav3.exceptions import RemoteResourceNotFound, ResponseErrorCode


class HeadRejectingWebDAV:
    """HTTP transport fixture using the real webdavclient3 request/XML handling."""

    def __init__(self, *, denied_method=None, corrupt_download=False):
        self.directories = {"/dav", "/dav/存档 同步"}
        self.files = {}
        self.methods = []
        self.denied_method = denied_method
        self.corrupt_download = corrupt_download

    def request(self, method, url, data=None, **kwargs):
        self.methods.append(method)
        path = unquote(urlsplit(url).path).rstrip("/")
        status, payload = 200, b""
        if method == "HEAD":
            status = 405
        elif method == self.denied_method:
            status = 403
        elif method == "MKCOL":
            if path in self.directories:
                status = 405
            elif str(PurePosixPath(path).parent) not in self.directories:
                status = 409
            else:
                self.directories.add(path)
                status = 201
        elif method == "PUT":
            self.files[path] = data.read()
            status = 201
        elif path not in self.directories and path not in self.files:
            status = 404
        elif method == "PROPFIND":
            paths = [path] + sorted(
                child for child in self.directories | self.files.keys()
                if child != path and str(PurePosixPath(child).parent) == path
            )
            responses = []
            for child in paths:
                is_dir = child in self.directories
                href = quote(child + ("/" if is_dir else ""))
                kind = "<d:collection/>" if is_dir else ""
                responses.append(
                    f"<d:response><d:href>{escape(href)}</d:href>"
                    f"<d:propstat><d:prop><d:resourcetype>{kind}</d:resourcetype>"
                    f"<d:getcontentlength>{len(self.files.get(child, b''))}</d:getcontentlength>"
                    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                )
            payload = ('<d:multistatus xmlns:d="DAV:">' + "".join(responses)
                       + "</d:multistatus>").encode()
            status = 207
        elif method == "GET":
            payload = b"corrupt" if self.corrupt_download else self.files[path]
        elif method == "DELETE":
            self.files.pop(path, None)
            self.directories.discard(path)
            status = 204
        else:
            raise AssertionError(f"Unexpected method: {method}")
        response = requests.Response()
        response.status_code = status
        response._content = payload
        response.raw = io.BytesIO(payload)
        response.headers["content-length"] = str(len(payload))
        return response


@unittest.skipUnless(main.HAS_WEBDAV, "webdavclient3 not installed")
class WebDAVProbeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_full_probe_works_without_head_for_all_presets(self):
        for preset in main.WEBDAV_PRESET_OPTIONS:
            with self.subTest(preset=preset):
                transport = HeadRejectingWebDAV()
                with mock.patch("requests.Session.request", side_effect=transport.request):
                    ok, message = main.webdav_test_connection(
                        "https://example.test/dav", "user", "password",
                        preset=preset, base_path="/存档 同步",
                    )
                self.assertTrue(ok, message)
                self.assertNotIn("HEAD", transport.methods)
                self.assertTrue({"PROPFIND", "PUT", "GET", "DELETE"} <= set(transport.methods))
                self.assertFalse(transport.files)
                self.assertFalse(any(".ssm_probe_" in path for path in transport.directories))

    def test_probe_still_rejects_permissions_and_corrupt_content(self):
        for options, expected in (
            ({"denied_method": "PUT"}, "403"),
            ({"denied_method": "GET"}, "403"),
            ({"denied_method": "DELETE"}, "403"),
            ({"corrupt_download": True}, "校验失败"),
        ):
            with self.subTest(options=options):
                transport = HeadRejectingWebDAV(**options)
                with mock.patch("requests.Session.request", side_effect=transport.request), \
                        mock.patch.object(main, "cfg_language", return_value="zh_CN"):
                    ok, message = main.webdav_test_connection(
                        "https://example.test/dav", "user", "password", base_path="/存档 同步",
                    )
                self.assertFalse(ok)
                self.assertIn(expected, message)
                self.assertNotIn("HEAD", transport.methods)

    def test_cached_sync_client_probes_existing_and_missing_directories(self):
        transport = HeadRejectingWebDAV()
        cfg = {"webdav_enabled": True, "webdav_url": "https://example.test/dav"}
        with mock.patch.dict(main._webdav_client_cache, {"fingerprint": None, "client": None}), \
                mock.patch("requests.Session.request", side_effect=transport.request):
            client = main._webdav_make_client(cfg)
            self.assertIsInstance(client, Client)
            self.assertEqual("/存档 同步", main._webdav_find_existing_variant(client, "/存档 同步"))
            with self.assertRaises(FileNotFoundError):
                main._webdav_find_existing_variant(client, "/missing")
        self.assertNotIn("HEAD", transport.methods)

    def test_directory_probe_tries_relative_path_and_preserves_errors(self):
        client = mock.Mock()
        client.list.side_effect = [RemoteResourceNotFound("/folder"), []]
        self.assertEqual("folder", main._webdav_find_existing_variant(client, "/folder"))
        client.check.assert_not_called()
        denied = ResponseErrorCode("https://example.test/folder", 403, b"Forbidden")
        client.list.side_effect = [denied, RemoteResourceNotFound("folder")]
        with self.assertRaises(ResponseErrorCode):
            main._webdav_find_existing_variant(client, "/folder")


if __name__ == "__main__":
    unittest.main()
