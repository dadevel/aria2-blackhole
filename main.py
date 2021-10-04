#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path
from typing import Any, AsyncGenerator
import asyncio
import base64
import dataclasses
import json
import logging
import os
import shutil
import sys
import uuid

from asyncinotify import Inotify, Mask
import websockets


async def main() -> None:
    config = Configuration.from_env()
    downloads = DownloadManager.from_file(config.state_file)
    async with Aria2Client(config.endpoint, config.token) as aria2:
        listener = Aria2EventListener(config.download_dir, config.storage_dir, aria2, downloads)
        watcher = InotifyWatcher(config.torrent_dir, aria2, downloads)
        tasks = [
            listener.start(),
            watcher.start(),
        ]
        await asyncio.wait(tasks)


@dataclasses.dataclass
class Configuration:
    token: str
    endpoint: str
    torrent_dir: Path
    download_dir: Path
    storage_dir: Path
    state_file: Path
    log_level: str

    def __post_init__(self) -> None:
        logging.basicConfig(level=self.log_level.upper(), stream=sys.stderr)

    @classmethod
    def from_env(cls) -> Configuration:
        try:
            return cls(
                os.environ['ARIA2_BLACKHOLE_TOKEN'],
                os.environ.get('ARIA2_BLACKHOLE_ENDPOINT', 'ws://localhost:6800/jsonrpc'),
                Path(os.environ.get('ARIA2_BLACKHOLE_TORRENT_DIR', './torrents')),
                Path(os.environ.get('ARIA2_BLACKHOLE_DOWNLOAD_DIR', './downloads')),
                Path(os.environ.get('ARIA2_BLACKHOLE_STORAGE_DIR', './files')),
                Path(os.environ.get('ARIA2_BLACKHOLE_STATE_FILE', './downloads/state.json')),
                os.environ.get('ARIA2_BLACKHOLE_LOG_LEVEL', 'info'),
            )
        except Exception as e:
            raise RuntimeError('failed to load configuration: missing or invalid environment variables') from e


class DownloadManager:
    def __init__(self, state_file: Path, state: set = None) -> None:
        self._state_file = state_file
        self._state = state if state else set()
        self._log = logging.getLogger('dlmgr')

    @classmethod
    def from_file(cls, state_file: Path) -> DownloadManager:
        if state_file.exists():
            data = json.loads(state_file.read_text())
            return cls(state_file, set(data['downloads']))
        return cls(state_file)

    def get(self) -> set:
        return self._state.copy()

    def track(self, gid: str) -> None:
        self._log.info(f'tracking {gid}')
        self._state.add(gid)
        self._store()

    def untrack(self, gid: str) -> None:
        self._log.info(f'untracking {gid}')
        self._state.discard(gid)
        self._store()

    def exists(self, gid: str) -> bool:
        return gid in self._state

    def _store(self) -> None:
        self._log.debug(f'storing state {self._state}')
        data = json.dumps(dict(downloads=list(self._state)))
        self._state_file.write_text(data)


class Aria2Client:
    version = '2.0'

    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint
        self.token = token
        self._callbacks = dict()
        self._events = asyncio.Queue()
        self._log = logging.getLogger('aria2')
        self._websocket = None
        self._task = None

    async def __aenter__(self) -> Aria2Client:
        assert not self._websocket and not self._task, 'invalid state'
        self._websocket = await websockets.connect(self.endpoint)  # type: ignore
        self._task = self._start()
        result = await self.get_version()
        self._log.info(f'connected to aria2 {result["version"]}')
        return self

    async def __aexit__(self, *_) -> None:
        assert self._websocket and self._task, 'invalid state'
        await self._websocket.close()
        self._task.cancel()

    async def subscribe(self) -> AsyncGenerator:
        while True:
            event = await self._events.get()
            method = event.get('method')
            params = event.get('params')
            if method and params:
                yield method, params
            self._events.task_done()

    async def get_version(self) -> dict[str, str]:
        return await self.call('aria2.getVersion')

    async def add_torrent(self, content: bytes) -> str:
        return await self.call('aria2.addTorrent', base64.b64encode(content).decode(), list(), dict())

    async def get_files(self, gid: str) -> list[dict[str, str]]:
        return await self.call('aria2.getFiles', gid)

    async def tell_status(self, gid: str, keys: list[str] = None) -> dict[str, str]:
        try:
            return await self.call('aria2.tellStatus', gid, keys if keys else ['gid', 'status'])
        except Aria2Error as e:
            if e.error.get('code') == 1:
                raise Aria2GidNotFound()
            else:
                raise

    async def call(self, method: str, *params: Any) -> Any:
        id = await self._send(method, *params)
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._callbacks[id] = future
        return await future

    async def _send(self, method: str, *params: Any) -> str:
        assert self._websocket and self._task, 'invalid state'
        id = str(uuid.uuid4())
        msg = dict(jsonrpc=self.version, id=id, method=method, params=[f'token:{self.token}', *params])
        await self._websocket.send(json.dumps(msg))
        return id

    def _start(self) -> asyncio.Task:
        return asyncio.create_task(self._run(), name='aria2-client')

    async def _run(self) -> None:
        try:
            self._log.info('starting up')
            while True:
                await self._step()
        except Exception as e:
            self._log.error('caught exception')
            self._log.exception(e)
            raise

    async def _step(self) -> None:
        assert self._websocket and self._task, 'invalid state'
        msg = json.loads(await self._websocket.recv())
        version = msg.get('jsonrpc')
        assert version == self.version, f'unsupported jsonrpc version {version}'
        id = msg.get('id')
        error = msg.get('error')
        method = msg.get('method')
        result = msg.get('result')
        if id is None and error:
            return
        if method:
            self._log.debug(f'event: {msg}')
            self._events.put_nowait(msg)
        elif id and error:
            self._log.debug(f'response error: {error}')
            future = self._callbacks.pop(id)
            future.set_exception(Aria2Error(error))
        elif id and result:
            self._log.debug(f'response result: {result}')
            future = self._callbacks.pop(id)
            future.set_result(result)
        else:
            self._log.warning(f'unhandeled message: {msg}')


