import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PIL import Image
from reader import Reader, ReadError


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / '项目'
    (root / '资料').mkdir(parents=True)
    (root / '说明.md').write_text('# 研究\n![图](资料/图.png)\n第二行\n', encoding='utf-8')
    Image.new('RGB', (100, 80), 'red').save(root / '资料/图.png')
    active = tmp_path / 'active.json'
    active.write_text(json.dumps({'root': str(root), 'access_id': 'valid'}), encoding='utf-8')
    return Reader(root, 'valid', active, tmp_path / 'audit.jsonl')


def test_read_and_refresh(fixture):
    r = fixture
    result = r.read_markdown('valid', '说明.md', max_lines=2)
    assert result['image_paths'] == ['资料/图.png']
    assert result['next_line'] == 3
    (r.root / '说明.md').write_text('更新后的内容', encoding='utf-8')
    assert '更新后的内容' in r.read_markdown('valid', '说明.md')['text']
    assert len(r.list_files('valid', limit=1)['files']) == 1
    assert r.list_files('valid', limit=1)['next_offset'] == 1


@pytest.mark.parametrize('path', ['../outside.md', '..\\outside.md', 'C:/outside.md',
                                  'C:outside.md', '//server/share/x.md', '/outside.md',
                                  '说明.md:secret', '说明.md.', '说明.md '])
def test_reject_unsafe_paths(fixture, path):
    with pytest.raises(ReadError):
        fixture.read_markdown('valid', path)


def test_revocation(fixture):
    with pytest.raises(ReadError):
        fixture.list_files('previous')
    fixture.active_file.write_text('{"access_id":"next"}', encoding='utf-8')
    with pytest.raises(ReadError):
        fixture.info()
    fixture.active_file.unlink()
    with pytest.raises(ReadError):
        fixture.read_image('valid', '资料/图.png')


def test_actual_pixels(fixture):
    meta, encoded, mime = fixture.read_image('valid', '资料/图.png')
    assert mime == 'image/png'
    assert meta['original_size'] == (100, 80)
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)


def test_no_external_images(fixture):
    (fixture.root / '说明.md').write_text('![x](https://example.com/x.png)\n![y](../out.png)', encoding='utf-8')
    assert fixture.read_markdown('valid', '说明.md')['image_paths'] == []


def test_junction(fixture, tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows junction test')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.md').write_text('private')
    link = fixture.root / 'linked'
    command = ['powershell.exe', '-NoProfile', '-Command',
               f"New-Item -ItemType Junction -Path '{str(link).replace(chr(39), chr(39)*2)}' -Target '{str(outside).replace(chr(39), chr(39)*2)}' | Out-Null"]
    subprocess.run(command, check=True, capture_output=True)
    try:
        with pytest.raises(ReadError):
            fixture.read_markdown('valid', 'linked/secret.md')
        assert not any('secret' in p for p in fixture.list_files('valid')['files'])
    finally:
        os.rmdir(link)  # unlink junction only; never traverse its target


def test_stdio_protocol(fixture):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (fixture.root / 'AGENTS.md').write_text('项目约定：引用来源。', encoding='utf-8')
    async def check():
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / 'server.py'), '--session', str(fixture.active_file)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {t.name for t in tools.tools} == {'project_info', 'list_files', 'read_markdown', 'read_image', 'search_markdown'}
                info = await client.call_tool('project_info', {})
                bootstrap = json.loads(info.content[0].text)
                assert bootstrap['access_id'] == 'valid'
                assert bootstrap['version'] == '1.1.0'
                assert '引用来源' in bootstrap['project_documents'][0]['text']
                result = await client.call_tool('read_image', {'access_id': 'valid', 'path': '资料/图.png'})
                assert not result.is_error
                assert result.content[1].type == 'image'
                searched = await client.call_tool('search_markdown', {'access_id': 'valid', 'query': '研究'})
                assert json.loads(searched.content[0].text)['matches'][0]['line'] == 1
                cropped = await client.call_tool('read_image', {'access_id': 'valid', 'path': '资料/图.png', 'crop': [0, 0, 10, 20], 'max_side': 4096})
                assert json.loads(cropped.content[0].text)['returned_size'] == [10, 20]
                invalid = await client.call_tool('read_markdown', {'access_id': 'valid', 'path': '../secret.md'})
                assert invalid.is_error
                write_attempt = await client.call_tool('write_file', {'path': 'x.md', 'content': 'do not write'})
                assert write_attempt.is_error
                assert not (fixture.root / 'x.md').exists()
                assert 'do not write' not in fixture.audit.read_text(encoding='utf-8')
    asyncio.run(check())


