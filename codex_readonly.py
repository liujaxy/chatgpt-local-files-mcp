"""Bounded, read-only access to the official local Codex app-server protocol."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import stat
import subprocess
import threading
import time

from reader import ReadError
from workflow import VERSION

MAX_RPC_BYTES = 32 * 1024 * 1024
THREAD_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def parse_thread_id(value):
    value = value.strip()
    if value.startswith('codex://threads/'):
        value = value[len('codex://threads/'):]
    if not THREAD_ID.fullmatch(value):
        raise ReadError('请输入 Codex 聊天链接或完整聊天 ID。')
    return value


def no_links(path, anchor):
    """Check the anchor and every descendant, before opened-handle checks."""
    try:
        parts = path.relative_to(anchor).parts
        if any(part == '..' or part.endswith((' ', '.')) or ':' in part for part in parts):
            raise ReadError('Ambiguous Codex resource path')
        cursor = anchor
        for part in (None, *parts):
            if part is not None:
                cursor /= part
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ReadError('Codex resource links and reparse points are not allowed')
    except (OSError, ValueError):
        raise ReadError('Codex resource unavailable or outside allowed scope') from None


def discover_codex():
    local_appdata = os.environ.get('LOCALAPPDATA')
    if not local_appdata or not Path(local_appdata).is_absolute():
        raise ReadError('未找到 Windows 本机应用目录。')
    base = Path(local_appdata) / 'OpenAI/Codex/bin'
    candidates = []
    if base.is_dir():
        for directory in base.iterdir():
            if not re.fullmatch('[0-9a-f]{16}', directory.name):
                continue
            executable = directory / 'codex.exe'
            try:
                no_links(executable, base)
                candidates.append((executable.stat().st_mtime_ns, executable))
            except (ReadError, OSError):
                continue
    if not candidates:
        raise ReadError('未找到 Codex 桌面版内置程序。请先安装或启动 Codex。')
    return max(candidates, key=lambda item: item[0])[1]


def native_environment(home):
    names = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
             'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'OS'}
    env = {key: value for key, value in os.environ.items() if key.upper() in names}
    env['CODEX_HOME'] = str(home)
    return env


class CodexReadOnly:
    # Never forward arbitrary method names, tool calls or turn/start/resume.
    METHODS = {'thread/read', 'skills/list'}

    def __init__(self, executable, home, cwd, timeout=15):
        self.executable = Path(executable)
        self.home = Path(home)
        self.cwd = Path(cwd)
        self.timeout = timeout

    def call(self, method, params):
        if method not in self.METHODS:
            raise ReadError('Only stored-chat and skill-metadata reads are allowed')
        if method == 'thread/read':
            if set(params) != {'threadId', 'includeTurns'} or type(params['includeTurns']) is not bool:
                raise ReadError('Invalid stored-chat read parameters')
            parse_thread_id(params['threadId'])
        elif params != {'cwds': [str(self.cwd)], 'forceReload': True}:
            raise ReadError('Skill metadata must use the selected project')
        command = [str(self.executable), 'app-server', '--listen', 'stdio://']
        return self._exchange(command, method, params)

    def _exchange(self, command, method, params):
        messages = queue.Queue(maxsize=64)
        process = None
        job = None
        deadline = time.monotonic() + self.timeout
        done = threading.Event()
        reader_thread = None

        def receive():
            consumed = 0
            try:
                while not done.is_set():
                    line = process.stdout.readline(MAX_RPC_BYTES + 1)
                    consumed += len(line)
                    if not line or consumed > MAX_RPC_BYTES or not line.endswith(b'\n'):
                        messages.put_nowait(None)
                        return
                    messages.put_nowait(json.loads(line))
            except (OSError, ValueError, queue.Full):
                try:
                    messages.put_nowait(None)
                except queue.Full:
                    pass

        def send(value):
            process.stdin.write((json.dumps(value) + '\n').encode('utf-8'))
            process.stdin.flush()

        def wait_for(identifier):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReadError('Codex metadata read timed out; retry locally')
                try:
                    message = messages.get(timeout=remaining)
                except queue.Empty:
                    raise ReadError('Codex metadata read timed out; retry locally') from None
                if not isinstance(message, dict):
                    raise ReadError('Codex metadata unavailable or exceeds 32 MiB')
                if 'method' in message:
                    if 'id' in message:
                        send({'id': message['id'], 'error': {'code': -32601, 'message': 'Read-only client'}})
                    continue
                if message.get('id') == identifier:
                    if 'error' in message or not isinstance(message.get('result'), dict):
                        # Native error text can contain local settings/content; never forward it.
                        raise ReadError('Codex metadata read failed; check the chat ID and native version')
                    return message['result']

        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=str(self.cwd), env=native_environment(self.home),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if os.name == 'nt':
                import win32job
                job = win32job.CreateJobObject(None, '')
                info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
                win32job.AssignProcessToJobObject(job, int(process._handle))
            reader_thread = threading.Thread(target=receive, daemon=True)
            reader_thread.start()
            send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {
                'name': 'local_project_reader', 'title': 'Local Project Reader', 'version': VERSION}}})
            wait_for(1)
            send({'method': 'initialized', 'params': {}})
            send({'id': 2, 'method': method, 'params': params})
            return wait_for(2)
        except (OSError, ValueError):
            raise ReadError('Local Codex program unavailable') from None
        except Exception as exc:
            if isinstance(exc, ReadError):
                raise
            raise ReadError('Local Codex metadata process could not be initialized') from None
        finally:
            done.set()
            if job:
                win32job.TerminateJobObject(job, 0)
                job.Close()
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if reader_thread:
                    reader_thread.join(timeout=2)
                process.stdin.close()
                process.stdout.close()

    def thread(self, thread_id, include_turns=True):
        result = self.call('thread/read', {'threadId': thread_id, 'includeTurns': include_turns})
        thread = result.get('thread', {})
        try:
            correct_project = Path(thread.get('cwd', '')).resolve(strict=True) == self.cwd.resolve(strict=True)
        except (OSError, ValueError):
            correct_project = False
        if thread.get('id') != thread_id or not thread.get('cwd') or not correct_project:
            raise ReadError('指定聊天不属于当前项目；请核对目录和聊天链接。')
        return thread

    def skills(self):
        result = self.call('skills/list', {'cwds': [str(self.cwd)], 'forceReload': True})
        entries = result.get('data', [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                matching = Path(entry.get('cwd', '')).resolve(strict=True) == self.cwd.resolve(strict=True)
            except (ValueError, OSError):
                matching = False
            if entry.get('cwd') and matching:
                return entry.get('skills', []), bool(entry.get('errors'))
        raise ReadError('Codex did not return skill metadata for the selected project')
