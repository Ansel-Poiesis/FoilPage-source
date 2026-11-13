# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Account-scoped notice cursors; notices never grant software or credit rights.

@玩家川行出品. Source: authenticated HTTPS, entitlements: EdDSA license.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlparse
from engine.local_file_lock import metadata_lock

RELEASE_REPOSITORY = 'Ansel-Poiesis/FoilPage-source'


def release_url(value) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        target = urlparse(value)
        suffix = target.path.removeprefix('/' + RELEASE_REPOSITORY)
        path_valid = re.fullmatch(r'/releases/(?:latest|tag/v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?|download/v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?/[A-Za-z0-9_-]+\.[A-Za-z0-9.]+)', suffix)
        return bool(target.scheme == 'https' and target.netloc == 'github.com'
                and not target.query and not target.fragment
                and target.path.startswith('/' + RELEASE_REPOSITORY + '/releases/') and path_valid)
    except ValueError:
        return False


def notice_subject(base: str, account_id: str) -> str:
    return hashlib.sha256((base.rstrip('/') + '\0' + account_id).encode('utf-8')).hexdigest()


def _time(value) -> float:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid notice time')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('notice time must have zone')
    return stamp.timestamp()


class ProductEvents:
    def __init__(self, path: Path):
        self.path, self.lock = Path(path), threading.RLock()

    def _read(self):
        try:
            if self.path.stat().st_size > 1_000_000:
                return {'subjects': {}, 'announcements': {}}
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(value.get('subjects'), dict) or not isinstance(value.get('announcements'), dict):
                raise ValueError('invalid inbox')
            # Persisted JSON is also untrusted. Preserve only valid cursors and
            # canonical notices; never expose arbitrary fields from disk.
            now = datetime.now(timezone.utc).timestamp()
            subjects = {}
            for key, record in list(value['subjects'].items())[-100:]:
                if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key) or not isinstance(record, dict):
                    continue
                cursor = record.get('cursor', 0)
                events = self._stored_events(record.get('events'), account=True, now=now)
                subjects[key] = {'cursor': cursor if type(cursor) is int and 0 <= cursor <= 2**53 - 1 else 0, 'events': events}
            announcements = {channel: self._stored_events(value['announcements'].get(channel), account=False, now=now, channel=channel)
                             for channel in ('stable', 'test')}
            return {'subjects': subjects, 'announcements': announcements}
        except (OSError, ValueError, TypeError, AttributeError):
            return {'subjects': {}, 'announcements': {}}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as stream:
                name = stream.name
                json.dump(data, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def cursor(self, subject):
        with self.lock:
            value = self._read()['subjects'].get(subject, {}).get('cursor', 0)
            return value if type(value) is int and value >= 0 else 0

    @classmethod
    def _stored_events(cls, raw, *, account, now, channel=None):
        events = []
        if not isinstance(raw, list):
            return events
        for item in raw[:50]:
            if not isinstance(item, dict):
                continue
            candidate = {**item, 'event_id': item.get('event_id')}
            if not account:
                candidate.update(id=item.get('event_id'), published_at=item.get('created_at'))
            try:
                public = cls._notice(candidate, account=account, now=now)
                if public and (account or public['channel'] == channel):
                    events.append(public)
            except (ValueError, TypeError, OverflowError):
                continue
        return events

    @staticmethod
    def _notice(item, *, account: bool, now: float):
        if not isinstance(item, dict):
            raise ValueError('invalid notice')
        kind = item.get('kind')
        allowed = {'credits_granted', 'subscription_updated'} if account else {'software_update'}
        if kind not in allowed:
            raise ValueError('invalid notice kind')
        created = item.get('created_at' if account else 'published_at')
        expires = item.get('expires_at')
        if _time(created) > now + 300 or _time(expires) < _time(created):
            raise ValueError('invalid notice lifetime')
        if _time(expires) <= now:
            return None
        identifier = item.get('event_id') if account else item.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}' if account else r'[A-Za-z0-9_.:-]{1,128}', identifier):
            raise ValueError('invalid notice id')
        for field, maximum in (('title', 80), ('message', 500)):
            if not isinstance(item.get(field), str) or len(item[field]) > maximum:
                raise ValueError('invalid notice copy')
        public = {'event_id': identifier, 'kind': kind, 'title': item['title'], 'message': item['message'],
                  'created_at': created, 'expires_at': expires}
        if not account:
            if item.get('channel') not in {'stable', 'test'} or not re.fullmatch(r'\d+\.\d+\.\d+', str(item.get('version', ''))):
                raise ValueError('invalid announcement version')
            if not release_url(item.get('url')):
                raise ValueError('invalid announcement URL')
            public.update(channel=item['channel'], version=item['version'], url=item['url'])
        return public

    def accept_account(self, subject, response, *, now=None):
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        if not isinstance(subject, str) or not re.fullmatch(r'[a-f0-9]{64}', subject) or not isinstance(response, dict) or response.get('ok') is not True:
            raise ValueError('invalid notice response')
        cursor, raw = response.get('cursor'), response.get('events')
        if type(cursor) is not int or not 0 <= cursor <= 2**53-1 or not isinstance(raw, list) or len(raw) > 50:
            raise ValueError('invalid notice response')
        with self.lock, metadata_lock(self.path):
            data = self._read(); previous = data['subjects'].get(subject, {'cursor': 0, 'events': []})
            old_cursor = previous['cursor']
            if cursor < old_cursor:
                raise ValueError('stale account notices')
            seen = {item['event_id'] for item in previous.get('events', [])}
            fresh, ids = [], set()
            for item in raw:
                sequence = item.get('id') if isinstance(item, dict) else None
                if type(sequence) is not int or not old_cursor < sequence <= cursor or sequence in ids:
                    raise ValueError('invalid notice sequence')
                ids.add(sequence)
                public = self._notice(item, account=True, now=now)
                if public and public['event_id'] not in seen:
                    fresh.append(public); seen.add(public['event_id'])
            data['subjects'][subject] = {'cursor': cursor, 'events': [*fresh, *previous.get('events', [])][:50]}
            self._write(data)
            return fresh

    def accept_announcements(self, channel, response, current_version, *, now=None):
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        raw = response.get('events') if isinstance(response, dict) and response.get('ok') is True else None
        if channel not in {'stable', 'test'} or not isinstance(raw, list) or len(raw) > 50:
            raise ValueError('invalid announcement response')
        with self.lock, metadata_lock(self.path):
            data = self._read(); previous = data['announcements'].get(channel, [])
            seen = {item['event_id'] for item in previous}; fresh = []
            if not re.fullmatch(r'\d+\.\d+\.\d+', str(current_version)):
                raise ValueError('invalid current version')
            current = tuple(map(int, current_version.split('.')))
            for item in raw:
                public = self._notice(item, account=False, now=now)
                if public and public['channel'] != channel:
                    raise ValueError('announcement channel mismatch')
                if public and tuple(map(int, public['version'].split('.'))) > current and public['event_id'] not in seen:
                    fresh.append(public); seen.add(public['event_id'])
            data['announcements'][channel] = [*fresh, *previous][:50]
            self._write(data)
            return fresh

    def snapshot(self, subject, channel, current_version=None):
        with self.lock:
            data = self._read()
            updates = data['announcements'].get(channel, [])
            if current_version is not None:
                if not re.fullmatch(r'\d+\.\d+\.\d+', str(current_version)):
                    raise ValueError('invalid current version')
                current = tuple(map(int, current_version.split('.')))
                updates = [item for item in updates if tuple(map(int, item['version'].split('.'))) > current]
            return {'ok': True, 'events': data['subjects'].get(subject, {}).get('events', [])[:20],
                    'updates': updates[:20]}
