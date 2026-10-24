"""Local socket tests; no account data, browser, or remote OAuth requests."""
import contextlib
import importlib.util
import io
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
import urllib.parse


class PortBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        source = Path(__file__).resolve().parents[1] / "agy_switch.py"
        spec = importlib.util.spec_from_file_location("agy_port_test", source)
        cls.app = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {
            "USERPROFILE": cls.home.name,
            "HOME": cls.home.name,
            "AGY_OAUTH_CONFIG": "",
        }), mock.patch.object(Path, "home", return_value=Path(cls.home.name)):
            spec.loader.exec_module(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.home.cleanup()

    def test_occupied_socket_falls_back_and_retains_binding(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with self.app._bind_http_server(self.app.Handler, [port]) as server:
                actual = server.server_address[1]
                self.assertNotEqual(actual, port)
                self.assertGreater(actual, 0)
                with socket.socket() as competitor:
                    with self.assertRaises(OSError):
                        competitor.bind(("127.0.0.1", actual))

    def denied_factory(self):
        real = self.app.ThreadingHTTPServer
        attempts = []
        bound = []

        def create(address, handler):
            attempts.append(address[1])
            if address[1]:
                raise OSError(10013, "An attempt was made to access a socket forbidden")
            server = real(address, handler)
            bound.append(server.server_address[1])
            return server

        return create, attempts, bound

    def test_default_app_10013_falls_back_and_main_prints_actual_url(self):
        create, attempts, bound = self.denied_factory()
        output = io.StringIO()
        with mock.patch.object(self.app, "ThreadingHTTPServer", side_effect=create), \
                mock.patch.object(self.app, "bootstrap_first_account"), \
                mock.patch.object(ThreadingHTTPServer,
                                  "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(self.app.webbrowser, "open") as browser, \
                mock.patch("sys.argv", ["agy_switch.py", "--no-browser"]), \
                contextlib.redirect_stdout(output):
            self.app.main()
        self.assertEqual(attempts, list(range(8791, 8801)) + [0])
        url = output.getvalue().split("http://127.0.0.1:", 1)[1].split()[0]
        self.assertEqual(int(url), bound[0])
        self.assertGreater(bound[0], 0)
        browser.assert_not_called()

    def test_explicit_occupied_port_does_not_fall_back(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with self.assertRaises(OSError):
                self.app._create_app_server(occupied.getsockname()[1])

    def test_explicit_zero_uses_actual_port(self):
        with self.app._create_app_server(0) as server:
            self.assertGreater(server.server_address[1], 0)

    def test_explicit_available_port_is_exact(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.app._create_app_server(port) as server:
            self.assertEqual(server.server_address[1], port)

    def test_oauth_10013_uses_bound_port_in_both_urls(self):
        create, attempts, _ = self.denied_factory()
        with mock.patch.object(self.app, "ThreadingHTTPServer", side_effect=create), \
                mock.patch.object(self.app, "_login_client", return_value=("test", "id", "secret")), \
                mock.patch.object(self.app.threading, "Timer"), \
                mock.patch.object(self.app.urllib.request, "urlopen") as remote, \
                mock.patch.object(self.app.webbrowser, "open") as browser:
            try:
                auth_url = self.app.oauth_start()
                server = self.app._oauth_cb["server"]
                expected = f"http://localhost:{server.server_address[1]}/callback"
                self.assertEqual(self.app._oauth_cb["redirect_uri"], expected)
                self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
                                 ["redirect_uri"], [expected])
                self.assertEqual(attempts, list(range(8792, 8802)) + [0])
                remote.assert_not_called()
                browser.assert_not_called()
            finally:
                server = self.app._oauth_cb["server"]
                if server:
                    server.shutdown()
                    server.server_close()
                self.app._oauth_cb.update(state=None, redirect_uri=None, server=None)


if __name__ == "__main__":
    unittest.main()
