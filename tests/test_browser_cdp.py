"""Exercise the real Node transport against a local fake CDP server, never Chrome."""
import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import threading
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts/browser_cdp.mjs'
NODE = shutil.which('node')
NODE_OK = bool(NODE and int(subprocess.check_output([NODE, '--version'], text=True).strip().lstrip('v').split('.')[0]) >= 22)
ORIGIN = 'https://example.feishu.cn'
BASE_URL = ORIGIN + '/base/BaseFixture?table=tblFixture'


def receive_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError()
        data.extend(part)
    return bytes(data)


def receive_frame(sock):
    header = receive_exact(sock, 2)
    opcode = header[0] & 15
    size = header[1] & 127
    if size == 126:
        size = struct.unpack('!H', receive_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack('!Q', receive_exact(sock, 8))[0]
    if size > 2 * 1024 * 1024:
        raise ValueError('fixture frame limit')
    mask = receive_exact(sock, 4) if header[1] & 128 else b''
    payload = receive_exact(sock, size)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def send_frame(sock, payload, opcode=1):
    size = len(payload)
    if size < 126:
        header = bytes([128 | opcode, size])
    elif size < 65536:
        header = bytes([128 | opcode, 126]) + struct.pack('!H', size)
    else:
        header = bytes([128 | opcode, 127]) + struct.pack('!Q', size)
    sock.sendall(header + payload)


class FakeCDP:
    def __init__(self, mode='ok'):
        self.mode = mode
        self.calls = []
        self.http_paths = []
        self.closed = []
        self.marker = None
        self.evaluate_started = threading.Event()
        self.document_ready = threading.Event()
        self.evaluated_before_document = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def do_GET(self):
                fixture.http_paths.append(self.path)
                if self.path == '/json/version':
                    if fixture.mode == 'metadata_redirect':
                        self.send_response(302)
                        self.send_header('Location', 'https://example.invalid/not-allowed')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    endpoint = f'ws://127.0.0.1:{self.server.server_port}/devtools/browser/fixture'
                    if fixture.mode == 'foreign_websocket':
                        endpoint = 'ws://example.invalid:9222/devtools/browser/fixture'
                    raw = json.dumps({'webSocketDebuggerUrl': endpoint}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path != '/devtools/browser/fixture':
                    self.send_error(404)
                    return
                key = self.headers['Sec-WebSocket-Key']
                accept = base64.b64encode(hashlib.sha1(
                    (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.send_response(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', accept)
                self.end_headers()
                self.connection.settimeout(8)
                try:
                    while True:
                        opcode, raw = receive_frame(self.connection)
                        if opcode == 8:
                            if fixture.mode == 'unanswered_close':
                                while self.connection.recv(1024):
                                    pass
                                return
                            send_frame(self.connection, b'', 8)
                            return
                        if opcode == 9:
                            send_frame(self.connection, raw, 10)
                            continue
                        if opcode != 1:
                            raise ValueError('unexpected fixture frame')
                        fixture.handle(self.connection, json.loads(raw))
                except (EOFError, OSError, ValueError):
                    return
                finally:
                    self.close_connection = True

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    @property
    def endpoint(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def send(self, sock, value):
        send_frame(sock, json.dumps(value).encode())

    def event(self, sock, method, params, session='owned-session'):
        self.send(sock, {'method': method, 'params': params, 'sessionId': session})

    def handle(self, sock, request):
        self.calls.append(request)
        method, params = request['method'], request.get('params', {})
        result = {}
        if method == 'Target.createTarget':
            self.marker = params['url']
            if self.mode == 'lost_create_reply':
                return
            result = {'targetId': 'owned-tab'}
        elif method == 'Target.attachToTarget':
            result = {'sessionId': 'owned-session'}
        elif method == 'Target.getTargets':
            result = {'targetInfos': [
                {'targetId': 'user-tab', 'type': 'page', 'url': BASE_URL},
                {'targetId': 'owned-tab', 'type': 'page', 'url': self.marker},
            ]}
        elif method == 'Page.getFrameTree':
            result = {'frameTree': {'frame': {'id': 'owned-frame', 'url': self.marker}}}
        elif method == 'Page.navigate':
            if self.mode in ('foreign_navigation', 'wrong_path'):
                url = ('https://outside.invalid/' if self.mode == 'foreign_navigation'
                       else ORIGIN + '/login')
                self.event(sock, 'Fetch.requestPaused', {
                    'requestId': 'blocked-request', 'frameId': 'owned-frame',
                    'resourceType': 'Document', 'request': {'url': url}})
                return
            if self.mode == 'unrelated_event':
                self.event(sock, 'Page.frameNavigated', {'frame': {
                    'id': 'owned-frame', 'url': 'https://outside.invalid/'}}, 'user-session')
            if self.mode == 'main_same_document_redirect':
                self.event(sock, 'Page.navigatedWithinDocument', {
                    'frameId': 'owned-frame', 'url': ORIGIN + '/login'})
                return
            if self.mode == 'iframe_navigation':
                self.event(sock, 'Page.frameNavigated', {'frame': {
                    'id': 'embedded-frame', 'parentId': 'owned-frame', 'url': 'https://embedded.invalid/'}})
                self.event(sock, 'Page.navigatedWithinDocument', {
                    'frameId': 'embedded-frame', 'url': 'https://embedded.invalid/#section'})
                self.event(sock, 'Fetch.requestPaused', {
                    'requestId': 'embedded-request', 'frameId': 'embedded-frame',
                    'resourceType': 'Document', 'request': {'url': 'https://embedded.invalid/'}})
            if self.mode == 'navigate_error':
                result = {'errorText': 'fixture error containing private text'}
            else:
                self.event(sock, 'Fetch.requestPaused', {
                    'requestId': 'owned-request', 'frameId': 'owned-frame',
                    'resourceType': 'Document', 'request': {'url': params['url']}})
                self.event(sock, 'Page.frameNavigated', {'frame': {'id': 'owned-frame', 'url': params['url']}})
                result = {'frameId': 'owned-frame', 'loaderId': 'owned-loader'}
                if self.mode != 'late_document':
                    self.document_ready.set()
                    self.event(sock, 'Page.lifecycleEvent', {'name': 'DOMContentLoaded', 'loaderId': 'owned-loader'})
        elif method == 'Runtime.evaluate':
            self.evaluated_before_document = not self.document_ready.is_set()
            self.evaluate_started.set()
            if self.mode in ('timeout', 'interrupt'):
                return
            if self.mode == 'exception':
                result = {'exceptionDetails': {'text': 'private fixture data'}}
            elif self.mode == 'guard_redirect':
                result = {'result': {'value': {'guard': False}}}
            elif self.mode == 'oversize':
                result = {'result': {'value': {'guard': True, 'value': {'text': 'x' * 4000}}}}
            elif self.mode == 'disconnect':
                sock.shutdown(socket.SHUT_RDWR)
                return
            else:
                result = {'result': {'value': {'guard': True, 'value': {
                    'url': BASE_URL, 'title': 'Fixture', 'ready_state': 'complete'}}}}
        elif method == 'Target.closeTarget':
            self.closed.append(params['targetId'])
            result = {'success': self.mode != 'close_failed'}
        self.send(sock, {'id': request['id'], 'result': result})
        if method == 'Page.navigate' and self.mode == 'late_document':
            def loaded():
                self.document_ready.set()
                self.event(sock, 'Page.lifecycleEvent', {'name': 'DOMContentLoaded', 'loaderId': 'owned-loader'})
            timer = threading.Timer(0.15, loaded)
            timer.daemon = True
            timer.start()


@unittest.skipUnless(NODE_OK, 'Node.js 22+ is needed only for browser transport tests')
class BrowserCdpTests(unittest.TestCase):
    def request(self, fixture, **changes):
        result = {'cdp_endpoint': fixture.endpoint, 'origin': ORIGIN, 'url': BASE_URL,
                  'timeout_ms': 1500, 'max_result_bytes': 1024}
        result.update(changes)
        return result

    def run_probe(self, fixture, **changes):
        output = subprocess.run([NODE, str(SCRIPT)], input=json.dumps(self.request(fixture, **changes)),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(output.stderr, '')
        return output, json.loads(output.stdout)

    def assert_only_owned(self, fixture):
        self.assertEqual(fixture.closed, ['owned-tab'])
        forbidden = ('Browser.close', 'Target.disposeBrowserContext', 'Target.setAutoAttach',
                     'Network.getCookies', 'Storage.getCookies', 'Network.enable')
        self.assertFalse(any(c['method'] in forbidden for c in fixture.calls))
        for call in fixture.calls:
            if call['method'] == 'Target.attachToTarget':
                self.assertEqual(call['params']['targetId'], 'owned-tab')
            if call['method'].startswith(('Page.', 'Runtime.', 'Fetch.')):
                self.assertEqual(call['sessionId'], 'owned-session')

    def test_probe_uses_only_owned_tab_and_leaves_browser_running(self):
        with FakeCDP() as fixture:
            output, data = self.run_probe(fixture)
            self.assertEqual(output.returncode, 0)
            self.assertTrue(data['ok'])
            self.assertEqual(data['data']['title'], 'Fixture')
            self.assertEqual(fixture.http_paths, ['/json/version', '/devtools/browser/fixture'])
            self.assertTrue(fixture.marker.startswith('about:blank#lark-dev-workflow-'))
            self.assert_only_owned(fixture)

    def test_rejects_nonlocal_endpoints_before_any_connection(self):
        bad = ('http://outside.invalid:9222', 'http://127.0.0.1:9222/extra',
               'http://user:secret@127.0.0.1:9222', 'http://127.0.0.1:9222?x=1',
               'https://127.0.0.1:9222', 'http://127.0.0.2:9222')
        with FakeCDP() as fixture:
            for endpoint in bad:
                with self.subTest(endpoint=endpoint):
                    output, data = self.run_probe(fixture, cdp_endpoint=endpoint)
                    self.assertNotEqual(output.returncode, 0)
                    self.assertEqual(data['error']['code'], 'browser_invalid_config')
            self.assertEqual(fixture.http_paths, [])

    def test_rejects_foreign_or_non_https_page_before_any_connection(self):
        with FakeCDP() as fixture:
            for url in ('https://other.feishu.cn/base/BaseFixture', 'http://example.feishu.cn/base/BaseFixture',
                        'https://user:secret@example.feishu.cn/base/BaseFixture'):
                with self.subTest(url=url):
                    _, data = self.run_probe(fixture, url=url)
                    self.assertEqual(data['error']['code'], 'browser_navigation_blocked')
            self.assertEqual(fixture.http_paths, [])

    def test_does_not_follow_cdp_metadata_redirect_or_foreign_websocket(self):
        for mode in ('metadata_redirect', 'foreign_websocket'):
            with self.subTest(mode=mode), FakeCDP(mode) as fixture:
                _, data = self.run_probe(fixture)
                self.assertEqual(data['error']['code'], 'browser_connect_failed')
                self.assertEqual(fixture.http_paths, ['/json/version'])

    def test_blocks_document_redirects_and_closes_only_owned_tab(self):
        for mode in ('foreign_navigation', 'wrong_path'):
            with self.subTest(mode=mode), FakeCDP(mode) as fixture:
                _, data = self.run_probe(fixture)
                self.assertEqual(data['error']['code'], 'browser_navigation_blocked')
                failures = [c for c in fixture.calls if c['method'] == 'Fetch.failRequest']
                self.assertEqual([c['params']['requestId'] for c in failures], ['blocked-request'])
                self.assert_only_owned(fixture)

    def test_ignores_events_from_other_sessions(self):
        with FakeCDP('unrelated_event') as fixture:
            _, data = self.run_probe(fixture)
            self.assertTrue(data['ok'])
            self.assert_only_owned(fixture)

    def test_embedded_documents_keep_their_normal_navigation(self):
        with FakeCDP('iframe_navigation') as fixture:
            _, data = self.run_probe(fixture)
            self.assertTrue(data['ok'])
            continued = [c['params']['requestId'] for c in fixture.calls if c['method'] == 'Fetch.continueRequest']
            self.assertIn('embedded-request', continued)
            self.assertFalse(any(c['method'] == 'Fetch.failRequest' for c in fixture.calls))
            self.assert_only_owned(fixture)

    def test_main_frame_same_document_navigation_still_stops_the_read(self):
        with FakeCDP('main_same_document_redirect') as fixture:
            _, data = self.run_probe(fixture)
            self.assertEqual(data['error']['code'], 'browser_navigation_blocked')
            self.assert_only_owned(fixture)

    def test_waits_for_the_new_document_before_reading(self):
        with FakeCDP('late_document') as fixture:
            _, data = self.run_probe(fixture)
            self.assertTrue(data['ok'])
            self.assertFalse(fixture.evaluated_before_document)
            self.assert_only_owned(fixture)

    def test_live_context_guard_rejects_an_unexpected_document(self):
        with FakeCDP('guard_redirect') as fixture:
            _, data = self.run_probe(fixture)
            self.assertEqual(data['error']['code'], 'browser_navigation_blocked')
            evaluation = next(c for c in fixture.calls if c['method'] == 'Runtime.evaluate')
            self.assertIn('location.pathname', evaluation['params']['expression'])
            self.assertIn('location.origin', evaluation['params']['expression'])
            self.assert_only_owned(fixture)

    def test_errors_timeouts_and_oversize_results_still_close_own_tab(self):
        modes = {'navigate_error': 'browser_navigation_failed', 'exception': 'browser_page_error',
                 'timeout': 'browser_timeout', 'oversize': 'browser_response_too_large'}
        for mode, code in modes.items():
            with self.subTest(mode=mode), FakeCDP(mode) as fixture:
                output, data = self.run_probe(fixture, timeout_ms=300)
                self.assertEqual(output.returncode, 1)
                self.assertEqual(data['error']['code'], code)
                self.assertNotIn('private', output.stdout)
                self.assert_only_owned(fixture)

    def test_lost_create_reply_recovers_only_its_unique_marker(self):
        with FakeCDP('lost_create_reply') as fixture:
            _, data = self.run_probe(fixture, timeout_ms=300)
            self.assertEqual(data['error']['code'], 'browser_timeout')
            self.assert_only_owned(fixture)

    def test_cleanup_failure_is_never_reported_as_success(self):
        for mode in ('close_failed', 'disconnect'):
            with self.subTest(mode=mode), FakeCDP(mode) as fixture:
                _, data = self.run_probe(fixture)
                self.assertFalse(data['ok'])
                self.assertEqual(data['error']['code'], 'browser_cleanup_failed')

    def test_cli_exits_when_peer_does_not_finish_websocket_close_handshake(self):
        with FakeCDP('unanswered_close') as fixture:
            started = time.monotonic()
            output, data = self.run_probe(fixture)
            self.assertEqual(output.returncode, 0)
            self.assertTrue(data['ok'])
            self.assertLess(time.monotonic() - started, 3)
            self.assert_only_owned(fixture)

    def test_rejects_javascript_from_stdin(self):
        with FakeCDP() as fixture:
            _, data = self.run_probe(fixture, expression='document.cookie')
            self.assertEqual(data['error']['code'], 'browser_invalid_config')
            self.assertEqual(fixture.http_paths, [])

    def test_sigterm_closes_only_owned_tab_before_exiting(self):
        with FakeCDP('interrupt') as fixture:
            process = subprocess.Popen([NODE, str(SCRIPT)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            process.stdin.write(json.dumps(self.request(fixture, timeout_ms=5000)))
            process.stdin.close()
            process.stdin = None
            self.assertTrue(fixture.evaluate_started.wait(3))
            process.terminate()
            stdout, stderr = process.communicate(timeout=6)
            self.assertEqual(stderr, '')
            self.assertEqual(json.loads(stdout)['error']['code'], 'browser_interrupted')
            self.assert_only_owned(fixture)


if __name__ == '__main__':
    unittest.main()
