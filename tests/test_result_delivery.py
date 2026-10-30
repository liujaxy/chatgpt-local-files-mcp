import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import result_delivery
from result_delivery import ResultReader, RESULT_DIRECTORY
from reader import ReadError


@pytest.fixture
def receiver(tmp_path):
    project = tmp_path / '测试项目'
    project.mkdir()
    (project / '研究.md').write_text('原始研究资料\n', encoding='utf-8')
    active = tmp_path / 'fake-session.json'
    active.write_text(json.dumps({'root': str(project), 'access_id': 'test-access'}), encoding='utf-8')
    return ResultReader(project, 'test-access', active, tmp_path / 'audit.jsonl')


def submit(receiver, submission_id='test-report-0001', content='# 分析报告\n完整结果与来源。\n'):
    return receiver.submit_web_result('test-access', submission_id, '测试分析', content,
                                      reported_model='user-reported-model')


def test_store_exact_report_duplicate_and_revision(receiver):
    content = '# 报告\r\n\n原始 Unicode 与换行。\n'
    receipt = submit(receiver, content=content)
    assert receipt['status'] == 'received' and receipt['review_status'] == 'not_reviewed'
    assert not receipt['duplicate']
    report = Path(receipt['local_report_path'])
    assert report.read_bytes() == content.encode('utf-8')
    assert receipt['content_sha256'] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert report.parent.parent == receiver.root / RESULT_DIRECTORY
    assert (receiver.root / '研究.md').read_text(encoding='utf-8') == '原始研究资料\n'
    assert submit(receiver, content=content)['duplicate']
    with pytest.raises(ReadError, match='different report'):
        submit(receiver, content='修改后的另一份报告')
    assert report.read_bytes() == content.encode('utf-8')
    revised = submit(receiver, 'test-report-0002', '修改后的另一份报告')
    assert revised['result_id'] != receipt['result_id']
    assert len(list((receiver.root / RESULT_DIRECTORY).iterdir())) == 2


def test_info_empty_catalog_no_write(receiver):
    info = receiver.info()
    assert info['version'] == '1.3.0'
    assert info['read_only'] is False and info['project_files_read_only'] is True
    assert info['result_delivery']['directory'] == RESULT_DIRECTORY
    assert receiver.list_web_results('test-access')['results'] == []
    with pytest.raises(ReadError, match='No web results'):
        receiver.read_web_result('test-access')
    assert not (receiver.root / 'outputs').exists()


def test_order_paging_and_integrity(receiver):
    first = submit(receiver, content='\n'.join(f'第{i}条结果' for i in range(550)))
    page = receiver.read_web_result('test-access', max_lines=2)
    assert page['result_id'] == first['result_id'] and page['next_line'] == 3
    second = submit(receiver, 'test-report-0002', '后来提交的报告')
    continued = receiver.read_web_result('test-access', first['result_id'], start_line=page['next_line'])
    assert '第2条结果' in continued['text'] and '后来提交' not in continued['text']
    assert continued['content_integrity'] == 'sha256_matches_receipt'
    newest = receiver.list_web_results('test-access', limit=1)
    assert newest['results'][0]['result_id'] == second['result_id']
    assert newest['next_offset'] == 1
    assert receiver.list_web_results('test-access', offset=1)['results'][0]['result_id'] == first['result_id']
    assert receiver.read_web_result('test-access')['result_id'] == second['result_id']
    long = submit(receiver, 'test-long-line-0003', 'x' * 25001 + '结尾')
    long_page = receiver.read_web_result('test-access', long['result_id'])
    assert long_page['next_line'] is not None
    assert receiver.read_web_result('test-access', long['result_id'], start_line=long_page['next_line'])['text'].endswith('结尾')
    assert Path(long['local_report_path']).read_text(encoding='utf-8') == 'x' * 25001 + '结尾'


@pytest.mark.parametrize('fields', [
    {'submission_id': '../outside'}, {'submission_id': 'C:\\outside'}, {'submission_id': 'bad:id-001'},
    {'content': ''}, {'content': 'x' * 200001}, {'content': '\x00'}, {'content': '\ud800'},
    {'title': ' '}, {'title': ' ' * 201 + 'x'}, {'reported_model': 'x' * 121},
])
def test_invalid_submission_never_writes(receiver, fields):
    arguments = dict(access_id='test-access', submission_id='test-validation-001', title='标题', content='完整报告')
    arguments.update(fields)
    with pytest.raises(ReadError):
        receiver.submit_web_result(**arguments)
    assert not (receiver.root / 'outputs').exists()


def test_revocation_project_change_and_persistence(receiver):
    receipt = submit(receiver)
    receiver.active_file.unlink()
    for operation in (lambda: submit(receiver), lambda: receiver.list_web_results('test-access'),
                      lambda: receiver.read_web_result('test-access', receipt['result_id'])):
        with pytest.raises(ReadError):
            operation()
    # A stopped tunnel cannot read remotely; native Codex can still read the saved artifact.
    assert '完整结果' in Path(receipt['local_report_path']).read_text(encoding='utf-8')
    receiver.active_file.write_text(json.dumps({'root': str(receiver.root), 'access_id': 'new-access'}))
    restarted = ResultReader(receiver.root, 'new-access', receiver.active_file, receiver.audit)
    assert restarted.read_web_result('new-access')['result_id'] == receipt['result_id']
    receiver.active_file.write_text(json.dumps({'root': str(receiver.root.parent), 'access_id': 'new-access'}))
    with pytest.raises(ReadError, match='selection changed'):
        restarted.list_web_results('new-access')


