import json
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from codex_readonly import CodexReadOnly, parse_thread_id, no_links, discover_codex
from context_skills import ExtendedReader, SkillLibrary, visible_context
from reader import ReadError

THREAD = '11111111-2222-3333-4444-555555555555'


class FakeNative(CodexReadOnly):
    def __init__(self, home, root):
        super().__init__('unused', home, root)
        self.entries = []
        self.calls = []
        self.data = {'id': THREAD, 'cwd': str(root), 'turns': [{'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': '请按当前方案写论文。'}]},
            {'type': 'agentMessage', 'text': '已选择讨论部分。', 'phase': 'final_answer'}]}]}

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/read':
            return {'thread': self.data}
        assert method == 'skills/list'
        assert params == {'cwds': [str(self.cwd)], 'forceReload': True}
        return {'data': [{'cwd': str(self.cwd), 'skills': self.entries, 'errors': []}]}


@pytest.fixture
def shared(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    home = tmp_path / 'fake-codex'
    home.mkdir()
    extensions = {'codex_executable': 'unused', 'codex_home': str(home),
                  'skills_enabled': True, 'thread_id': THREAD}
    active = tmp_path / 'active.json'
    active.write_text(json.dumps({'root': str(root), 'access_id': 'valid', 'extensions': extensions}))
    native = FakeNative(home, root)
    reader = ExtendedReader(root, 'valid', active, tmp_path / 'audit.jsonl', extensions, native)
    return reader


def add_skill(reader, package='materials-manuscript-workflow', name='paper-writing', implicit=True):
    if package == 'personal':
        path = Path(reader.extensions['codex_home']) / 'skills' / name
    else:
        base = Path(reader.extensions['codex_home']) / 'plugins/cache/jaxy-materials-manuscript' / package / '1.0.1'
        (base / '.codex-plugin').mkdir(parents=True, exist_ok=True)
        (base / '.codex-plugin/plugin.json').write_text(json.dumps({'name': package, 'version': '1.0.1'}))
        path = base / 'skills' / name
    (path / 'references/shared').mkdir(parents=True)
    (path / 'agents').mkdir()
    (path / 'SKILL.md').write_text('---\nname: writing\n---\nRead [rules](references/shared/rules.md)\n'
                                '[outside](../../private.md)\n', encoding='utf-8')
    (path / 'references/shared/rules.md').write_text('学术规则：保持证据边界。', encoding='utf-8')
    (path / 'agents/openai.yaml').write_text('interface:\n  display_name: "writing"\npolicy:\n'
        '  allow_implicit_invocation: ' + str(implicit).lower() + '\n', encoding='utf-8')
    (path / 'script.py').write_text('raise RuntimeError("must never execute")')
    reader.native.entries.append({'name': name, 'description': '用于写作', 'enabled': True,
                                  'path': str(path / 'SKILL.md')})
    return path


def test_skill_entrypoint_references_policy_and_refresh(shared):
    path = add_skill(shared, implicit=False)
    entry = shared.list_skills('valid')['skills'][0]
    assert entry['version'] == '1.0.1'
    assert entry['allow_implicit_invocation'] is False
    assert not entry['runtime_tools_connected']
    result = shared.read_skill_file('valid', entry['skill_id'])
    assert result['reference_paths'] == ['references/shared/rules.md']
    assert result['unavailable_references'] == 1
    assert '证据边界' in shared.read_skill_file('valid', entry['skill_id'], result['reference_paths'][0])['text']
    (path / 'references/shared/rules.md').write_text('已更新', encoding='utf-8')
    assert '已更新' in shared.read_skill_file('valid', entry['skill_id'], result['reference_paths'][0])['text']
    assert 'script.py' not in shared.list_skill_files('valid', entry['skill_id'])['files']
    with pytest.raises(ReadError):
        shared.read_skill_file('valid', entry['skill_id'], 'script.py')


def test_scope_and_disabled_skill(shared):
    add_skill(shared)
    add_skill(shared, 'personal', 'my-writing')
    add_skill(shared, 'unknown-plugin', 'unknown')
    assert len(shared.list_skills('valid')['skills']) == 2
    shared.native.entries[0]['enabled'] = False
    with pytest.raises(ReadError):
        shared.read_skill_file('valid', 'materials-manuscript-workflow:paper-writing')
    assert [x['skill_id'] for x in shared.list_skills('valid')['skills']] == ['personal:my-writing']


def test_ambiguous_skill_id_is_not_exposed(shared):
    add_skill(shared)
    shared.native.entries.append(dict(shared.native.entries[0]))
    assert shared.list_skills('valid')['skills'] == []
    assert shared.list_skills('valid')['incomplete'] is True


@pytest.mark.parametrize('path', ['../SKILL.md', 'C:/private.md', 'SKILL.md:secret', '../private.md',
                                 'references/shared/rules.md.', 'references/shared/rules.md '])
def test_skill_paths_denied(shared, path):
    add_skill(shared)
    with pytest.raises(ReadError):
        shared.read_skill_file('valid', 'materials-manuscript-workflow:paper-writing', path)


def test_manifest_and_entrypoint_junction(shared, tmp_path):
    path = add_skill(shared)
    manifest = path.parents[1] / '.codex-plugin/plugin.json'
    manifest.write_text('{"name":"materials-manuscript-workflow","version":"other"}')
    assert shared.list_skills('valid')['incomplete']
    assert shared.list_skills('valid')['skills'] == []
    if os.name != 'nt':
        return
    other = tmp_path / 'outside'
    other.mkdir()
    (other / 'SKILL.md').write_text('outside-private')
    linked = Path(shared.extensions['codex_home']) / 'skills/linked'
    linked.parent.mkdir(exist_ok=True)
    subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        f"New-Item -ItemType Junction -Path '{linked}' -Target '{other}' | Out-Null"], check=True, capture_output=True)
    try:
        shared.native.entries = [{'name': 'linked', 'enabled': True, 'path': str(linked / 'SKILL.md')}]
        assert shared.list_skills('valid')['skills'] == []
    finally:
        os.rmdir(linked)


