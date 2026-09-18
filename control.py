"""Windows controller. A single window owns a kill-on-close process job."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import win32api
import win32con
import win32crypt
import win32event
import win32job
import win32process
from workflow import VERSION

BASE = Path(__file__).resolve().parent
STATE = BASE / 'state'
TUNNEL = BASE / 'vendor/tunnel/tunnel-client.exe'
PYTHON = BASE / '.venv/Scripts/python.exe'
SAMPLE = BASE / 'sample-project'
SESSION = STATE / 'active.json'
HEALTH = STATE / 'health.url'
ACCOUNT_LABELS = {'A': '账号 A', 'B': '账号 B'}
ACCOUNT_IDS = {label: account for account, label in ACCOUNT_LABELS.items()}


def load_preferences():
    try:
        value = json.loads((STATE / 'preferences.json').read_text(encoding='utf-8'))
        account = value.get('account', 'A')
        root = Path(value.get('root', str(SAMPLE)))
        return (account if account in {'A', 'B'} else 'A', str(root if root.is_dir() else SAMPLE))
    except (OSError, ValueError, TypeError):
        return 'A', str(SAMPLE)


def save_preferences(account, root):
    STATE.mkdir(exist_ok=True)
    target = STATE / 'preferences.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'account': account, 'root': str(root)}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(target)


def save_credentials(account, tunnel_id, key):
    if account not in {'A', 'B'} or not re.fullmatch(r'tunnel_[0-9a-f]{32}', tunnel_id):
        raise ValueError('隧道 ID 必须是 tunnel_ 后接 32 位小写十六进制字符。')
    if not key.startswith('sk-') or any(c.isspace() for c in key):
        raise ValueError('请输入有效的运行 API 密钥；不要使用管理员密钥。')
    STATE.mkdir(exist_ok=True)
    data = json.dumps({'tunnel_id': tunnel_id, 'key': key}).encode()
    blob = win32crypt.CryptProtectData(data, 'Project Reader', None, None, None, 1)
    path = STATE / f'account-{account}.dpapi'
    temp = path.with_suffix('.tmp')
    temp.write_bytes(blob)
    temp.replace(path)


def load_credentials(account):
    if account not in {'A', 'B'}:
        raise ValueError('Unknown account')
    blob = (STATE / f'account-{account}.dpapi').read_bytes()
    return json.loads(win32crypt.CryptUnprotectData(blob, None, None, None, 1)[1])


def clean_environment():
    # Explicit allowlist prevents inherited OpenAI/Cloudflare/MCP settings redirecting this connection.
    names = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
             'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES',
             'PROGRAMFILES(X86)', 'NUMBER_OF_PROCESSORS', 'OS'}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


class TunnelProcess:
    def __init__(self):
        self.job = None
        self.process = None

    def alive(self):
        return self.process is not None and win32process.GetExitCodeProcess(self.process) == 259

    def start(self, credentials, root=None):
        if self.alive():
            raise RuntimeError('连接已运行，请先停止。')
        root = Path(root or SAMPLE).resolve(strict=True)
        if not root.is_dir() or root.parent == root:
            raise ValueError('请选择一个项目文件夹，不能开放整个磁盘。')
        self.stop()
        STATE.mkdir(exist_ok=True)
        session_temp = SESSION.with_suffix('.tmp')
        session_temp.write_text(json.dumps({'root': str(root), 'access_id': secrets.token_hex(16)}), encoding='utf-8')
        session_temp.replace(SESSION)
        HEALTH.unlink(missing_ok=True)
        # Forward slash paths are understood by the tunnel client's command parser on Windows.
        command = ' '.join('"' + str(p).replace('\\', '/') + '"' for p in [PYTHON, BASE / 'server.py'])
        command += ' --session "' + SESSION.as_posix() + '"'
        args = [str(TUNNEL), 'run', '--control-plane.tunnel-id', credentials['tunnel_id'],
                '--control-plane.api-key', 'env:CONTROL_PLANE_API_KEY',
                '--mcp.command', command, '--mcp.stdio-send-initialized-notification',
                '--health.listen-addr', '127.0.0.1:0', '--health.url-file', str(HEALTH),
                '--log.level', 'warn', '--log.file', str(STATE / 'tunnel.log')]
        env = clean_environment()
        env['CONTROL_PLANE_API_KEY'] = credentials['key']
        self.job = win32job.CreateJobObject(None, '')
        info = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, info)
        thread = None
        try:
            self.process, thread, pid, tid = win32process.CreateProcess(
                None, subprocess.list2cmdline(args), None, None, False,
                win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
                env, str(BASE), win32process.STARTUPINFO())
            win32job.AssignProcessToJobObject(self.job, self.process)
            win32process.ResumeThread(thread)
        except Exception:
            if self.process:
                win32process.TerminateProcess(self.process, 1)
            self.stop()
            raise
        finally:
            if thread:
                thread.Close()
            env.pop('CONTROL_PLANE_API_KEY', None)

    def stop(self):
        SESSION.unlink(missing_ok=True)
        if self.job:
            win32job.TerminateJobObject(self.job, 0)
            self.job.Close()
            self.job = None
        if self.process:
            win32event.WaitForSingleObject(self.process, 5000)
            self.process.Close()
            self.process = None
        HEALTH.unlink(missing_ok=True)


def health_url():
    value = HEALTH.read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+', value):
        raise ValueError('Unexpected health endpoint')
    return value


class Window:
    def __init__(self, root):
        self.root = root
        self.runtime = TunnelProcess()
        account, project = load_preferences()
        self.account = tk.StringVar(value=ACCOUNT_LABELS[account])
        self.project = tk.StringVar(value=project)
        self.status = tk.StringVar(value='未启动。选择与 Chat 相同的账号，以及本次项目文件夹。')
        self.diagnostic_generation = 0
        self.closing = False
        root.title('项目资料只读 MCP · ' + VERSION)
        root.geometry('800x690')
        root.minsize(760, 650)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='项目资料只读 · Markdown 与图片', font=('Microsoft YaHei UI', 17)).pack(anchor='w')
        ttk.Label(frame, text='一次开放一个项目及其子目录。切换账号或项目时先停止，并在 Chat 中新建聊天。', wraplength=730).pack(anchor='w', pady=10)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=6)
        ttk.Label(row, text='当前账号：').pack(side='left')
        self.combo = ttk.Combobox(row, textvariable=self.account, values=list(ACCOUNT_LABELS.values()), width=16, state='readonly')
        self.combo.pack(side='left')
        self.save_btn = ttk.Button(row, text='录入 / 更新该账号凭据', command=self.credentials)
        self.save_btn.pack(side='left', padx=10)
        ttk.Label(frame, text='开放目录：').pack(anchor='w', pady=(8, 0))
        ttk.Label(frame, textvariable=self.project, wraplength=730).pack(anchor='w', pady=4)
        project_buttons = ttk.Frame(frame)
        project_buttons.pack(fill='x')
        self.project_btn = ttk.Button(project_buttons, text='选择项目文件夹', command=self.choose_project)
        self.project_btn.pack(side='left')
        self.sample_btn = ttk.Button(project_buttons, text='使用测试项目', command=lambda: self.project.set(str(SAMPLE)))
        self.sample_btn.pack(side='left', padx=8)
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=10)
        self.start_btn = ttk.Button(buttons, text='启动连接', command=self.start)
        self.start_btn.pack(side='left')
        ttk.Button(buttons, text='停止', command=self.stop).pack(side='left', padx=8)
        ttk.Button(buttons, text='诊断连接', command=self.diagnose).pack(side='left')
        ttk.Button(buttons, text='打开隧道状态页', command=self.open_health).pack(side='left', padx=8)
        ttk.Label(frame, textvariable=self.status, wraplength=690, foreground='#164E63').pack(anchor='w', pady=10)
        links = ttk.Frame(frame)
        links.pack(fill='x', pady=8)
        ttk.Button(links, text='开发者平台：隧道', command=lambda: webbrowser.open('https://platform.openai.com/settings/organization/tunnels')).pack(side='left')
        ttk.Button(links, text='开发者平台：运行密钥', command=lambda: webbrowser.open('https://platform.openai.com/settings/organization/api-keys')).pack(side='left', padx=8)
        ttk.Button(links, text='打开说明', command=lambda: os.startfile(str(BASE / '使用说明.md'))).pack(side='left')
        ttk.Label(frame, text='在网页聊天中选择模型和本插件，发送以下消息：').pack(anchor='w', pady=(15, 5))
        self.prompt = tk.Text(frame, height=5, wrap='word', font=('Microsoft YaHei UI', 10))
        self.prompt.insert('1.0', '请结合当前项目的资料分析以下问题，并注明实际读取的来源。我的问题是：')
        self.prompt.pack(fill='x')
        ttk.Button(frame, text='复制任务模板', command=self.copy_prompt).pack(anchor='w', pady=6)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(1500, self.tick)

    def copy_prompt(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.prompt.get('1.0', 'end-1c'))

    def choose_project(self):
        if self.runtime.alive():
            return
        path = filedialog.askdirectory(parent=self.root, title='选择 Codex 项目对应的本地文件夹',
                                       initialdir=self.project.get(), mustexist=True)
        if path:
            self.project.set(str(Path(path).resolve()))

    def credentials(self):
        if self.runtime.alive():
            return
        account = ACCOUNT_IDS[self.account.get()]
        dialog = tk.Toplevel(self.root)
        dialog.title('账号 ' + ACCOUNT_LABELS[account] + ' · 凭据仅保存在本机')
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='隧道 ID（tunnel_…）').pack(anchor='w')
        tunnel = ttk.Entry(body, width=62)
        tunnel.pack(pady=5)
        try:
            stored = load_credentials(account)
            tunnel.insert(0, stored['tunnel_id'])
            stored.clear()
        except Exception:
            pass
        ttk.Label(body, text='运行 API 密钥（不使用管理员密钥；保存后不回显）').pack(anchor='w')
        key = ttk.Entry(body, width=62, show='●')
        key.pack(pady=5)
        ttk.Label(body, text='使用 Windows 当前用户加密保存。无需把密钥发给 Codex。').pack(anchor='w', pady=8)

        def save():
            try:
                save_credentials(account, tunnel.get().strip(), key.get().strip())
                key.delete(0, 'end')
                self.status.set(f'账号 {ACCOUNT_LABELS[account]} 已加密保存；尚未验证远程权限。')
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror('保存失败', str(exc), parent=dialog)
        ttk.Button(body, text='加密保存', command=save).pack(anchor='e')

    def start(self):
        credentials = None
        try:
            account = ACCOUNT_IDS[self.account.get()]
            credentials = load_credentials(account)
            root = Path(self.project.get()).resolve(strict=True)
            if not messagebox.askokcancel('确认开放的项目',
                    f'账号：{self.account.get()}\n项目目录：{root}\n\n将允许该连接按需读取目录内的 Markdown 和图片。', parent=self.root):
                return
            self.runtime.start(credentials, root)
            save_preferences(account, root)
            self.combo.configure(state='disabled')
            self.save_btn.configure(state='disabled')
            self.start_btn.configure(state='disabled')
            self.project_btn.configure(state='disabled')
            self.sample_btn.configure(state='disabled')
            self.status.set(f'账号 {self.account.get()} 本地进程已启动；请点击诊断。启动不等于远程已连接。')
        except FileNotFoundError:
            messagebox.showerror('文件不可用', '请确认项目目录存在，并已录入该账号的隧道 ID 和运行密钥。')
        except Exception:
            self.stop()
            messagebox.showerror('启动失败', '未能启动连接。请让 Codex 检查本地配置；不要发送密钥。')
        finally:
            if credentials:
                credentials.clear()

    def stop(self):
        self.diagnostic_generation += 1
        self.runtime.stop()
        self.combo.configure(state='readonly')
        self.save_btn.configure(state='normal')
        self.start_btn.configure(state='normal')
        self.project_btn.configure(state='normal')
        self.sample_btn.configure(state='normal')
        self.status.set('已停止；项目访问已撤销。可以切换账号或项目。')

    def diagnose(self):
        if not self.runtime.alive():
            self.status.set('没有运行中的隧道。先保存凭据并启动。')
            return
        self.status.set('正在检查本地健康状态与就绪状态…')
        generation = self.diagnostic_generation
        def work():
            try:
                base = health_url()
                # Never use system HTTP proxy for loopback diagnostics.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                states = []
                for endpoint in ['/healthz', '/readyz']:
                    try:
                        with opener.open(base + endpoint, timeout=4) as response:
                            states.append(response.status == 200)
                    except Exception:
                        states.append(False)
                text = f'本地健康：{"通过" if states[0] else "未通过"}；就绪检查：{"通过" if states[1] else "未通过"}。仍需网页模型实际调用验收。'
            except Exception:
                text = '尚未找到本地健康端点。可能仍在启动，或连接 / 凭据有误。'
            def publish():
                if not self.closing and generation == self.diagnostic_generation:
                    self.status.set(text)
            if not self.closing:
                try:
                    self.root.after(0, publish)
                except RuntimeError:
                    pass
        threading.Thread(target=work, daemon=True).start()

    def open_health(self):
        try:
            webbrowser.open(health_url() + '/ui')
        except Exception:
            messagebox.showinfo('尚未启动', '先启动测试连接，然后再打开状态页。')

    def tick(self):
        if self.runtime.process and not self.runtime.alive():
            self.stop()
            self.status.set('隧道进程已退出，项目访问已撤销。请检查配置或日志。')
        self.root.after(1500, self.tick)

    def close(self):
        self.closing = True
        self.runtime.stop()
        self.root.destroy()


def main():
    STATE.mkdir(exist_ok=True)
    # One controller per Windows logon session; OS releases it after a crash.
    mutex = win32event.CreateMutex(None, False, r'Local\ProjectReaderMCPPilot')
    if win32api.GetLastError() == 183:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo('已经打开', '已有控制窗口在运行，请使用现有窗口。')
        root.destroy()
        mutex.Close()
        return
    SESSION.unlink(missing_ok=True)
    root = tk.Tk()
    window = Window(root)
    try:
        root.mainloop()
    finally:
        window.runtime.stop()
        mutex.Close()


if __name__ == '__main__':
    main()
