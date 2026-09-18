import json
import sys
import time
from pathlib import Path

import pytest
import control


def test_dpapi_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(control, 'STATE', tmp_path)
    for account, digit in [('A', 'a'), ('B', 'b')]:
        secret = 'sk-test-only-' + digit * 12
        control.save_credentials(account, 'tunnel_' + digit * 32, secret)
        assert control.load_credentials(account)['key'] == secret
        assert secret.encode() not in (tmp_path / f'account-{account}.dpapi').read_bytes()
    assert control.load_credentials('A') != control.load_credentials('B')


def test_clean_environment(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'do-not-inherit')
    monkeypatch.setenv('CONTROL_PLANE_BASE_URL', 'https://wrong.example')
    monkeypatch.setenv('CLOUDFLARED_MANAGED', 'true')
    env = control.clean_environment()
    assert not any(k in env for k in ['OPENAI_API_KEY', 'CONTROL_PLANE_BASE_URL', 'CLOUDFLARED_MANAGED'])


def test_window_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(control, 'STATE', tmp_path)
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    app = control.Window(root)
    root.update_idletasks()
    assert app.account.get() == '账号 A'
    assert str(app.combo['state']) == 'readonly'
    root.destroy()


def test_real_tunnel_process_stops(tmp_path, monkeypatch):
    """Real official executable, fake credentials, loopback-only mock control plane."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import urllib.request
    import win32process
    import win32api
    import win32con
    import subprocess

    class Denied(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"test unauthorized"}}')
        do_POST = do_GET
        def log_message(self, *args):
            pass

    mock = ThreadingHTTPServer(('127.0.0.1', 0), Denied)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    monkeypatch.setattr(control, 'STATE', tmp_path)
    monkeypatch.setattr(control, 'SESSION', tmp_path / 'active.json')
    monkeypatch.setattr(control, 'HEALTH', tmp_path / 'health.url')
    real_create = win32process.CreateProcess
    seen = []

    def local_only(*args):
        args = list(args)
        args[1] += f' --control-plane.base-url http://127.0.0.1:{mock.server_port}'
        result = real_create(*args)
        seen.append(result[2])
        return result

    monkeypatch.setattr(win32process, 'CreateProcess', local_only)
    runtime = control.TunnelProcess()
    try:
        runtime.start({'tunnel_id': 'tunnel_' + 'a' * 32, 'key': 'sk-test-no-real-key'})
        assert runtime.alive()
        for _ in range(60):
            if control.HEALTH.exists() or not runtime.alive():
                break
            time.sleep(.1)
        assert runtime.alive(), (tmp_path / 'tunnel.log').read_text(encoding='utf-8')
        assert control.HEALTH.exists()
        url = control.health_url()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + '/healthz', timeout=3) as response:
            assert response.status == 200
        process = win32api.OpenProcess(win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_INFORMATION, False, seen[0])
        runtime.stop()
        assert not control.SESSION.exists()
        assert win32process.GetExitCodeProcess(process) != 259
        process.Close()
        assert 'sk-test-no-real-key' not in (tmp_path / 'tunnel.log').read_text(encoding='utf-8')
        selected = tmp_path / '第二个项目'
        selected.mkdir()
        runtime.start({'tunnel_id': 'tunnel_' + 'b' * 32, 'key': 'sk-test-other'}, root=selected)
        assert json.loads(control.SESSION.read_text(encoding='utf-8'))['root'] == str(selected)
        assert runtime.alive()
        runtime.stop()
        assert not control.SESSION.exists()
    finally:
        runtime.stop()
        mock.shutdown()
        mock.server_close()


def test_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(control, 'STATE', tmp_path)
    assert control.load_preferences()[0] == 'A'
    control.save_preferences('B', tmp_path)
    assert control.load_preferences() == ('B', str(tmp_path))


def test_root_drive_denied():
    import pytest
    runtime = control.TunnelProcess()
    with pytest.raises(ValueError):
        runtime.start({}, Path(control.BASE.anchor))