def test_context_projection_secrets_and_long_line():
    secret = 'sk-test-secret-123456789'
    thread = {'turns': [{'items': [
        {'type': 'reasoning', 'summary': ['private-reasoning']},
        {'type': 'commandExecution', 'aggregatedOutput': 'private-tool-output'},
        {'type': 'userMessage', 'content': [{'type': 'text', 'text': f'api_key={secret}\n保留上下文'},
                                           {'type': 'localImage', 'path': 'private-image'}]},
        {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'x' * 25001 + 'end'}]}]}
    text, omitted = visible_context(thread)
    assert secret not in text and '[REDACTED' in text
    assert not any(value in text for value in ['private-reasoning', 'private-tool-output', 'private-image'])
    assert omitted == 3 and '保留上下文' in text and text.endswith('end')
    assert max(map(len, text.splitlines())) <= 2000


def test_context_paging_snapshot_and_project_binding(shared):
    shared.native.data['turns'][0]['items'][1]['text'] = '\n'.join(f'材料结果{i}' for i in range(550))
    result = shared.read_codex_context('valid')
    assert '材料结果549' in result['text'] and result['previous_start_line'] is not None
    earlier = shared.read_codex_context('valid', start_line=1, max_lines=1, snapshot_id=result['snapshot_id'])
    assert earlier['next_line'] == 2
    shared.native.data['turns'][0]['items'][1]['text'] += '\n新内容'
    with pytest.raises(ReadError, match='聊天内容已变化'):
        shared.read_codex_context('valid', start_line=2, snapshot_id=result['snapshot_id'])
    shared.native.data['cwd'] = str(shared.root.parent)
    with pytest.raises(ReadError, match='不属于当前项目'):
        shared.read_codex_context('valid')


def test_opt_in_and_revocation_before_after(shared, monkeypatch):
    add_skill(shared)
    active = json.loads(shared.active_file.read_text())
    active['extensions']['thread_id'] = '00000000-0000-0000-0000-000000000000'
    shared.active_file.write_text(json.dumps(active))
    with pytest.raises(ReadError):
        shared.read_codex_context('valid')
    assert shared.native.calls == []
    active['extensions'] = shared.extensions
    shared.active_file.write_text(json.dumps(active))
    original = shared.native.call
    def revoked(method, params):
        value = original(method, params)
        shared.active_file.unlink()
        return value
    monkeypatch.setattr(shared.native, 'call', revoked)
    with pytest.raises(ReadError):
        shared.list_skills('valid')
    assert len(shared.native.calls) == 1


def test_optional_functions_closed_by_default(shared):
    shared.extensions = {}
    active = {'root': str(shared.root), 'access_id': 'valid'}
    shared.active_file.write_text(json.dumps(active))
    assert not shared.info()['optional_sharing']['skills']
    with pytest.raises(ReadError, match='未开放'):
        shared.list_skills('valid')
    with pytest.raises(ReadError, match='未开放'):
        shared.read_codex_context('valid')
    assert shared.native.calls == []


def test_opened_skill_handle_scope(shared, monkeypatch):
    if os.name != 'nt':
        pytest.skip('Windows handle validation')
    add_skill(shared)
    import win32file
    monkeypatch.setattr(win32file, 'GetFinalPathNameByHandle', lambda *args: r'C:\outside\private.md')
    assert shared.list_skills('valid')['skills'] == []


def test_project_change_revokes_optional_sharing(shared):
    active = json.loads(shared.active_file.read_text())
    active['root'] = str(shared.root.parent)
    shared.active_file.write_text(json.dumps(active))
    with pytest.raises(ReadError):
        shared.list_skills('valid')
    assert shared.native.calls == []


