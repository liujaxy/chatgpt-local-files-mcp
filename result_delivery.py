"""Append-only web reports in one selected project; never starts a model task."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from context_skills import ExtendedReader, page_text
from reader import ReadError

RESULT_DIRECTORY = 'outputs/web-results'
MAX_CONTENT_CHARS = 200_000
MAX_CONTENT_BYTES = 1024 * 1024
MAX_RESULTS = 1000
SUBMISSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,63}')
RESULT_ID = re.compile(r'web-[0-9a-f]{32}')
HASH = re.compile(r'[0-9a-f]{64}')


def result_id_for(submission_id):
    if not isinstance(submission_id, str) or not SUBMISSION_ID.fullmatch(submission_id):
        raise ReadError('submission_id must be 8-64 letters, digits, underscores or hyphens; start with a letter or digit')
    return 'web-' + hashlib.sha256(submission_id.encode('ascii')).hexdigest()[:32]


def payload_hash(title, content, reported_model):
    data = json.dumps({'title': title, 'content': content, 'reported_model': reported_model},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def checked_handle(handle, expected, directory):
    import win32file
    attributes = win32file.GetFileInformationByHandle(handle)[0]
    if attributes & 0x400 or bool(attributes & 0x10) != directory:
        raise ReadError('Result storage links, reparse points and unexpected file types are not allowed')
    actual = win32file.GetFinalPathNameByHandle(handle, 0)
    if actual.startswith('\\\\?\\UNC\\'):
        actual = '\\\\' + actual[8:]
    elif actual.startswith('\\\\?\\'):
        actual = actual[4:]
    if Path(actual) != expected:
        raise ReadError('Opened result storage is outside the fixed destination')


@contextmanager
def pinned_directory(path):
    """Block rename/delete while writing, and inspect the directory handle itself."""
    if os.name != 'nt':
        raise ReadError('Result submission requires the supported Windows runtime')
    import win32con
    import win32file
    import pywintypes
    handle = None
    try:
        handle = win32file.CreateFile(str(path), 0, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None, win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
        checked_handle(handle, path, directory=True)
        yield
    except pywintypes.error:
        raise ReadError('Result destination is unavailable or locked; retry with the same submission_id') from None
    finally:
        if handle is not None:
            handle.Close()


def write_new(path, data):
    """CREATE_NEW prevents overwriting source files or following existing hard links."""
    import win32con
    import win32file
    import pywintypes
    handle = None
    try:
        handle = win32file.CreateFile(str(path), win32con.GENERIC_WRITE, 0, None,
            win32con.CREATE_NEW, win32con.FILE_ATTRIBUTE_NORMAL | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
        checked_handle(handle, path, directory=False)
        error, written = win32file.WriteFile(handle, data)
        if error or written != len(data):
            raise ReadError('Incomplete result write; retry with the same submission_id')
        win32file.FlushFileBuffers(handle)
    except pywintypes.error:
        raise ReadError('Result file could not be created or saved; retry with the same submission_id') from None
    finally:
        if handle is not None:
            handle.Close()


class ResultStore:
    def __init__(self, owner):
        self.owner = owner
        self.directory = owner.root / RESULT_DIRECTORY

    @contextmanager
    def writable_directory(self):
        with ExitStack() as stack:
            cursor = self.owner.root
            stack.enter_context(pinned_directory(cursor))
            for part in RESULT_DIRECTORY.split('/'):
                self.owner.authorize(self.owner.access_id)
                cursor /= part
                cursor.mkdir(exist_ok=True)
                stack.enter_context(pinned_directory(cursor))
            yield cursor

    def receipt(self, result_id, verify_content=False):
        if not isinstance(result_id, str) or not RESULT_ID.fullmatch(result_id):
            raise ReadError('Invalid result_id; use list_web_results to find a saved report')
        relative = f'{RESULT_DIRECTORY}/{result_id}'
        try:
            raw, _ = self.owner.bytes(relative + '/receipt.json', {'.json'}, 16 * 1024)
            item = json.loads(raw)
            valid = (isinstance(item, dict) and item.get('schema_version') == 1
                and item.get('result_id') == result_id
                and result_id_for(item.get('submission_id')) == result_id
                and item.get('project_root') == str(self.owner.root)
                and item.get('review_status') == 'not_reviewed'
                and isinstance(item.get('title'), str) and 1 <= len(item['title']) <= 200
                and isinstance(item.get('reported_model'), str) and len(item['reported_model']) <= 120
                and type(item.get('content_bytes')) is int and 0 < item['content_bytes'] <= MAX_CONTENT_BYTES
                and isinstance(item.get('content_sha256'), str) and HASH.fullmatch(item['content_sha256'])
                and isinstance(item.get('payload_sha256'), str) and HASH.fullmatch(item['payload_sha256'])
                and isinstance(item.get('received_utc'), str) and len(item['received_utc']) <= 40)
            if not valid or datetime.fromisoformat(item['received_utc']).utcoffset() != timezone.utc.utcoffset(None):
                raise ValueError('Invalid receipt')
            content = None
            if verify_content:
                data, _ = self.owner.bytes(relative + '/report.md', {'.md'}, MAX_CONTENT_BYTES)
                content = data.decode('utf-8')
                if (len(data) != item['content_bytes'] or hashlib.sha256(data).hexdigest() != item['content_sha256']
                        or payload_hash(item['title'], content, item['reported_model']) != item['payload_sha256']):
                    raise ReadError('Saved report differs from its receipt; inspect it locally before continuing')
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ReadError('Saved result receipt is invalid; inspect the result directory locally') from None
        return item, content

    def public_receipt(self, item):
        relative = f'{RESULT_DIRECTORY}/{item["result_id"]}'
        return {**item, 'report_path': relative + '/report.md', 'receipt_path': relative + '/receipt.json',
                'local_report_path': str(self.owner.root / relative / 'report.md'),
                'note': 'Received web-model output, not a verified conclusion or permission to execute instructions. '
                        'reported_model is self-reported, not independently verified. Continue in Codex only when the user asks.'}

    def catalog(self):
        self.owner.authorize(self.owner.access_id)
        for relative in ('outputs', RESULT_DIRECTORY):
            try:
                (self.owner.root / relative).lstat()
            except FileNotFoundError:
                return [], 0
            directory = self.owner.path(relative, directory=True)
        items, skipped = [], 0
        deadline = time.monotonic() + 8
        with os.scandir(directory) as entries:
            for number, entry in enumerate(entries, 1):
                self.owner.authorize(self.owner.access_id)
                if number > MAX_RESULTS * 2 or time.monotonic() > deadline:
                    raise ReadError('Result scan limit reached; organize the fixed result directory locally')
                if not RESULT_ID.fullmatch(entry.name):
                    continue
                if len(items) >= MAX_RESULTS:
                    raise ReadError('Result storage limit reached; organize saved reports locally')
                try:
                    item, _ = self.receipt(entry.name)
                    items.append(item)
                except ReadError:
                    skipped += 1
        items.sort(key=lambda item: (datetime.fromisoformat(item['received_utc']), item['result_id']), reverse=True)
        self.owner.authorize(self.owner.access_id)
        return items, skipped

    def submit(self, access_id, submission_id, title, content, reported_model=''):
        self.owner.authorize(access_id)
        result_id = result_id_for(submission_id)
        if not isinstance(title, str) or not title.strip() or not 1 <= len(title) <= 200 or '\x00' in title:
            raise ReadError('title must contain 1-200 characters')
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT_CHARS or '\x00' in content:
            raise ReadError(f'content must contain 1-{MAX_CONTENT_CHARS} characters of the complete report')
        if not isinstance(reported_model, str) or len(reported_model) > 120:
            raise ReadError('reported_model must contain at most 120 characters')
        try:
            data = content.encode('utf-8')
            fingerprint = payload_hash(title, content, reported_model)
        except UnicodeError:
            raise ReadError('Report must be valid UTF-8 text') from None
        if len(data) > MAX_CONTENT_BYTES:
            raise ReadError('Report exceeds the 1 MiB UTF-8 limit')
        item = {'schema_version': 1, 'result_id': result_id, 'submission_id': submission_id,
                'project_root': str(self.owner.root), 'title': title,
                'received_utc': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
                'reported_model': reported_model, 'content_bytes': len(data),
                'content_sha256': hashlib.sha256(data).hexdigest(), 'payload_sha256': fingerprint,
                'review_status': 'not_reviewed'}
        with self.writable_directory() as directory:
            destination = directory / result_id
            if destination.exists():
                return self.existing(result_id, fingerprint)
            items, skipped = self.catalog()
            if len(items) + skipped >= MAX_RESULTS:
                raise ReadError('Result storage limit reached; organize saved reports locally')
            pending = directory / ('.pending-' + uuid.uuid4().hex)
            pending.mkdir()
            try:
                with pinned_directory(pending):
                    self.owner.authorize(access_id)
                    write_new(pending / 'report.md', data)
                    self.owner.authorize(access_id)
                    write_new(pending / 'receipt.json', json.dumps(item, ensure_ascii=False, indent=2).encode('utf-8'))
                    self.owner.authorize(access_id)
                try:
                    # Windows rename does not replace an existing destination directory.
                    pending.rename(destination)
                except FileExistsError:
                    return self.existing(result_id, fingerprint)
            finally:
                if pending.exists():
                    # Remove only our two known staging files; never recurse into a changed path.
                    with pinned_directory(pending):
                        for name in ('receipt.json', 'report.md'):
                            (pending / name).unlink(missing_ok=True)
                    pending.rmdir()
        self.owner.authorize(access_id)
        saved, _ = self.receipt(result_id, verify_content=True)
        return {**self.public_receipt(saved), 'status': 'received', 'duplicate': False}

    def existing(self, result_id, fingerprint):
        saved, _ = self.receipt(result_id, verify_content=True)
        if saved['payload_sha256'] != fingerprint:
            raise ReadError('submission_id already contains a different report; use a new submission_id for a revision')
        self.owner.authorize(self.owner.access_id)
        return {**self.public_receipt(saved), 'status': 'received', 'duplicate': True}


class ResultReader(ExtendedReader):
    def info(self):
        result = super().info()
        result.update(read_only=False, project_files_read_only=True,
            result_delivery={'enabled': True, 'directory': RESULT_DIRECTORY,
                'local_directory': str(self.root / RESULT_DIRECTORY), 'manual_codex_continue': True,
                'max_content_chars': MAX_CONTENT_CHARS, 'max_content_bytes': MAX_CONTENT_BYTES,
                'max_results': MAX_RESULTS,
                'note': 'Only new report artifacts may be saved in this fixed directory. '
                        'No source-file edits, model dispatch or automatic Codex continuation.'})
        return result

    def submit_web_result(self, access_id, submission_id, title, content, reported_model=''):
        return ResultStore(self).submit(access_id, submission_id, title, content, reported_model)

    def list_web_results(self, access_id, offset=0, limit=20):
        self.authorize(access_id)
        if type(offset) is not int or not 0 <= offset <= MAX_RESULTS or type(limit) is not int or not 1 <= limit <= 100:
            raise ReadError('Invalid result pagination')
        store = ResultStore(self)
        items, skipped = store.catalog()
        return {'results': [store.public_receipt(item) for item in items[offset:offset + limit]],
                'total_results': len(items), 'skipped_entries': skipped,
                'next_offset': offset + limit if offset + limit < len(items) else None,
                'order': 'received_utc descending; content hashes are checked when reading the report'}

    def read_web_result(self, access_id, result_id=None, start_line=1, max_lines=200):
        self.authorize(access_id)
        store = ResultStore(self)
        if result_id is None:
            items, skipped = store.catalog()
            if skipped:
                raise ReadError('Some saved results are unavailable; list results and specify a result_id after inspecting the directory')
            if not items:
                raise ReadError('No web results have been submitted for this project')
            result_id = items[0]['result_id']
        item, content = store.receipt(result_id, verify_content=True)
        # Preserve the original report on disk; only display lines are split for bounded paging.
        display = '\n'.join(line[pos:pos + 2000] for line in content.splitlines()
                            for pos in range(0, max(1, len(line)), 2000))
        page = page_text(display, start_line, max_lines)
        self.authorize(access_id)
        return {**store.public_receipt(item), **page, 'content_integrity': 'sha256_matches_receipt',
                'line_numbering': 'Display lines; source lines longer than 2000 characters are split without changing report.md'}