class Aria2Error(Exception):
    def __init__(self, error: dict[str, str]) -> None:
        super().__init__(f'aria2 responded with error: {error}')
        self.error = error


class Aria2GidNotFound(Exception):
    pass


class Aria2EventListener:
    def __init__(self, download_dir: Path, storage_dir: Path, aria2: Aria2Client, downloads: DownloadManager):
        self._download_dir = download_dir
        self._storage_dir = storage_dir
        self._aria2 = aria2
        self._downloads = downloads
        self._log = logging.getLogger('aria2-events')

    def start(self) -> asyncio.Task:
        return asyncio.create_task(self._run(), name='aria2-events')

    async def _run(self):
        try:
            self._log.info('catching up')
            await self._catch_up()
            self._log.info('starting up')
            async for event, params in self._aria2.subscribe():
                await self._step(event, params)
        except Exception as e:
            self._log.error('caught exception')
            self._log.exception(e)
            raise

    async def _catch_up(self) -> None:
        for gid in self._downloads.get():
            try:
                result = await self._aria2.tell_status(gid)
                gid = result['gid']
                status = result['status']
                if status in ('active', 'waiting', 'paused'):
                    continue
                elif status == 'error':
                    self._downloads.untrack(gid)
                elif status in ('complete', 'removed'):
                    result = await self._aria2.get_files(gid)
                    self._move_files(result)
                    self._downloads.untrack(gid)
                else:
                    self._log.error(f'download {gid} has unknown status {status}')
            except Aria2GidNotFound:
                self._downloads.untrack(gid)

    async def _step(self, event: str, params: list[dict[str, str]]) -> None:
        if event in ('aria2.onDownloadStop', 'aria2.onDownloadError'):
            gid = params[0]['gid']
            self._downloads.untrack(gid)
        elif event == 'aria2.onDownloadComplete':
            gid = params[0]['gid']
            if self._downloads.exists(gid):
                result = await self._aria2.get_files(gid)
                self._move_files(result)
                self._downloads.untrack(gid)

    def _move_files(self, message: list[dict[str, str]]):
        paths = [Path(item['path']) for item in message]
        paths = [self._download_dir/path.name for path in paths]
        self._log.info(f'moving {", ".join(str(x) for x in paths)} to destination')
        for path in paths:
            dest = self._storage_dir/path.name
            self._log.debug(f'moving {path} to {dest}')
            shutil.move(path, dest)


class InotifyWatcher:
    def __init__(self, torrent_dir: Path, aria2: Aria2Client, downloads: DownloadManager) -> None:
        self._torrent_dir = torrent_dir
        self._aria2 = aria2
        self._downloads = downloads
        self._log = logging.getLogger('inotify')

    def start(self) -> asyncio.Task:
        return asyncio.create_task(self._run(), name='inotify-watcher')

    async def _run(self) -> None:
        try:
            self._log.info('catching up')
            await self._catch_up()
            self._log.info('starting up')
            with Inotify() as inotify:
                inotify.add_watch(self._torrent_dir, Mask.CLOSE_WRITE)
                async for event in inotify:
                    await self._step(event)
        except Exception as e:
            self._log.error('caught exception')
            self._log.exception(e)
            raise

    async def _catch_up(self) -> None:
        for path in self._torrent_dir.glob('**/*.torrent'):
            await self._submit_file(path)

    async def _step(self, event) -> None:
        self._log.debug(f'received {event}')
        if not event.watch or not event.name or not event.name.suffix == '.torrent':
            return
        path = event.watch.path/event.name
        await self._submit_file(path)

    async def _submit_file(self, path: Path) -> None:
        self._log.info(f'adding torrent {path} to download queue')
        gid = await self._aria2.add_torrent(path.read_bytes())
        self._downloads.track(gid)
        path.unlink()


asyncio.run(main())