def test_transport_handshake_no_turn_and_secret_environment(tmp_path, monkeypatch):
    script = tmp_path / 'fake-app-server.py'
    trace = tmp_path / 'trace.jsonl'
    script.write_text('''import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    with open("trace.jsonl", "a") as f: f.write(json.dumps(message) + "\\n")
    if message["method"] == "initialize":
        assert "CONTROL_PLANE_API_KEY" not in os.environ
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message["method"] == "thread/read":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": message["params"]["threadId"]}}}), flush=True)
''', encoding='utf-8')
    client = CodexReadOnly(sys.executable, tmp_path, tmp_path, timeout=3)
    monkeypatch.setenv('CONTROL_PLANE_API_KEY', 'sk-test-must-not-inherit')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-must-not-inherit')
    result = client._exchange([sys.executable, str(script)], 'thread/read', {'threadId': THREAD, 'includeTurns': True})
    assert result['thread']['id'] == THREAD
    assert [json.loads(line)['method'] for line in trace.read_text().splitlines()] == ['initialize', 'initialized', 'thread/read']
    with pytest.raises(ReadError):
        client.call('turn/start', {})
    with pytest.raises(ReadError):
        client.call('skills/list', {'cwds': ['another-project'], 'forceReload': True})


def test_transport_timeout_and_private_error(tmp_path):
    script = tmp_path / 'fake-app-server.py'
    script.write_text('import time\ntime.sleep(10)')
    client = CodexReadOnly(sys.executable, tmp_path, tmp_path, timeout=.3)
    started = time.monotonic()
    with pytest.raises(ReadError, match='timed out'):
        client._exchange([sys.executable, str(script)], 'skills/list', {})
    assert time.monotonic() - started < 4
    script.write_text('import sys,json\nfor line in sys.stdin:\n print(json.dumps({"id":json.loads(line).get("id"),"error":{"message":"private-secret"}}),flush=True)')
    with pytest.raises(ReadError) as error:
        client._exchange([sys.executable, str(script)], 'skills/list', {})
    assert 'private-secret' not in str(error.value)


def test_thread_id():
    assert parse_thread_id('codex://threads/' + THREAD) == THREAD
    for value in ['latest', '../private', THREAD + '?extra', 'codex://threads/other']:
        with pytest.raises(ReadError):
            parse_thread_id(value)


def test_native_resource_traversal_and_missing_install_root(shared, monkeypatch):
    home = Path(shared.extensions['codex_home'])
    with pytest.raises(ReadError):
        no_links(home / 'skills/../outside', home)
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    with pytest.raises(ReadError):
        discover_codex()


def test_extended_tools_over_mcp_stdio(shared, tmp_path):
    """Exercise the real MCP dispatcher/SDK with an isolated native metadata fixture."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    add_skill(shared, implicit=False)
    fixture_data = tmp_path / 'native-fixture.json'
    fixture_data.write_text(json.dumps({'thread': shared.native.data, 'skills': shared.native.entries}), encoding='utf-8')
    helper = tmp_path / 'fixture-server.py'
    repo = Path(__file__).resolve().parents[1]
    helper.write_text(f'''import sys, json, asyncio
from pathlib import Path
sys.path.insert(0, {str(repo)!r})
import server
from result_delivery import ResultReader
from codex_readonly import CodexReadOnly
fixture = json.loads(Path({str(fixture_data)!r}).read_text(encoding="utf-8"))
class FixtureNative(CodexReadOnly):
    def call(self, method, params):
        if method == "thread/read": return {{"thread": fixture["thread"]}}
        assert method == "skills/list"
        return {{"data": [{{"cwd": str(self.cwd), "skills": fixture["skills"], "errors": []}}]}}
class FixtureReader(ResultReader):
    def __init__(self, root, access, active, audit, ext=None):
        super().__init__(root, access, active, audit, ext, FixtureNative("unused", ext["codex_home"], root))
server.ResultReader = FixtureReader
asyncio.run(server.run())
''', encoding='utf-8')

    async def check():
        params = StdioServerParameters(command=sys.executable, args=[str(helper), '--session', str(shared.active_file)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                info = await client.call_tool('project_info', {})
                assert json.loads(info.content[0].text)['optional_sharing']['skills']
                context = await client.call_tool('read_codex_context', {'access_id': 'valid'})
                assert not context.is_error and '请按当前方案' in context.content[0].text
                library = await client.call_tool('list_skills', {'access_id': 'valid'})
                entry = json.loads(library.content[0].text)['skills'][0]
                document = await client.call_tool('read_skill_file', {'access_id': 'valid', 'skill_id': entry['skill_id']})
                assert json.loads(document.content[0].text)['allow_implicit_invocation'] is False
                files = await client.call_tool('list_skill_files', {'access_id': 'valid', 'skill_id': entry['skill_id']})
                assert 'references/shared/rules.md' in json.loads(files.content[0].text)['files']
                escape = await client.call_tool('read_skill_file', {'access_id': 'valid', 'skill_id': entry['skill_id'], 'path': '../private.md'})
                assert escape.is_error
                other_chat = await client.call_tool('read_codex_context', {'access_id': 'valid', 'thread_id': THREAD})
                assert other_chat.is_error
                shared.active_file.unlink()
                revoked = await client.call_tool('read_codex_context', {'access_id': 'valid'})
                assert revoked.is_error
        log = shared.audit.read_text(encoding='utf-8')
        assert '请按当前方案' not in log and 'private.md' not in log
    asyncio.run(check())