def test_revoke_mid_write_does_not_publish(receiver, monkeypatch):
    original = result_delivery.write_new
    def revoke(path, data):
        original(path, data)
        receiver.active_file.unlink()
    monkeypatch.setattr(result_delivery, 'write_new', revoke)
    with pytest.raises(ReadError, match='offline'):
        submit(receiver)
    assert list((receiver.root / RESULT_DIRECTORY).iterdir()) == []


def test_concurrent_retry_publishes_one_report(receiver):
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda _: submit(receiver), range(2)))
    assert {item['result_id'] for item in receipts} == {receipts[0]['result_id']}
    assert sorted(item['duplicate'] for item in receipts) == [False, True]
    assert len(list((receiver.root / RESULT_DIRECTORY).iterdir())) == 1


def test_corruption_scope_and_limits(receiver, tmp_path, monkeypatch):
    receipt = submit(receiver)
    with pytest.raises(ReadError, match='Invalid result_id'):
        receiver.read_web_result('test-access', '../研究')
    Path(receipt['local_report_path']).write_text('本机后来改动', encoding='utf-8')
    with pytest.raises(ReadError, match='differs from its receipt'):
        receiver.read_web_result('test-access', receipt['result_id'])
    (receiver.root / receipt['receipt_path']).write_text('{}')
    assert receiver.list_web_results('test-access')['skipped_entries'] == 1
    with pytest.raises(ReadError, match='unavailable'):
        receiver.read_web_result('test-access')
    monkeypatch.setattr(result_delivery, 'MAX_RESULTS', 1)
    with pytest.raises(ReadError, match='storage limit'):
        submit(receiver, 'test-over-limit-0002')
    other = tmp_path / '另一个项目'
    other.mkdir()
    active = tmp_path / 'other-active.json'
    active.write_text(json.dumps({'root': str(other), 'access_id': 'other'}))
    assert ResultReader(other, 'other', active, tmp_path / 'other-audit').list_web_results('other')['results'] == []


@pytest.mark.parametrize('relative', ['outputs', RESULT_DIRECTORY])
def test_result_junction_cannot_write_outside(receiver, tmp_path, relative):
    if os.name != 'nt':
        pytest.skip('Windows directory-handle contract')
    outside = tmp_path / 'outside'
    outside.mkdir()
    junction = receiver.root / relative
    junction.parent.mkdir(parents=True, exist_ok=True)
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        f'New-Item -ItemType Junction -Path {quote(junction)} -Target {quote(outside)} | Out-Null'],
        check=True, capture_output=True)
    try:
        with pytest.raises(ReadError, match='reparse'):
            submit(receiver)
        with pytest.raises(ReadError):
            receiver.list_web_results('test-access')
        assert list(outside.iterdir()) == []
    finally:
        os.rmdir(junction)


def test_opened_result_handle_is_checked_before_writing(receiver, monkeypatch):
    import win32file
    original = win32file.GetFinalPathNameByHandle
    def changed(handle, flags):
        actual = original(handle, flags)
        return r'C:\outside\report.md' if actual.endswith('report.md') else actual
    monkeypatch.setattr(win32file, 'GetFinalPathNameByHandle', changed)
    with pytest.raises(ReadError, match='outside the fixed destination'):
        submit(receiver)
    assert list((receiver.root / RESULT_DIRECTORY).iterdir()) == []


def test_copy_codex_instruction_uses_selected_project_without_clipboard_side_effect(receiver):
    import control
    copied = []
    window = SimpleNamespace(project=SimpleNamespace(get=lambda: str(receiver.root)),
        root=SimpleNamespace(clipboard_clear=lambda: None, clipboard_append=copied.append))
    control.Window.copy_result_prompt(window)
    assert str(receiver.root / RESULT_DIRECTORY) in copied[0] and 'content_sha256' in copied[0]


def test_result_tools_over_real_mcp_stdio(receiver):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check():
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / 'server.py'), '--session', str(receiver.active_file)])
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                assert len(tools) == 12
                assert [tool.name for tool in tools if not tool.annotations.read_only_hint] == ['submit_web_result']
                arguments = {'access_id': 'test-access', 'submission_id': 'mcp-result-test-0001',
                    'title': 'private-test-title', 'content': 'private-test-report', 'reported_model': 'private-test-model'}
                invalid = await client.call_tool('submit_web_result', {**arguments, 'path': '../source.md'})
                assert invalid.is_error
                saved = await client.call_tool('submit_web_result', arguments)
                assert not saved.is_error, saved
                receipt = json.loads(saved.content[0].text)
                assert receipt['status'] == 'received' and not receipt['duplicate']
                duplicate = await client.call_tool('submit_web_result', arguments)
                assert json.loads(duplicate.content[0].text)['duplicate']
                listed = await client.call_tool('list_web_results', {'access_id': 'test-access'})
                assert json.loads(listed.content[0].text)['results'][0]['result_id'] == receipt['result_id']
                report = await client.call_tool('read_web_result', {'access_id': 'test-access'})
                assert 'private-test-report' in json.loads(report.content[0].text)['text']
                receiver.active_file.unlink()
                revoked = await client.call_tool('submit_web_result', {**arguments, 'submission_id': 'revoked-test-0002'})
                assert revoked.is_error
        log = receiver.audit.read_text(encoding='utf-8')
        assert all(value not in log for value in ('private-test-title', 'private-test-report', 'private-test-model', '../source.md'))
    asyncio.run(check())