def test_search_sources_paging_and_refresh(fixture):
    r = fixture
    (r.root / '资料/机制.md').write_text('Ni alloy\n蠕变机制\nni creep\nni particles', encoding='utf-8')
    first = r.search_markdown('valid', 'NI', directory='资料', limit=1)
    assert first['matches'][0]['line'] == 1
    second = r.search_markdown('valid', 'NI', directory='资料', offset=first['next_offset'], limit=1)
    assert second['matches'][0]['line'] == 3
    assert r.search_markdown('valid', 'NI', case_sensitive=True)['matches'] == []
    assert any(m['match'] == 'filename' for m in r.search_markdown('valid', '机制')['matches'])
    (r.root / '资料/机制.md').write_text('updated', encoding='utf-8')
    assert r.search_markdown('valid', 'NI')['matches'] == []


def test_search_no_match_and_limits(fixture):
    assert fixture.search_markdown('valid', 'absent')['matches'] == []
    for query in ['', ' ', 'x' * 257]:
        with pytest.raises(ReadError):
            fixture.search_markdown('valid', query)
    with pytest.raises(ReadError):
        fixture.search_markdown('valid', 'x', directory='../')


def test_reference_and_html_images(fixture):
    (fixture.root / '说明.md').write_text('![figure][fig]\n\n<img src="资料/图.png" width="50">\n\n[fig]: 资料/图.png\n', encoding='utf-8')
    assert fixture.read_markdown('valid', '说明.md', max_lines=1)['image_paths'] == ['资料/图.png']
    assert fixture.read_markdown('valid', '说明.md', start_line=3, max_lines=1)['image_paths'] == ['资料/图.png']


def test_image_path_parentheses_and_spaces(fixture):
    target = fixture.root / '资料/图 (1).png'
    Image.new('RGB', (10, 10)).save(target)
    (fixture.root / '说明.md').write_text('![图](<资料/图 (1).png>)', encoding='utf-8')
    assert fixture.read_markdown('valid', '说明.md')['image_paths'] == ['资料/图 (1).png']


def test_subdirectory_list(fixture):
    result = fixture.list_files('valid', directory='资料')
    assert result['files'] == ['资料/图.png']
    assert fixture.list_files('valid')['directories'] == ['资料']


def test_crop_pixels_and_original_unchanged(fixture):
    image = Image.new('RGB', (3000, 1000), 'blue')
    from PIL import ImageDraw
    ImageDraw.Draw(image).rectangle((2000, 0, 2999, 999), fill='red')
    path = fixture.root / '资料/图.png'
    image.save(path)
    original_bytes = path.read_bytes()
    meta, b64, mime = fixture.read_image('valid', '资料/图.png', crop=[2000, 0, 1000, 1000], max_side=4096)
    assert meta['returned_size'] == (1000, 1000)
    assert Image.open(io.BytesIO(base64.b64decode(b64))).getpixel((0, 0)) == (255, 0, 0)
    assert path.read_bytes() == original_bytes
    for crop in [[-1, 0, 10, 10], [0, 0, 0, 10], [2990, 0, 100, 100], [0, 0, 2.5, 2], [0, 0]]:
        with pytest.raises(ReadError):
            fixture.read_image('valid', '资料/图.png', crop=crop)


