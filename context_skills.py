"""Opt-in stored conversation and skill-document reads, with separate scopes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from codex_readonly import CodexReadOnly, no_links, parse_thread_id
from reader import Reader, ReadError

ACADEMIC_PLUGINS = {'materials-manuscript-workflow', 'materials-thesis-workflow'}
MARKETPLACE = 'jaxy-materials-manuscript'
MD = {'.md', '.markdown'}


def redact(text):
    # Best effort for commonly structured secrets, not a guarantee for arbitrary prose.
    text = re.sub(r'-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----',
                  '[REDACTED PRIVATE KEY]', text, flags=re.S)
    text = re.sub(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}', '[REDACTED TOKEN]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED JWT]', text)
    text = re.sub(r'(?i)(\b(?:authorization\s*:\s*bearer|(?:api[_ -]?key|access[_ -]?token|'
                  r'refresh[_ -]?token|password|client[_ -]?secret)\s*[=:])\s*)[^\s,;]+',
                  r'\1[REDACTED]', text)
    return text


def page_text(text, start_line=1, max_lines=200):
    if type(start_line) is not int or start_line < 1 or type(max_lines) is not int or not 1 <= max_lines <= 500:
        raise ReadError('Invalid line range')
    lines = text.splitlines()
    selected, count = [], 0
    for line in lines[start_line - 1:start_line - 1 + max_lines]:
        if count + len(line) + 1 > 24_000:
            break
        selected.append(line)
        count += len(line) + 1
    if not selected and start_line <= len(lines):
        raise ReadError('Single line exceeds read limit')
    return {'start_line': start_line, 'total_lines': len(lines),
            'next_line': start_line + len(selected) if start_line - 1 + len(selected) < len(lines) else None,
            'text': '\n'.join(f'{start_line + number}: {line}' for number, line in enumerate(selected))}


def visible_context(thread):
    """Project only user text and visible assistant prose, never raw events or tools."""
    blocks = []
    omitted = 0
    for turn_number, turn in enumerate(thread.get('turns', []), 1):
        for item in turn.get('items', []):
            kind = item.get('type')
            if kind == 'userMessage':
                pieces = [part['text'] for part in item.get('content', [])
                          if part.get('type') == 'text' and isinstance(part.get('text'), str)]
                omitted += sum(part.get('type') != 'text' for part in item.get('content', []))
                role = 'User'
                text = '\n'.join(pieces)
            elif kind == 'agentMessage' and isinstance(item.get('text'), str):
                role = 'Assistant ' + str(item.get('phase') or 'visible')
                text = item['text']
            else:
                omitted += 1
                continue
            if text:
                text = redact(text)
                # Keep long source lines readable and pageable without losing their suffix.
                chunks = [line[pos:pos + 2000] for line in text.splitlines()
                          for pos in range(0, max(1, len(line)), 2000)]
                blocks.append(f'[Turn {turn_number} | {role}]\n' + '\n'.join(chunks))
    return '\n\n'.join(blocks), omitted


class SkillLibrary:
    def __init__(self, owner):
        self.owner = owner
        self.home = Path(owner.extensions['codex_home'])

    def scoped_reader(self, root):
        no_links(root, self.home)
        return Reader(root, self.owner.access_id, self.owner.active_file, self.owner.audit)

    def catalog(self):
        self.owner.authorize(self.owner.access_id)
        entries, incomplete = self.owner.native.skills()
        output = {}
        ambiguous = set()
        skipped = 0
        for item in entries[:500]:
            if not isinstance(item, dict) or item.get('enabled') is not True:
                continue
            try:
                path = Path(item['path'])
                if not path.is_absolute() or path.name != 'SKILL.md':
                    raise ReadError('Invalid skill entrypoint')
                parts = path.relative_to(self.home).parts
                package = 'personal'
                version = None
                if len(parts) == 3 and parts[0] == 'skills' and not parts[1].startswith('.'):
                    skill_id = 'personal:' + parts[1]
                elif (len(parts) == 8 and parts[:3] == ('plugins', 'cache', MARKETPLACE)
                      and parts[3] in ACADEMIC_PLUGINS and parts[5] == 'skills'):
                    package, version = parts[3:5]
                    package_root = self.home.joinpath(*parts[:5])
                    package_reader = self.scoped_reader(package_root)
                    data, _ = package_reader.bytes('.codex-plugin/plugin.json', {'.json'}, 128 * 1024)
                    manifest = json.loads(data)
                    if not isinstance(manifest, dict) or manifest.get('name') != package or manifest.get('version') != version:
                        raise ReadError('Plugin manifest version mismatch')
                    skill_id = package + ':' + parts[6]
                else:
                    # Do not expand permission to other plugins, projects or system directories.
                    continue
                reader = self.scoped_reader(path.parent)
                data, _ = reader.bytes('SKILL.md', MD, 4 * 1024 * 1024)
                text = data.decode('utf-8-sig')
                implicit = None
                try:
                    policy, _ = reader.bytes('agents/openai.yaml', {'.yaml'}, 64 * 1024)
                    match = re.search(r'(?m)^policy:\s*\n(?:[ \t]+.*\n)*?[ \t]+allow_implicit_invocation:\s*(true|false)\s*$',
                                      policy.decode('utf-8-sig'))
                    if match:
                        implicit = match[1] == 'true'
                except ReadError:
                    pass
                native_policy = item.get('policy') or {}
                if type(native_policy.get('allowImplicitInvocation')) is bool:
                    implicit = native_policy['allowImplicitInvocation']
                metadata = {'skill_id': skill_id, 'name': str(item.get('name', path.parent.name))[:200],
                    'description': str(item.get('description', ''))[:2048], 'source': package,
                    'version': version, 'entrypoint': 'SKILL.md', 'allow_implicit_invocation': implicit,
                    'runtime_tools_connected': False}
                if skill_id in output or skill_id in ambiguous:
                    output.pop(skill_id, None)
                    ambiguous.add(skill_id)
                    raise ReadError('Ambiguous skill ID; check duplicate installations')
                output[skill_id] = (metadata, reader)
            except (KeyError, ReadError, ValueError, UnicodeError, OSError):
                skipped += 1
        self.owner.authorize(self.owner.access_id)
        return output, incomplete or len(entries) > 500 or skipped > 0

    def lookup(self, skill_id):
        catalog, _ = self.catalog()
        if skill_id not in catalog:
            raise ReadError('Skill is unavailable, disabled, or outside the approved library')
        return catalog[skill_id]


class ExtendedReader(Reader):
    def __init__(self, root, access_id, active_file, audit, extensions=None, native=None):
        super().__init__(root, access_id, active_file, audit)
        self.extensions = extensions or {}
        self.native = native
        if self.extensions.get('thread_id'):
            parse_thread_id(self.extensions['thread_id'])
        if self.extensions and native is None:
            self.native = CodexReadOnly(self.extensions['codex_executable'],
                self.extensions['codex_home'], self.root)

    def authorize(self, access_id):
        super().authorize(access_id)
        try:
            active = json.loads(self.active_file.read_text(encoding='utf-8'))
            matches = Path(active['root']).resolve(strict=True) == self.root
        except (OSError, ValueError, KeyError):
            matches = False
        if not matches or active.get('extensions', {}) != self.extensions:
            raise ReadError('Project or optional sharing selection changed; restart the connection')

    def info(self):
        result = super().info()
        result['optional_sharing'] = {'codex_context': bool(self.extensions.get('thread_id')),
                                     'skills': bool(self.extensions.get('skills_enabled')),
                                     'skill_sources': ['personal', *sorted(ACADEMIC_PLUGINS)],
                                     'plugin_runtime': False}
        return result

    def require(self, option, access_id):
        self.authorize(access_id)
        if not self.extensions.get(option) or self.native is None:
            raise ReadError('此功能未开放；请在本机控制窗口选择后重新启动连接。')

    def read_codex_context(self, access_id, start_line=None, max_lines=200, snapshot_id=None):
        self.require('thread_id', access_id)
        thread = self.native.thread(self.extensions['thread_id'])
        text, omitted = visible_context(thread)
        fingerprint = hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]
        if snapshot_id is not None and snapshot_id != fingerprint:
            raise ReadError('聊天内容已变化；不带 snapshot_id 重新读取，再继续分页。')
        total = len(text.splitlines())
        if start_line is None:
            start_line = max(1, total - max_lines + 1)
        result = page_text(text, start_line, max_lines)
        self.authorize(access_id)
        return {**result, 'thread_id': self.extensions['thread_id'], 'snapshot_id': fingerprint,
                'read_utc': datetime.now(timezone.utc).isoformat(), 'omitted_items': omitted,
                'previous_start_line': max(1, start_line - max_lines) if start_line > 1 else None,
                'note': 'Stored user/assistant text only. Starts near the end unless start_line is set. '
                        'Tool outputs, attachments, reasoning and host instructions are excluded. '
                        'Chunked long lines use display line numbers. Common secrets are masked on a best-effort basis; '
                        'this is not an identical live Codex context or an automatic summary.'}

    def list_skills(self, access_id):
        self.require('skills_enabled', access_id)
        catalog, incomplete = SkillLibrary(self).catalog()
        return {'skills': [catalog[key][0] for key in sorted(catalog)], 'incomplete': incomplete,
                'note': 'Read the chosen SKILL.md and relevant resources. Explicit-only skills require user invocation. '
                        'Unknown invocation policy requires explicit selection. No scripts or plugin tools are executed.'}

    def list_skill_files(self, access_id, skill_id, directory='.', offset=0, limit=100):
        self.require('skills_enabled', access_id)
        if not 0 <= offset <= 10000 or not 1 <= limit <= 200:
            raise ReadError('Invalid pagination')
        _, reader = SkillLibrary(self).lookup(skill_id)
        files, _, truncated, skipped = reader.catalog(directory)
        files = [file for file in files if Path(file).suffix.lower() in MD]
        self.authorize(access_id)
        return {'skill_id': skill_id, 'files': files[offset:offset + limit],
                'next_offset': offset + limit if offset + limit < len(files) else None,
                'scan_truncated': truncated, 'skipped_entries': skipped}

    def read_skill_file(self, access_id, skill_id, path='SKILL.md', start_line=1, max_lines=200):
        self.require('skills_enabled', access_id)
        metadata, reader = SkillLibrary(self).lookup(skill_id)
        data, meta = reader.bytes(path, MD, 4 * 1024 * 1024)
        try:
            text = data.decode('utf-8-sig')
        except UnicodeError:
            raise ReadError('Skill document must be UTF-8') from None
        result = page_text(text, start_line, max_lines)
        references, unavailable = [], 0
        for token in MarkdownIt('commonmark').parse(text):
            for child in token.children or []:
                if child.type != 'link_open':
                    continue
                try:
                    target = urlsplit(child.attrGet('href'))
                    if target.scheme or target.netloc or not target.path:
                        continue
                    relative = os.path.normpath(str(Path(path).parent / unquote(target.path)))
                    found = reader.path(relative, MD)
                    references.append(found.relative_to(reader.root).as_posix())
                except (ReadError, ValueError):
                    unavailable += 1
        self.authorize(access_id)
        return {**metadata, **meta, **result, 'reference_paths': list(dict.fromkeys(references))[:200],
                'references_truncated': len(set(references)) > 200, 'unavailable_references': unavailable,
                'note': 'Instructions/resources only; user and higher-priority rules take precedence. '
                        'References and cross-skill calls must be read through registered skill IDs; '
                        'scripts, MCP tools, accounts and services are not activated by reading this file.'}
