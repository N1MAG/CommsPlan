"""Protected, optional Subspace file discovery. Worker-only filesystem/DB API.

A coherent copy is not proof that the radio transfer finished. Original files
are never changed; occurrence identity is independent of immutable blob identity.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading
import time
import uuid

from freqinout.core.config_paths import get_config_dir
from freqinout.core.message_file_scanner import file_path_display, file_path_key, file_path_from_key

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 20 * 1024 * 1024
MIN_FREE_BYTES = 50 * 1024 * 1024
MAX_SCAN_ENTRIES = 100
SETTLE_SECONDS = 5
CHUNK_BYTES = 64 * 1024
_active_lock = threading.Lock()
_active_captures = set()
_recovery_locks = {}


def _identity(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _fingerprint(s):
    return json.dumps(_identity(s))


def _dir_identity(path):
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or path.is_symlink():
        raise ValueError('Select an accessible real directory, not a link.')
    return (s.st_dev, s.st_ino)


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _digest_file(path, *, dir_fd=None):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags, **({'dir_fd':dir_fd} if dir_fd is not None else {}))
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError('Protected copy is unavailable or invalid.')
        digest = hashlib.sha256()
        total=0
        for block in iter(lambda: handle.read(CHUNK_BYTES), b''):
            total+=len(block)
            if total>MAX_FILE_BYTES:
                raise ValueError('Protected copy exceeds the capture limit.')
            digest.update(block)
        if _identity(before) != _identity(os.fstat(handle.fileno())):
            raise ValueError('Protected copy changed during verification.')
        return digest.hexdigest(), before.st_size


class SubspaceFileStore:
    """Owns a private durable capture journal, separate from normalized messages.

    Construction is IO-free. All public methods below are background operations.
    A caller can inject ``fault(stage)`` to exercise real crash/recovery boundaries.
    """
    def __init__(self, root=None, *, fault=None):
        self.root = Path(root) if root is not None else get_config_dir() / 'data' / 'subspace_received'
        self.db_path = self.root / 'index.sqlite'
        self.fault = fault or (lambda stage: None)
        self._init_lock = threading.RLock()
        self._ready = False
        self._root_identity = None
        self._owned_identities = {}
        self._iterators = {}
        self._source_locks = {}
        self._configuration_lock = threading.RLock()
        self._generation = 0
        self._stop_epoch = 0
        self._source_versions = {}
        self._source_signatures = {}
        self._scan_context = threading.local()

    def _initialize(self):
        with self._init_lock:
            if self._ready:
                if _dir_identity(self.root) != self._root_identity:
                    raise ValueError('Protected archive directory changed; restart after repair.')
                for name, identity in self._owned_identities.items():
                    if _dir_identity(self.root / name) != identity:
                        raise ValueError('Protected archive storage changed; repair it before retrying.')
                return
            self.root.mkdir(parents=True, exist_ok=True)
            self._root_identity = _dir_identity(self.root)
            for name in ('blobs', 'tmp'):
                p = self.root / name
                p.mkdir(exist_ok=True)
                self._owned_identities[name] = _dir_identity(p)
            if self.db_path.is_symlink():
                raise ValueError('Protected journal cannot be a link.')
            with self._connection(initial=True) as conn:
                conn.executescript('''
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS sources (
                        source_id TEXT PRIMARY KEY, path_key TEXT NOT NULL, label TEXT NOT NULL,
                        identity TEXT NOT NULL, policy TEXT NOT NULL, instances TEXT NOT NULL,
                        enabled INTEGER NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL,
                        baseline INTEGER NOT NULL DEFAULT 1, baseline_ns INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS entries (
                        source_id TEXT NOT NULL, path_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        occurrence_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(source_id,path_key));
                    CREATE TABLE IF NOT EXISTS occurrences (
                        occurrence_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, path_key TEXT NOT NULL,
                        filename TEXT NOT NULL, fingerprint TEXT NOT NULL, found_ts REAL NOT NULL,
                        stable_ts REAL NOT NULL, size INTEGER NOT NULL, state TEXT NOT NULL,
                        detail TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '',
                        temp_name TEXT NOT NULL DEFAULT '', reviewed INTEGER NOT NULL DEFAULT 0,
                        notes TEXT NOT NULL DEFAULT '', allow_empty INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1, projected_revision INTEGER NOT NULL DEFAULT 0);
                    CREATE INDEX IF NOT EXISTS occurrence_pending ON occurrences(state);
                ''')
                columns={r[1] for r in conn.execute('PRAGMA table_info(sources)')}
                if 'baseline_ns' not in columns:
                    conn.execute('ALTER TABLE sources ADD COLUMN baseline_ns INTEGER NOT NULL DEFAULT 0')
            self._ready = True
            self.recover()

    @property
    def generation(self):
        return self._generation

    def _check_operation(self, generation=None, cancel_event=None):
        token=getattr(self._scan_context,'token',None)
        scan_cancel=getattr(self._scan_context,'cancel_event',None)
        if token is not None and token != self._source_token(token[0]):
            raise InterruptedError('Receive source configuration changed; work retained.')
        if scan_cancel is not None and scan_cancel.is_set():
            raise InterruptedError('Receive monitoring stopped; work retained.')
        if ((generation is not None and generation != self._generation)
                or (cancel_event is not None and cancel_event.is_set())):
            raise InterruptedError('Review context changed; saved work retained.')

    def _source_token(self,sid):
        return (sid,self._stop_epoch,self._source_versions.get(sid,0))

    @contextlib.contextmanager
    def _connection(self, *, initial=False, generation=None, cancel_event=None):
        if not initial:
            self._initialize()
        conn = sqlite3.connect(self.db_path, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=FULL')
        try:
            self._check_operation(generation,cancel_event)
            yield conn
            with self._configuration_lock:
                self._check_operation(generation,cancel_event)
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _source(self, source_id):
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM sources WHERE source_id=?', (source_id,)).fetchone()
        if row is None:
            raise ValueError('Receive source is no longer configured.')
        return dict(row)

    def configure(self, profiles):
        """Coalesce physical directories; multiple configured owners never imply RF attribution."""
        self._initialize()
        with self._connection() as conn:
            known=[dict(r) for r in conn.execute('SELECT * FROM sources')]
        grouped = {}
        identities = {}
        rejected = []
        for profile in profiles:
            if not profile.get('js8_received_files_enabled'):
                continue
            raw = str(profile.get('js8_received_files_path') or '').strip()
            instance = str(profile.get('js8_instance_id') or profile.get('id') or '')
            if not raw:
                continue
            try:
                original = Path(raw).expanduser()
                # A root alias is accepted only when it is an actual directory;
                # symlink roots are deliberately not followed.
                identity = _dir_identity(original)
                path = original.resolve(strict=True)
                if self.root.resolve() == path or self.root.resolve() in path.parents:
                    raise ValueError('Receive source cannot be inside the protected archive.')
                key = file_path_key(path)
                sid = next((s['source_id'] for s in known if s['path_key']==key),None)
                sid = sid or identities.get(identity) or next((s['source_id'] for s in known if s['identity']==json.dumps(identity)),None)
                sid = sid or hashlib.sha256(os.fsencode(path)).hexdigest()
                identities[identity]=sid
                item = grouped.setdefault(sid, dict(source_id=sid,path_key=key,identity=json.dumps(identity),
                    policies=set(),instances=set(),label='Subspace received files'))
                item['policies'].add(str(profile.get('js8_received_files_existing') or 'review'))
                item['instances'].add(instance)
            except (OSError, ValueError) as exc:
                # Retain unavailable sources instead of making their files disappear.
                path = Path(raw).expanduser().absolute()
                sid = hashlib.sha256(os.fsencode(path)).hexdigest()
                rejected.append(dict(source_id=sid,path_key=file_path_key(path),label='Subspace received files',
                    identity='',policies={str(profile.get('js8_received_files_existing') or 'review')},instances={instance},error=str(exc)))
        with self._configuration_lock:
            wanted = set(grouped) | {s['source_id'] for s in rejected}
            with self._connection() as conn:
                old = {r['source_id']:dict(r) for r in conn.execute('SELECT * FROM sources')}
                for sid in set(old)-wanted:
                    conn.execute("UPDATE sources SET enabled=0,state='disabled',detail='Monitoring disabled' WHERE source_id=?",(sid,))
                for item in list(grouped.values()) + rejected:
                    sid = item['source_id']
                    policies = item['policies']
                    conflict = len(policies) != 1 or not policies <= {'review','new'}
                    policy = sorted(policies)[0]
                    error = item.get('error','') or ('Shared directory has conflicting existing-file choices. Match the choices in source settings.' if conflict else '')
                    identity_changed = bool(old.get(sid,{}).get('identity') and item['identity'] and old[sid]['identity'] != item['identity'])
                    if identity_changed:
                        error = 'Receive directory identity changed. Disable monitoring, save, then re-enable after confirming the directory.'
                    if old.get(sid,{}).get('enabled') == 0:
                        identity_changed = False
                        error = item.get('error','') or ('Shared directory choices conflict.' if conflict else '')
                    conn.execute('''INSERT INTO sources(source_id,path_key,label,identity,policy,instances,enabled,state,detail,baseline,baseline_ns)
                        VALUES(?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(source_id) DO UPDATE SET
                        path_key=excluded.path_key,label=excluded.label,identity=CASE WHEN sources.enabled=0 OR sources.identity='' THEN excluded.identity ELSE sources.identity END,
                        baseline=CASE WHEN sources.policy!=excluded.policy OR sources.enabled=0 THEN 1 ELSE sources.baseline END,
                        baseline_ns=CASE WHEN sources.policy!=excluded.policy OR sources.enabled=0 THEN excluded.baseline_ns ELSE sources.baseline_ns END,
                        policy=excluded.policy,instances=excluded.instances,enabled=excluded.enabled,state=CASE WHEN excluded.state='attention' THEN excluded.state WHEN sources.state='attention' THEN 'monitoring' ELSE sources.state END,
                        detail=CASE WHEN excluded.state='attention' THEN excluded.detail WHEN sources.state='attention' THEN '' ELSE sources.detail END''',
                        (sid,item['path_key'],item['label'],item['identity'],policy,json.dumps(sorted(item['instances'])),0 if conflict else 1,'attention' if error else 'monitoring',error,time.time_ns()))
                    if policy=='review' and old.get(sid,{}).get('policy')=='new':
                        conn.execute("DELETE FROM entries WHERE source_id=? AND occurrence_id=''",(sid,))
            signature = tuple(sorted((s['source_id'],s['identity'],s['policy'],s['enabled'],s['detail']) for s in self.sources()))
            if signature != getattr(self,'_config_signature',None):
                self._generation += 1
                self._config_signature = signature
                current={s['source_id']:(s['path_key'],s['identity'],s['policy'],s['enabled'],s['state']=='attention') for s in self.sources()}
                for sid in set(current)|set(self._source_signatures):
                    if current.get(sid)!=self._source_signatures.get(sid):
                        self._source_versions[sid]=self._source_versions.get(sid,0)+1
                        iterator=self._iterators.pop(sid,None)
                        if iterator is not None:
                            iterator.close()
                self._source_signatures=current
            return self.sources()

    def sources(self):
        with self._connection() as conn:
            return tuple(dict(r) for r in conn.execute('SELECT * FROM sources ORDER BY source_id'))

    def _close_iterators(self):
        iterators=tuple(self._iterators.values())
        self._iterators.clear()
        for iterator in iterators:
            iterator.close()

    def stop(self):
        with self._configuration_lock:
            self._generation += 1
            self._stop_epoch += 1
            self._close_iterators()

    def _check(self, source, generation, cancel_event):
        self._check_operation(cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('Source changed; protected work retained for retry.')
        if tuple(json.loads(source['identity'])) != _dir_identity(file_path_from_key(source['path_key'])):
            raise ValueError('Receive directory identity changed. Confirm it in source settings.')

    def scan(self, source_id, *, now=None, cancel_event=None):
        self._initialize()
        lock = self._source_locks.setdefault(source_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return {'state':'busy','processed':0}
        try:
            source = self._source(source_id)
            if not source['enabled'] or source['state']=='attention':
                return {'state':source['state'],'processed':0,'detail':source['detail']}
            generation = self._generation
            self._scan_context.token=self._source_token(source_id)
            self._scan_context.cancel_event=cancel_event
            self.recover()
            now = time.time() if now is None else now
            root = file_path_from_key(source['path_key'])
            self._check(source,generation,cancel_event)
            iterator = self._iterators.get(source_id)
            if iterator is None:
                iterator = os.scandir(root)
                self._iterators[source_id] = iterator
            processed = copied = 0
            started = time.monotonic()
            exhausted = False
            while processed < MAX_SCAN_ENTRIES and time.monotonic()-started < 0.25:
                self._check(source,generation,cancel_event)
                try:
                    entry = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                processed += 1
                if entry.name.startswith('.') or entry.name.lower().endswith(('.part','.partial','.tmp','.download','.crdownload')):
                    continue
                s = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(s.st_mode):
                    continue
                path = Path(entry.path)
                record = self._discover(source,path,s,now)
                if not record:
                    continue
                if record['state'] in {'waiting','attention'} and now-record['stable_ts'] >= SETTLE_SECONDS:
                    if s.st_size > MAX_FILE_BYTES:
                        self._attention(record['occurrence_id'],'File exceeds the 10 MiB capture limit; use manual handling.')
                    elif s.st_size == 0 and not record['allow_empty']:
                        self._attention(record['occurrence_id'],'Empty file found. Capture empty explicitly if intended; transfer completion is unverified.')
                    elif copied+s.st_size <= MAX_BATCH_BYTES:
                        try:
                            self._capture(source,record,s,generation,cancel_event)
                            copied += s.st_size
                        except (OSError,ValueError,InterruptedError) as exc:
                            self._attention(record['occurrence_id'],str(exc))
            if exhausted:
                iterator.close()
                self._iterators.pop(source_id,None)
                with self._connection() as conn:
                    conn.execute('UPDATE sources SET baseline=0 WHERE source_id=?',(source_id,))
            with self._connection() as conn:
                conn.execute("UPDATE sources SET state='monitoring',detail='' WHERE source_id=? AND state='unavailable'",(source_id,))
            return {'state':'monitoring','processed':processed,'copied_bytes':copied,'more':not exhausted}
        except (OSError,ValueError,InterruptedError) as exc:
            try:
                self._check_operation(cancel_event=cancel_event)
            except InterruptedError:
                return {'state':'cancelled','processed':0,'detail':str(exc)}
            iterator = self._iterators.pop(source_id,None)
            if iterator is not None:
                iterator.close()
            with self._connection() as conn:
                conn.execute("UPDATE sources SET state='unavailable',detail=? WHERE source_id=?",(str(exc),source_id))
            return {'state':'unavailable','processed':0,'detail':str(exc)}
        finally:
            self._scan_context.token=None
            self._scan_context.cancel_event=None
            lock.release()

    def _discover(self,source,path,s,now):
        key = file_path_key(path)
        fingerprint = _fingerprint(s)
        with self._connection() as conn:
            entry = conn.execute('SELECT * FROM entries WHERE source_id=? AND path_key=?',(source['source_id'],key)).fetchone()
            prior = conn.execute('SELECT * FROM occurrences WHERE occurrence_id=?',(entry['occurrence_id'],)).fetchone() if entry and entry['occurrence_id'] else None
            if entry and entry['fingerprint']==fingerprint:
                return dict(prior) if prior and prior['state']!='available' else None
            if not entry and source['baseline'] and source['policy']=='new' and s.st_ctime_ns<=source['baseline_ns']:
                conn.execute('INSERT INTO entries VALUES(?,?,?,?)',(source['source_id'],key,fingerprint,''))
                return None
            if prior and prior['state']!='available' and not prior['sha256']:
                oid = prior['occurrence_id']
                conn.execute("UPDATE occurrences SET fingerprint=?,stable_ts=?,size=?,state='waiting',detail='',allow_empty=0,revision=revision+1 WHERE occurrence_id=?",(fingerprint,now,s.st_size,oid))
            else:
                oid = uuid.uuid4().hex
                conn.execute('''INSERT INTO occurrences(occurrence_id,source_id,path_key,filename,fingerprint,found_ts,stable_ts,size,state)
                    VALUES(?,?,?,?,?,?,?,?,?)''',(oid,source['source_id'],key,file_path_display(path.name),fingerprint,now,now,s.st_size,'waiting'))
            conn.execute('INSERT INTO entries VALUES(?,?,?,?) ON CONFLICT(source_id,path_key) DO UPDATE SET fingerprint=excluded.fingerprint,occurrence_id=excluded.occurrence_id',(source['source_id'],key,fingerprint,oid))
            return dict(conn.execute('SELECT * FROM occurrences WHERE occurrence_id=?',(oid,)).fetchone())

    def _attention(self,oid,detail, *, generation=None,cancel_event=None):
        with self._connection(generation=generation,cancel_event=cancel_event) as conn:
            conn.execute("UPDATE occurrences SET state='attention',detail=?,revision=revision+1 WHERE occurrence_id=? AND (state!='attention' OR detail!=?)",(detail,oid,detail))

    @contextlib.contextmanager
    def _source_reader(self,source,path,flags):
        root=file_path_from_key(source['path_key'])
        directory_fd=None
        try:
            if os.open in os.supports_dir_fd:
                directory_fd=os.open(root,os.O_RDONLY | getattr(os,'O_DIRECTORY',0) | getattr(os,'O_NOFOLLOW',0))
                value=os.fstat(directory_fd)
                if (value.st_dev,value.st_ino)!=tuple(json.loads(source['identity'])):
                    raise ValueError('Receive directory identity changed.')
            fd=os.open(path if directory_fd is None else path.name,flags,**({'dir_fd':directory_fd} if directory_fd is not None else {}))
            with os.fdopen(fd,'rb') as handle:
                yield handle
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def _capture(self,source,record,expected,generation,cancel_event):
        oid = record['occurrence_id']
        if shutil.disk_usage(self.root).free < expected.st_size+MIN_FREE_BYTES:
            raise OSError('Not enough free space for protected capture. Free space, then retry.')
        name = uuid.uuid4().hex+'.part'
        temp = self.root/'tmp'/name
        with _active_lock:
            _active_captures.add((str(self.root),oid))
        try:
            with self._connection() as conn:
                conn.execute("UPDATE occurrences SET state='capturing',temp_name=?,revision=revision+1 WHERE occurrence_id=?",(name,oid))
            self._check(source,generation,cancel_event)
            path = file_path_from_key(record['path_key'])
            flags = os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0)
            with self._source_reader(source,path,flags) as reader:
                before = os.fstat(reader.fileno())
                if not stat.S_ISREG(before.st_mode) or _identity(before)!=_identity(expected) or path.is_symlink():
                    raise ValueError('Source changed before capture; waiting for a stable version.')
                # Advisory locks are supplementary; the second streamed hash and
                # identity checks establish read consistency even without them.
                if os.name!='nt':
                    import fcntl
                    fcntl.flock(reader.fileno(),fcntl.LOCK_SH | fcntl.LOCK_NB)
                with self._owned_directory('tmp') as tmpfd:
                    writefd = os.open(temp if tmpfd is None else name,os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0),0o600,**({'dir_fd':tmpfd} if tmpfd is not None else {}))
                    writer_handle=os.fdopen(writefd,'wb')
                with writer_handle as writer:
                    self.fault('temp_created')
                    digest = hashlib.sha256()
                    total = 0
                    for block in iter(lambda:reader.read(CHUNK_BYTES),b''):
                        self._check(source,generation,cancel_event)
                        total += len(block)
                        if total>MAX_FILE_BYTES:
                            raise ValueError('File grew beyond the capture limit; waiting for a stable version.')
                        digest.update(block)
                        writer.write(block)
                    writer.flush()
                    os.fsync(writer.fileno())
                self.fault('copy_flushed')
                if total!=expected.st_size or _identity(os.fstat(reader.fileno()))!=_identity(expected):
                    raise ValueError('Source changed while copying; retry after it settles.')
                reader.seek(0)
                again = hashlib.sha256()
                for block in iter(lambda:reader.read(CHUNK_BYTES),b''):
                    self._check(source,generation,cancel_event)
                    again.update(block)
                if digest.digest()!=again.digest() or _identity(os.fstat(reader.fileno()))!=_identity(expected) or _identity(path.lstat())!=_identity(expected):
                    raise ValueError('Source changed during verification; retry after it settles.')
                self._check(source,generation,cancel_event)
                hexdigest = digest.hexdigest()
                if record['sha256'] and record['sha256']!=hexdigest:
                    raise ValueError('Source differs from the preserved artifact. Original identity and notes retained.')
                with self._connection() as conn:
                    conn.execute('UPDATE occurrences SET sha256=? WHERE occurrence_id=?',(hexdigest,oid))
                self.fault('journal_committed')
                self._finalize(temp,hexdigest,total)
                self.fault('blob_finalized')
                self._check(source,generation,cancel_event)
                with self._connection() as conn:
                    conn.execute("UPDATE occurrences SET state='available',temp_name='',detail='File found · transfer completion unverified',revision=revision+1 WHERE occurrence_id=?",(oid,))
                self.fault('available_committed')
        except Exception:
            # A finalized blob/journal survives errors for recovery; unfinished
            # owned temps can be removed without touching the source.
            self._unlink_temp(name)
            raise
        finally:
            with _active_lock:
                _active_captures.discard((str(self.root),oid))

    @contextlib.contextmanager
    def _owned_directory(self,name):
        self._initialize()
        path=self.root/name
        if os.open not in os.supports_dir_fd:
            # Windows path fallback still fails closed on detected replacements;
            # native Windows concurrent-replacement qualification is separate.
            yield None
            self._initialize()
            return
        fd=os.open(path,os.O_RDONLY | getattr(os,'O_DIRECTORY',0) | getattr(os,'O_NOFOLLOW',0))
        try:
            value=os.fstat(fd)
            if (value.st_dev,value.st_ino)!=self._owned_identities[name]:
                raise ValueError('Protected storage directory changed.')
            yield fd
        finally:
            os.close(fd)

    def _unlink_temp(self,name):
        if not name:
            return
        with self._owned_directory('tmp') as fd:
            try:
                if fd is None:
                    (self.root/'tmp'/name).unlink()
                else:
                    os.unlink(name,dir_fd=fd)
            except FileNotFoundError:
                pass

    def _finalize(self,temp,digest,size):
        destination=self.root/'blobs'/(digest+'.blob')
        with self._owned_directory('tmp') as tmpfd,self._owned_directory('blobs') as blobfd:
            try:
                if tmpfd is None or blobfd is None:
                    os.link(temp,destination)
                else:
                    os.link(temp.name,destination.name,src_dir_fd=tmpfd,dst_dir_fd=blobfd,follow_symlinks=False)
                if blobfd is not None:
                    os.fsync(blobfd)
                else:
                    _sync_directory(destination.parent)
            except FileExistsError:
                actual,actual_size=_digest_file(destination if blobfd is None else destination.name,dir_fd=blobfd)
                if actual!=digest or actual_size!=size:
                    raise ValueError('Existing protected copy failed verification; no file was overwritten.')
            if tmpfd is None:
                temp.unlink()
                _sync_directory(temp.parent)
            else:
                os.unlink(temp.name,dir_fd=tmpfd)
                os.fsync(tmpfd)

    def recover(self):
        self._initialize()
        with _active_lock:
            lock=_recovery_locks.setdefault(str(self.root),threading.RLock())
        with lock:
            self._recover()

    def _recover(self):
        with self._connection() as conn:
            pending = [dict(r) for r in conn.execute("SELECT * FROM occurrences WHERE state='capturing' OR temp_name!=''")]
        for row in pending:
            with _active_lock:
                if (str(self.root),row['occurrence_id']) in _active_captures:
                    continue
            name = row['temp_name']
            if name and (len(name)!=37 or not name.endswith('.part') or not all(c in '0123456789abcdef' for c in name[:-5])):
                self._attention(row['occurrence_id'],'Invalid capture journal; protected source retained.')
                continue
            temp = self.root/'tmp'/name if name else None
            digest = row['sha256']
            try:
                if digest:
                    blob = self.root/'blobs'/(digest+'.blob')
                    candidate=blob
                    try:
                        with self._owned_directory('blobs') as blobfd:
                            actual=_digest_file(blob if blobfd is None else blob.name,dir_fd=blobfd)
                    except FileNotFoundError:
                        if temp is None:
                            raise ValueError('Interrupted copy is missing; retry capture.')
                        with self._owned_directory('tmp') as tmpfd:
                            actual=_digest_file(temp if tmpfd is None else temp.name,dir_fd=tmpfd)
                        candidate=temp
                    if actual!=(digest,row['size']):
                        raise ValueError('Interrupted copy is missing or incomplete; retry capture.')
                    if candidate==temp:
                        self._finalize(temp,digest,row['size'])
                    with self._connection() as conn:
                        conn.execute("UPDATE occurrences SET state='available',temp_name='',detail='File found · transfer completion unverified',revision=revision+1 WHERE occurrence_id=?",(row['occurrence_id'],))
                else:
                    self._attention(row['occurrence_id'],'Interrupted capture; retry after the source settles.')
                if temp:
                    self._unlink_temp(temp.name)
            except (OSError,ValueError) as exc:
                self._attention(row['occurrence_id'],str(exc))
            finally:
                with self._connection() as conn:
                    conn.execute("UPDATE occurrences SET temp_name='' WHERE occurrence_id=?",(row['occurrence_id'],))

    def list_items(self,limit=200):
        with self._connection() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM occurrences ORDER BY found_ts DESC,occurrence_id LIMIT ?',(min(max(1,int(limit)),200),))]

    def _record(self,oid):
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM occurrences WHERE occurrence_id=?',(oid,)).fetchone()
        if not row:
            raise ValueError('File item is no longer available.')
        return dict(row)

    @contextlib.contextmanager
    def _verified_blob_reader(self,record, *, generation=None,cancel_event=None):
        if record['state']!='available' or not record['sha256']:
            raise ValueError('A protected copy is not available yet.')
        name=record['sha256']+'.blob'
        with self._owned_directory('blobs') as fd:
            handlefd=os.open(self.root/'blobs'/name if fd is None else name,
                os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0),
                **({'dir_fd':fd} if fd is not None else {}))
            with os.fdopen(handlefd,'rb') as handle:
                before=os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size!=record['size'] or before.st_size>MAX_FILE_BYTES:
                    raise ValueError('Protected copy is missing or failed verification.')
                digest=hashlib.sha256(); total=0
                for block in iter(lambda:handle.read(CHUNK_BYTES),b''):
                    self._check_operation(generation,cancel_event)
                    total+=len(block)
                    if total>MAX_FILE_BYTES:
                        raise ValueError('Protected copy exceeds its recorded size.')
                    digest.update(block)
                if total!=record['size'] or digest.hexdigest()!=record['sha256'] or _identity(before)!=_identity(os.fstat(handle.fileno())):
                    raise ValueError('Protected copy failed verification.')
                handle.seek(0)
                yield handle
                self._check_operation(generation,cancel_event)
                if _identity(before)!=_identity(os.fstat(handle.fileno())):
                    raise ValueError('Protected copy changed during review.')

    def open_path(self,oid, *, generation=None, cancel_event=None):
        self._check_operation(generation,cancel_event)
        record = self._record(oid)
        if record['state']!='available' or not record['sha256']:
            raise ValueError('A protected copy is not available yet. Retry capture.')
        path = self.root/'blobs'/(record['sha256']+'.blob')
        try:
            with self._verified_blob_reader(record,generation=generation,cancel_event=cancel_event):
                pass
            valid=True
        except (OSError,ValueError):
            valid=False
        if not valid:
            self._attention(oid,'Protected copy is missing or failed verification; source and notes retained.',generation=generation,cancel_event=cancel_event)
            raise ValueError('Protected copy is missing or failed verification. Retry capture; notes are retained.')
        self._check_operation(generation,cancel_event)
        return path

    def detail(self,oid,preview_limit=65536, *, generation=None, cancel_event=None):
        self._check_operation(generation,cancel_event)
        row = self._record(oid)
        source = self._source(row['source_id'])
        row.update(source_label=source['label'],source_scope='Receiving radio and sender unverified',
            source_path=file_path_display(file_path_from_key(source['path_key'])),
            completion='unverified',preview='',preview_state='unavailable',archive_path='')
        if row['state']=='available':
            try:
                path = self.open_path(oid,generation=generation,cancel_event=cancel_event)
                row['archive_path']=str(path)
                limit = min(max(int(preview_limit),0),65536)
                if row['size']>limit:
                    row['preview_state']='too_large'
                else:
                    with self._verified_blob_reader(row,generation=generation,cancel_event=cancel_event) as reader:
                        raw = reader.read(limit+1)
                    if len(raw)>limit or hashlib.sha256(raw).hexdigest()!=row['sha256']:
                        raise ValueError('Protected copy changed during preview.')
                    text = raw.decode('utf-8-sig',errors='strict')
                    if any(ord(c)<32 and c not in '\n\r\t' for c in text):
                        raise UnicodeError('Binary controls')
                    row.update(preview=text,preview_state='text')
            except UnicodeError:
                row['preview_state']='binary'
            except (OSError,ValueError) as exc:
                row.update(state='attention',detail=str(exc),archive_path='')
        self._check_operation(generation,cancel_event)
        return row

    def mark_reviewed(self,oid,notes=None, *, generation=None, cancel_event=None):
        self._check_operation(generation,cancel_event)
        self._record(oid)
        if notes is not None and len(str(notes))>10000:
            raise ValueError('Review notes exceed the 10,000 character limit.')
        with self._connection(generation=generation,cancel_event=cancel_event) as conn:
            if notes is None:
                conn.execute('UPDATE occurrences SET reviewed=1,revision=revision+1 WHERE occurrence_id=?',(oid,))
            else:
                conn.execute('UPDATE occurrences SET reviewed=1,notes=?,revision=revision+1 WHERE occurrence_id=?',(str(notes),oid))

    def retry(self,oid,allow_empty=False, *, generation=None, cancel_event=None):
        self._check_operation(generation,cancel_event)
        record = self._record(oid)
        # A preserved artifact is never replaced underneath a reviewer. If its
        # source changed, discovery will create a separate occurrence.
        if record['sha256']:
            try:
                self.recover()
                self.open_path(oid,generation=generation,cancel_event=cancel_event)
                return
            except (OSError,ValueError):
                source=self._source(record['source_id'])
                path=file_path_from_key(record['path_key'])
                if not source['enabled'] or _fingerprint(path.lstat())!=record['fingerprint']:
                    raise ValueError('Original source changed or is unavailable. Preserved artifact identity and notes retained.')
        with self._connection(generation=generation,cancel_event=cancel_event) as conn:
            conn.execute("UPDATE occurrences SET state='waiting',detail='Waiting for a stable source',allow_empty=?,revision=revision+1 WHERE occurrence_id=?",(int(bool(allow_empty)),oid))

    def export(self,oid,destination, *, generation=None, cancel_event=None):
        self._check_operation(generation,cancel_event)
        record=self._record(oid)
        self.open_path(oid,generation=generation,cancel_event=cancel_event)
        target=Path(destination)
        name='.commsplan-'+uuid.uuid4().hex+'.part'
        temp=target.parent/name
        directory_fd=None
        if os.open in os.supports_dir_fd:
            directory_fd=os.open(target.parent,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
        try:
            fd=os.open(temp if directory_fd is None else name,os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0),0o600,**({'dir_fd':directory_fd} if directory_fd is not None else {}))
            with os.fdopen(fd,'wb') as writer,self._verified_blob_reader(record,generation=generation,cancel_event=cancel_event) as reader:
                digest=hashlib.sha256()
                total=0
                for block in iter(lambda:reader.read(CHUNK_BYTES),b''):
                    self._check_operation(generation,cancel_event)
                    total+=len(block)
                    if total>record['size']:
                        raise ValueError('Protected copy changed during export.')
                    writer.write(block)
                    digest.update(block)
                writer.flush()
                os.fsync(writer.fileno())
            if total!=record['size'] or digest.hexdigest()!=record['sha256']:
                raise ValueError('Protected copy changed during export.')
            with self._configuration_lock:
                self._check_operation(generation,cancel_event)
                if directory_fd is None:
                    os.link(temp,target)
                    _sync_directory(target.parent)
                else:
                    os.link(name,target.name,src_dir_fd=directory_fd,dst_dir_fd=directory_fd,follow_symlinks=False)
                    os.fsync(directory_fd)
            return target
        finally:
            try:
                if directory_fd is None:
                    temp.unlink(missing_ok=True)
                else:
                    try:os.unlink(name,dir_fd=directory_fd)
                    except FileNotFoundError:pass
            finally:
                if directory_fd is not None:os.close(directory_fd)

    def open_text_path(self,oid, *, generation=None,cancel_event=None):
        info=self.detail(oid,generation=generation,cancel_event=cancel_event)
        if info['preview_state']!='text':
            raise ValueError('Use Export for files without a verified plain-text preview.')
        # OS-open receives a generated .txt suffix rather than an executable or
        # unknown extension supplied by a received filename.
        target=self.root/'blobs'/(info['sha256']+'.txt')
        try:
            return self.export(oid,target,generation=generation,cancel_event=cancel_event)
        except FileExistsError:
            with self._owned_directory('blobs') as blobfd:
                if _digest_file(target if blobfd is None else target.name,dir_fd=blobfd)!=(info['sha256'],info['size']):
                    raise ValueError('Open-text copy failed verification; use Export.')
            self._check_operation(generation,cancel_event)
            return target

    def project_pending(self,db_path, *, generation=None, cancel_event=None):
        self._check_operation(generation,cancel_event)
        from freqinout.core.message_projection_store import (MessageSourceRecord,MessageProjectionRecord,ExternalMessageRef,MessageArtifactRecord,content_hash)
        from freqinout.core.message_projection_writer import ProjectionBundle,get_projection_writer
        with self._connection() as conn:
            rows=[dict(r) for r in conn.execute('SELECT * FROM occurrences WHERE revision>projected_revision ORDER BY found_ts LIMIT 25')]
        bundles=[]
        for row in rows:
            oid=row['occurrence_id']; sid='subspace-files:'+row['source_id']; mid='subspace-file:'+oid
            source=self._source(row['source_id'])
            info=dict(subspace_occurrence_id=oid,completion='unverified',capture_state=row['state'],
                original_filename=row['filename'],source_scope='Receiving radio and sender unverified')
            body=f"{row['filename']}\nSender unknown · receiving radio unverified\n{row['detail'] or 'Waiting for a stable source'}\nTransfer completion unverified"
            blobpath=str(self.root/'blobs'/(row['sha256']+'.blob')) if row['sha256'] else ''
            bundles.append(ProjectionBundle(
                MessageSourceRecord(sid,'subspace',source['label'],endpoint_or_path=file_path_display(file_path_from_key(source['path_key'])),capabilities={'read':True},provenance={'receiving_radio':'unverified'}),
                MessageProjectionRecord(mid,mid,content_hash(row['revision'],row['state'],row['sha256'],row['reviewed']),sid,'subspace',source_label=source['label'],message_type='Subspace file',display_type='Subspace file',status='READ' if row['reviewed'] else 'NEW',read_state='read' if row['reviewed'] else 'new',event_ts=row['found_ts'],received_ts=row['found_ts'],subject=row['filename'],summary=row['filename'],body_preview=body,entities=info,retention_class='artifact',search_text=' '.join((row['filename'],'Subspace file',row['detail'],'Sender unknown'))),
                (ExternalMessageRef(mid,sid,'subspace_capture',oid,external_path='',external_size=row['size'],external_hash=row['sha256'],metadata=info),),
                (MessageArtifactRecord('subspace-artifact:'+oid,mid,'subspace_capture',source_id=sid,external_key=oid,path=blobpath,content_hash=row['sha256'],transfer_state='unverified',metadata=info),)))
        if not bundles:
            return 0
        fence=_ProjectionFence(self,generation,cancel_event)
        result=get_projection_writer(db_path).submit(bundles,cancel_event=fence).result(timeout=5)
        if result.state != 'committed' or result.deferred_bundles:
            raise ValueError(str(result.error or result.state))
        with self._connection(generation=generation,cancel_event=cancel_event) as conn:
            for row in rows:
                conn.execute('UPDATE occurrences SET projected_revision=? WHERE occurrence_id=?',(row['revision'],row['occurrence_id']))
        return len(rows)


class _ProjectionFence:
    def __init__(self,store,generation,cancel_event):
        self.store=store
        self.expected=store.generation if generation is None else generation
        self.cancel_event=cancel_event
    def is_set(self):
        return self.expected!=self.store.generation or (self.cancel_event is not None and self.cancel_event.is_set())
    @contextlib.contextmanager
    def commit_guard(self):
        with self.store._configuration_lock:
            if self.is_set():
                raise InterruptedError('Receive context changed before projection commit.')
            yield


_default_lock=threading.Lock()
_default_stores={}

def default_file_store():
    root=get_config_dir()/'data'/'subspace_received'
    with _default_lock:
        return _default_stores.setdefault(str(root),SubspaceFileStore(root))


class SubspaceFileRuntime:
    """One independent daemon lane per canonical source; no UI-thread IO."""
    def __init__(self,store=None):
        self.store=store
        self._lock=threading.RLock()
        self._lanes={}
        self._threads=[]
        self._stopped=False
        self._lifecycle_generation=0
        self._health_keys=set()
        self._allowed_health_sources=set()

    def configure(self,profiles,db_path, *, should_continue=lambda:True):
        with self._lock:
            generation=self._lifecycle_generation
            if self.store is None:
                self.store=default_file_store()
            store=self.store
        sources=store.configure(profiles)
        wanted={s['source_id']:s for s in sources if s['enabled']}
        with self._lock:
            if generation!=self._lifecycle_generation or not should_continue():
                return
            self._stopped=False
            active_keys={'subspace-files:'+s['source_id'] for s in sources if s['enabled'] or s.get('state')=='attention'}
            from freqinout.core.dependency_health import get_dependency_health_registry
            registry=get_dependency_health_registry()
            self._allowed_health_sources=active_keys
            for key in self._health_keys-active_keys:
                registry.discard(key)
            self._health_keys.intersection_update(active_keys)
            for source in sources:
                if source.get('state')=='attention':
                    self._health(source['source_id'],'attention',source.get('detail','Receive source needs attention.'))
            for sid,(event,thread) in tuple(self._lanes.items()):
                if sid not in wanted or not thread.is_alive():
                    event.set()
                    del self._lanes[sid]
            for sid,source in wanted.items():
                if sid in self._lanes:
                    continue
                if len(self._lanes)>=8:
                    self._health(sid,'attention','At most eight receive directories can be monitored. Consolidate sources or disable another source.')
                    continue
                event=threading.Event()
                thread=threading.Thread(target=self._run,args=(sid,db_path,event),daemon=True,name='subspace-files-'+sid[:8])
                self._lanes[sid]=(event,thread)
                self._threads.append(thread)
                thread.start()
            self._threads=[t for t in self._threads if t.is_alive()]

    def _health(self,sid,state,detail):
        with self._lock:
            if self._stopped or "subspace-files:"+sid not in self._allowed_health_sources:
                return
            from freqinout.core.dependency_health import get_dependency_health_registry
            registry=get_dependency_health_registry()
            key='subspace-files:'+sid
            self._health_keys.add(key)
            metadata={'source_family':'subspace','state':state,'detail':detail,'optional':True}
            if state in {'monitoring','busy'}:
                registry.record_success(key,metadata=metadata)
            else:
                registry.record_failure(key,detail,metadata=metadata)


    def _run(self,sid,db_path,event):
        while not event.is_set():
            try:
                outcome=self.store.scan(sid,cancel_event=event)
                if event.is_set():
                    return
                self.store.project_pending(db_path,cancel_event=event)
                if outcome['state']=='cancelled':
                    event.wait(5)
                    continue
                self._health(sid,outcome['state'],outcome.get('detail','Monitoring receive directory; transfer completion remains unverified.'))
            except Exception as exc:
                self._health(sid,'unavailable',str(exc))
            event.wait(5)

    def stop(self):
        with self._lock:
            self._stopped=True
            self._lifecycle_generation+=1
            for event,thread in self._lanes.values():
                event.set()
            self._lanes.clear()
            if self.store is not None:
                self.store.stop()
            from freqinout.core.dependency_health import get_dependency_health_registry
            registry=get_dependency_health_registry()
            for key in tuple(self._health_keys):
                registry.discard(key)
            self._health_keys.clear()
            self._allowed_health_sources.clear()

    def is_stopped(self):
        with self._lock:
            return not any(t.is_alive() for t in self._threads)