def test_tiff_pages(fixture):
    path = fixture.root / '资料/multi.tiff'
    first = Image.new('RGB', (40, 20), 'red')
    first.save(path, save_all=True, append_images=[Image.new('RGB', (40, 20), 'green')])
    meta, b64, _ = fixture.read_image('valid', '资料/multi.tiff', page=1)
    assert meta['pages'] == 2 and meta['page'] == 1
    assert Image.open(io.BytesIO(base64.b64decode(b64))).getpixel((0, 0)) == (0, 128, 0)
    with pytest.raises(ReadError):
        fixture.read_image('valid', '资料/multi.tiff', page=2)


def test_high_bit_tiff(fixture):
    image = Image.new('I', (2, 1))
    image.putdata([1000, 50000])
    image.save(fixture.root / '资料/high.tiff')
    meta, b64, _ = fixture.read_image('valid', '资料/high.tiff')
    output = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert output.getpixel((0, 0)) == (0, 0, 0)
    assert output.getpixel((1, 0)) == (255, 255, 255)
    assert 'not quantitative raw data' in meta['conversion']


def test_scanning_truncation(fixture, monkeypatch):
    import reader
    monkeypatch.setattr(reader, 'MAX_SCAN', 1)
    assert fixture.list_files('valid')['scan_truncated'] is True


def test_image_transparency(fixture):
    Image.new('RGBA', (10, 10), (0, 0, 0, 0)).save(fixture.root / '资料/alpha.png')
    _, b64, _ = fixture.read_image('valid', '资料/alpha.png')
    assert Image.open(io.BytesIO(base64.b64decode(b64))).getpixel((0, 0)) == (255, 255, 255)


def test_bootstrap_missing_documents(fixture):
    result = fixture.info()
    assert [d['status'] for d in result['project_documents']] == ['missing', 'missing']
    assert result['access_id'] == 'valid'
    assert fixture.list_files('valid')['files']


def test_bootstrap_reads_only_root_guidance_and_refreshes(fixture, monkeypatch):
    (fixture.root / 'AGENTS.md').write_text('保留科学术语', encoding='utf-8')
    (fixture.root / 'PROJECT_STATUS.md').write_text('当前任务：修改讨论', encoding='utf-8')
    read = []
    original = fixture.bytes
    def record(path, *args):
        read.append(path)
        return original(path, *args)
    monkeypatch.setattr(fixture, 'bytes', record)
    result = fixture.info()
    assert read == ['AGENTS.md', 'PROJECT_STATUS.md']
    assert '保留科学术语' in result['project_documents'][0]['text']
    assert '当前任务' in result['project_documents'][1]['text']
    (fixture.root / 'PROJECT_STATUS.md').write_text('新任务', encoding='utf-8')
    assert '新任务' in fixture.info()['project_documents'][1]['text']
    log = fixture.audit.read_text(encoding='utf-8')
    assert 'project_info.bootstrap' in log and '保留科学术语' not in log


def test_bootstrap_long_document_continuation(fixture):
    (fixture.root / 'AGENTS.md').write_text('\n'.join(f'约定{i}' for i in range(250)), encoding='utf-8')
    document = fixture.info()['project_documents'][0]
    assert document['next_line'] == 201
    assert document['total_lines'] == 250
    assert '约定249' not in document['text']
    assert '约定249' in fixture.read_markdown('valid', 'AGENTS.md', start_line=document['next_line'])['text']


def test_bootstrap_bad_encoding_does_not_block_project(fixture):
    (fixture.root / 'AGENTS.md').write_bytes(b'\xff\xfe\x80')
    (fixture.root / 'PROJECT_STATUS.md').write_text('正常状态', encoding='utf-8')
    documents = fixture.info()['project_documents']
    assert documents[0]['status'] == 'unavailable'
    assert documents[1]['status'] == 'read'
