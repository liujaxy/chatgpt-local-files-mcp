"""Read-only project primitives. No network or model calls."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import time
import warnings
from html.parser import HTMLParser
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageOps
from markdown_it import MarkdownIt
from workflow import VERSION, WORKFLOW

EXTENSIONS = {'.md', '.markdown', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff'}
IMAGE_EXTENSIONS = EXTENSIONS - {'.md', '.markdown'}
Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_SCAN = 10_000
IGNORED_DIRS = {'node_modules', '__pycache__', 'state', 'vendor'}


class ImageTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'img':
            source = dict(attrs).get('src')
            if source:
                self.targets.append(source)


class ReadError(Exception):
    pass


class Reader:
    def __init__(self, root: Path, access_id: str, active_file: Path, audit: Path):
        self.root = root.resolve(strict=True)
        self.access_id = access_id
        self.active_file = active_file
        self.audit = audit
        if not self.root.is_dir():
            raise ReadError('Project is not a directory')

    def authorize(self, access_id):
        try:
            active = json.loads(self.active_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ReadError('Project offline; restart the local connection') from None
        if access_id != self.access_id or active.get('access_id') != self.access_id:
            raise ReadError('Stale project access ID; start a new chat for the selected project')

    def path(self, relative: str, extensions=EXTENSIONS, directory=False):
        if directory and relative in {'', '.'}:
            return self.root
        if not isinstance(relative, str) or not relative or len(relative) > 2000:
            raise ReadError('Invalid relative path')
        win = PureWindowsPath(relative)
        if win.is_absolute() or win.drive or win.root or ':' in relative or '\x00' in relative:
            raise ReadError('Only project-relative paths are allowed')
        if any(p in {'.', '..'} or p.endswith((' ', '.')) for p in relative.replace('\\', '/').split('/')):
            raise ReadError('Traversal and ambiguous paths are not allowed')
        candidate = self.root.joinpath(*win.parts)
        # Reject reparse points, including Windows junctions, rather than following them.
        cursor = self.root
        for part in win.parts:
            cursor = cursor / part
            try:
                s = cursor.lstat()
            except OSError:
                raise ReadError('File not found') from None
            if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
                raise ReadError('Links and reparse points are not allowed')
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError):
            raise ReadError('Path outside project or unavailable') from None
        if directory:
            if not resolved.is_dir():
                raise ReadError('Not a directory')
            return resolved
        if resolved.suffix.lower() not in extensions or not resolved.is_file():
            raise ReadError('Unsupported file type')
        return resolved

    def bytes(self, relative, extensions, max_bytes):
        p = self.path(relative, extensions)
        try:
            with p.open('rb') as f:
                # Check the opened handle, not just the path, to resist junction races.
                if os.name == 'nt':
                    import msvcrt
                    import win32file
                    actual = win32file.GetFinalPathNameByHandle(msvcrt.get_osfhandle(f.fileno()), 0)
                    if actual.startswith('\\\\?\\UNC\\'):
                        actual = '\\\\' + actual[8:]
                    elif actual.startswith('\\\\?\\'):
                        actual = actual[4:]
                    try:
                        Path(actual).relative_to(self.root)
                    except ValueError:
                        raise ReadError('Opened file is outside project') from None
                before = os.fstat(f.fileno())
                if before.st_size > max_bytes:
                    raise ReadError('File exceeds read limit')
                data = f.read(max_bytes + 1)
                after = os.fstat(f.fileno())
                if len(data) > max_bytes:
                    raise ReadError('File exceeds read limit')
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise ReadError('File changed during reading; retry')
        except OSError:
            raise ReadError('File unavailable') from None
        return data, {'path': p.relative_to(self.root).as_posix(),
                      'modified_utc': datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat()}

    def info(self):
        self.authorize(self.access_id)
        documents = []
        for name in ('AGENTS.md', 'PROJECT_STATUS.md'):
            try:
                (self.root / name).lstat()
            except FileNotFoundError:
                documents.append({'path': name, 'status': 'missing'})
                continue
            except OSError:
                documents.append({'path': name, 'status': 'unavailable'})
                continue
            try:
                document = self.read_markdown(self.access_id, name, max_lines=200)
                documents.append({**document, 'status': 'read'})
                self.record('project_info.bootstrap', name, 'ok')
            except ReadError as exc:
                documents.append({'path': name, 'status': 'unavailable', 'reason': str(exc)})
                self.record('project_info.bootstrap', None, 'denied')
        self.authorize(self.access_id)
        return {'project': self.root.name, 'access_id': self.access_id, 'read_only': True,
                'version': VERSION, 'file_types': sorted(EXTENSIONS),
                'project_documents': documents,
                'limits': {'scan_entries': MAX_SCAN, 'markdown_bytes': 4194304, 'image_bytes': 67108864,
                           'image_pixels': 40000000, 'returned_image_max_side': 4096},
                'instructions': WORKFLOW}

    def catalog(self, directory='.'):
        start = self.path(directory, directory=True)
        files, directories, skipped = [], [], 0
        scanned, truncated = 0, False
        deadline = time.monotonic() + 8
        for base, dirs, names in os.walk(start, followlinks=False):
            if time.monotonic() > deadline:
                truncated = True
                break
            safe = []
            for d in sorted(dirs):
                scanned += 1
                if scanned > MAX_SCAN:
                    truncated = True
                    break
                if d.startswith('.') or d in IGNORED_DIRS:
                    continue
                rel = (Path(base) / d).relative_to(self.root).as_posix()
                try:
                    self.path(rel, directory=True)
                    safe.append(d)
                    directories.append(rel)
                except ReadError:
                    skipped += 1
            dirs[:] = safe
            if truncated:
                break
            for name in sorted(names):
                scanned += 1
                if scanned > MAX_SCAN or time.monotonic() > deadline:
                    truncated = True
                    break
                if Path(name).suffix.lower() not in EXTENSIONS or name.startswith('.'):
                    continue
                relative = (Path(base) / name).relative_to(self.root).as_posix()
                try:
                    self.path(relative)
                    files.append(relative)
                except ReadError:
                    skipped += 1
            if truncated:
                break
        return files, directories, truncated, skipped

    def list_files(self, access_id, offset=0, limit=100, directory='.'):
        self.authorize(access_id)
        if not 0 <= offset <= 10_000 or not 1 <= limit <= 200:
            raise ReadError('Invalid pagination')
        files, dirs, truncated, skipped = self.catalog(directory)
        return {'files': files[offset:offset + limit],
                'directories': dirs[offset:offset + limit],
                'next_offset': offset + limit if offset + limit < len(files) else None,
                'next_directory_offset': offset + limit if offset + limit < len(dirs) else None,
                'scan_truncated': truncated, 'skipped_entries': skipped,
                'note': 'If truncated, query a narrower directory. Hidden and support directories are not scanned.'}

    def search_markdown(self, access_id, query, directory='.', offset=0, limit=30, case_sensitive=False):
        self.authorize(access_id)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
            raise ReadError('Query must contain 1-256 characters')
        if not 0 <= offset <= 10_000 or not 1 <= limit <= 100:
            raise ReadError('Invalid pagination')
        files, _, scan_truncated, skipped = self.catalog(directory)
        needle = query if case_sensitive else query.lower()
        hits, count, read_bytes = [], 0, 0
        more = False
        truncated = scan_truncated
        deadline = time.monotonic() + 8
        for relative in files:
            self.authorize(access_id)
            if time.monotonic() > deadline or read_bytes >= 32 * 1024 * 1024:
                truncated = True
                break
            filename = relative if case_sensitive else relative.lower()
            candidates = []
            if needle in filename:
                candidates.append({'path': relative, 'line': None, 'match': 'filename'})
            if Path(relative).suffix.lower() in {'.md', '.markdown'}:
                try:
                    data, meta = self.bytes(relative, {'.md', '.markdown'}, 4 * 1024 * 1024)
                    read_bytes += len(data)
                    for number, line in enumerate(data.decode('utf-8-sig').splitlines(), 1):
                        pos = (line if case_sensitive else line.lower()).find(needle)
                        if pos >= 0:
                            left = max(0, pos - 100)
                            candidates.append({**meta, 'line': number, 'match': 'content',
                                               'snippet': line[left:left + 350]})
                        if len(candidates) > offset + limit:
                            break
                except (ReadError, UnicodeError):
                    skipped += 1
            for hit in candidates:
                if count >= offset:
                    if len(hits) == limit:
                        more = True
                        break
                    hits.append(hit)
                count += 1
            if more:
                break
        return {'matches': hits, 'next_offset': offset + limit if more else None,
                'scan_truncated': truncated, 'skipped_files': skipped,
                'note': 'Literal text search, no model calls. Results reflect current files; narrow directory if scan_truncated.'}

    def read_markdown(self, access_id, path, start_line=1, max_lines=200):
        self.authorize(access_id)
        if not 1 <= start_line or not 1 <= max_lines <= 500:
            raise ReadError('Invalid line range')
        data, meta = self.bytes(path, {'.md', '.markdown'}, 4 * 1024 * 1024)
        try:
            lines = data.decode('utf-8-sig').splitlines()
        except UnicodeError:
            raise ReadError('Markdown must be UTF-8') from None
        selected, chars = [], 0
        for line in lines[start_line - 1:start_line - 1 + max_lines]:
            if chars + len(line) > 24_000:
                if not selected:
                    raise ReadError('Single line exceeds 24000 characters; split it locally')
                break
            selected.append(line)
            chars += len(line)
        # Parse the full document so reference-style definitions outside the slice resolve.
        tokens = MarkdownIt('commonmark').parse('\n'.join(lines))
        targets = []
        for token in tokens:
            if not token.map or token.map[1] <= start_line - 1 or token.map[0] >= start_line - 1 + len(selected):
                continue
            for child in [token] + (token.children or []):
                if child.type == 'image':
                    targets.append(child.attrGet('src'))
                elif child.type in {'html_inline', 'html_block'}:
                    parser = ImageTags()
                    parser.feed(child.content)
                    targets.extend(parser.targets)
        images, unavailable = [], 0
        for target in targets:
            try:
                url = urlsplit(target)
            except ValueError:
                unavailable += 1
                continue
            if url.scheme or url.netloc or not url.path:
                unavailable += 1
                continue
            rel = os.path.normpath(str(Path(path).parent / unquote(url.path)))
            try:
                p = self.path(rel, IMAGE_EXTENSIONS)
                images.append(p.relative_to(self.root).as_posix())
            except ReadError:
                unavailable += 1
        return {**meta, 'start_line': start_line, 'total_lines': len(lines),
                'next_line': start_line + len(selected) if start_line - 1 + len(selected) < len(lines) else None,
                'text': '\n'.join(f'{start_line + i}: {line}' for i, line in enumerate(selected)),
                'image_paths': list(dict.fromkeys(images)), 'unavailable_image_references': unavailable}

    def read_image(self, access_id, path, page=0, crop=None, max_side=1600):
        self.authorize(access_id)
        if not isinstance(page, int) or page < 0 or not 256 <= max_side <= 4096:
            raise ReadError('Invalid page or max_side (256-4096)')
        if crop is not None and (not isinstance(crop, list) or len(crop) != 4 or
                                 any(type(v) is not int for v in crop)):
            raise ReadError('crop must be [x,y,width,height] in oriented original-image pixels')
        data, meta = self.bytes(path, IMAGE_EXTENSIONS, 64 * 1024 * 1024)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                source = Image.open(io.BytesIO(data))
            with source:
                frames = getattr(source, 'n_frames', 1)
                if page >= frames:
                    raise ReadError(f'Page out of range; use 0 through {frames - 1}')
                source.seek(page)
                if source.width * source.height > Image.MAX_IMAGE_PIXELS:
                    raise ReadError('Image exceeds 40 million pixels')
                original = source.size
                oriented = ImageOps.exif_transpose(source)
                oriented_size = oriented.size
                conversion = ['EXIF orientation applied', 'no AI/OCR']
                if oriented.mode in {'I', 'F', 'I;16', 'I;16B', 'I;16L'}:
                    lo, hi = oriented.getextrema()
                    if hi > lo:
                        oriented = oriented.convert('F').point(lambda p: (p - lo) * 255 / (hi - lo)).convert('L')
                    else:
                        oriented = Image.new('L', oriented.size, 0)
                    conversion.append(f'High-bit grayscale mapped linearly to 8-bit using full-page min={lo}, max={hi}; not quantitative raw data')
                if crop is not None:
                    x, y, width, height = crop
                    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > oriented.width or y + height > oriented.height:
                        raise ReadError('Crop must fit the oriented full-resolution image')
                    oriented = oriented.crop((x, y, x + width, y + height))
                if 'A' in oriented.getbands() or 'transparency' in oriented.info:
                    rgba = oriented.convert('RGBA')
                    picture = Image.new('RGB', rgba.size, 'white')
                    picture.paste(rgba, mask=rgba.getchannel('A'))
                    conversion.append('Transparency composited on white')
                else:
                    picture = oriented.convert('RGB')
                picture.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                stream = io.BytesIO()
                picture.save(stream, format='PNG')
                encoded = stream.getvalue()
                if len(encoded) > 5 * 1024 * 1024:
                    stream = io.BytesIO()
                    picture.save(stream, format='JPEG', quality=90)
                    encoded, mime = stream.getvalue(), 'image/jpeg'
                    conversion.append('JPEG quality 90 applied for payload size')
                else:
                    mime = 'image/png'
                while len(encoded) > 5 * 1024 * 1024 and max(picture.size) > 256:
                    picture.thumbnail((max(256, int(picture.width * .8)), max(256, int(picture.height * .8))), Image.Resampling.LANCZOS)
                    stream = io.BytesIO()
                    picture.save(stream, format='JPEG', quality=90)
                    encoded, mime = stream.getvalue(), 'image/jpeg'
                    conversion.append('Further reduced to fit 5 MiB payload')
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ReadError('Image cannot be decoded safely') from None
        return ({**meta, 'original_size': original, 'returned_size': picture.size,
                 'oriented_size': oriented_size, 'crop': crop,
                 'page': page, 'pages': frames,
                 'conversion': '; '.join(conversion), 'max_side': max_side},
                base64.b64encode(encoded).decode('ascii'), mime)

    def record(self, tool, relative, status):
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        # Never log attacker-provided arguments, rejected paths, content, or errors.
        entry = {'time': datetime.now(timezone.utc).isoformat(), 'tool': tool, 'status': status}
        if status == 'ok' and relative:
            entry['path'] = relative
        with self.audit.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
