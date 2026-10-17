#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 125hz
# Madeira Converter Exception: see LICENSE-EXCEPTION.md
"""Host checks for the owned Steam library and downloads (docs/STEAM_LIBRARY.md).

Never contacts Steam. It needs swiftc, cc, Python 3.14 with `cryptography` or the
openssl command. macOS uses Apple's SDK; Linux needs libssl/liblzma/zlib headers.

Part A is static: licence headers, the Xcode project, no program names, the
only token path (SteamSignIn's Keychain item), no account data in a log line, no
engine or pool change, Info.plist permitting only the background download task,
and that the sources do not say they are
ports of another project.

Part B builds the production Swift (protocol messages, message codec, app-info
parsing, library fetcher, depot downloader, content decryptor, install record
writer, playtime) plus the production C decoders (liblzma shim, zstd educational
decoder, zip chunks) into one AddressSanitizer executable, with three small
stand-ins for Apple frameworks (CommonCrypto on OpenSSL, Compression, zlib) and a
scripted Steam connection (`SteamCMSession`), and runs it:

* units: the message and manifest formats, hostile input, the decrypt and
  decompress pipeline on known vectors, PICS parsing through the fetcher, and
  the install record;
* installs: the whole download against local HTTP content servers built here;
  a host-only URLProtocol bridges declared HTTPS fixture origins to those servers
  and blocks every undeclared origin (three servers: one dead, one that corrupts the first answer for
  each chunk, one healthy; every chunk container Steam serves, AES with the depot
  key, encrypted file names, a shared depot, a depot the account does not own): an
  interrupted install resumes without fetching finished chunks, is not listed as
  installed until its record is written, and once written is found by Madeira
  Dock's own scanner (MadeiraDock.games); an update fetches only what changed and
  shrinks files; hostile manifest paths never leave the install folder.
"""
from pathlib import Path
import base64
import hashlib
import http.server
import io
import json
import lzma
import os
import random
import re
import shutil
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile
import zlib
from urllib.parse import urlsplit

root = Path(__file__).resolve().parents[2]
app = root / 'app/Madeira'
steam = app / 'SwiftSteam'
SWIFTC = os.environ.get('SWIFTC') or shutil.which('swiftc') or str(Path.home() / '.local/share/swiftly/bin/swiftc')
CC = os.environ.get('CC') or shutil.which('cc') or 'cc'
failures = 0


def require(condition, label):
    global failures
    print(('PASS: ' if condition else 'FAIL: ') + label)
    if not condition:
        failures += 1


def block(source, start_marker):
    start = source.index(start_marker)
    depth, i = 0, source.index('{', start)
    while True:
        c = source[i]
        if c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0: return source[start:i + 1] + '\n'
        i += 1


# ---------------------------------------------------------------- Part A
library_swift = [
    'SteamInstall.swift', 'SteamOwnedLibrary.swift', 'SteamDownloadBackground.swift', 'SteamGames.swift',
    'SwiftSteam/Core/CMServerList.swift', 'SwiftSteam/Core/LicenseListBox.swift', 'SwiftSteam/Core/SteamCMSession.swift',
    'SwiftSteam/Core/SteamConnection.swift', 'SwiftSteam/Core/SteamMessageCodec.swift', 'SwiftSteam/Core/SteamProtocol.swift',
    'SwiftSteam/Core/SteamSession.swift', 'SwiftSteam/Content/ContentDecryptor.swift', 'SwiftSteam/Content/DepotDownloader.swift',
    'SwiftSteam/Content/SteamDownloadSources.swift',
    'SwiftSteam/Content/DepotManifest.swift', 'SwiftSteam/Library/SteamAppInfo.swift',
    'SwiftSteam/Library/SteamLibraryFetcher.swift', 'SwiftSteam/Install/AppManifestWriter.swift']
c_files = ['SwiftSteam/chunk_zip.c', 'SwiftSteam/chunk_zip.h', 'SwiftSteam/lzma_shim.c', 'SwiftSteam/lzma_shim.h',
           'SwiftSteam/zstd_edu.c', 'SwiftSteam/zstd_edu.h']
project = (root / 'app/Madeira.xcodeproj/project.pbxproj').read_text()
sources = {p: (app / p).read_text() for p in library_swift}
for path, text in sources.items():
    head = text.split('\n', 3)
    require(head[0] == '// SPDX-License-Identifier: GPL-3.0-or-later' and head[1].startswith('// Copyright 2026 ')
            and head[2] == '// Madeira Converter Exception: see LICENSE-EXCEPTION.md', f'{path}: licence header')
    require(f'/* {path} in Sources */' in project, f'{path} is built by the Xcode project')
    if 'Jfishin' in head[1]:
        require("Jfishin's Madeira Steam client" in text.split('import ', 1)[0], f'{path} credits Jfishin')
for path in c_files:
    require(re.search(r'\bpath\s*=\s*(?:"' + re.escape(path) + r'"|' + re.escape(path) + r')\s*;', project),
            f'{path} is in the Xcode project')
    if path.endswith('.c'):
        require(f'/* {path} in Sources */' in project, f'{path} is built by the Xcode project')
require('liblzma.tbd in Frameworks' in project, 'liblzma is linked (the system library; nothing is bundled)')
bridging = (app / 'Madeira-Bridging-Header.h').read_text()
require('#import "SwiftSteam/lzma_shim.h"' in bridging, 'the bridging header imports the decoders')
zstd = (steam / 'zstd_edu.c').read_text()
require('Copyright (c) Meta Platforms, Inc. and affiliates.' in zstd.split('\n', 3)[1] + zstd.split('\n', 3)[2] and
        'BSD-style license' in zstd[:600], "zstd_edu.c keeps Meta's notice")
require((root / 'LICENSES/ZSTD-BSD.txt').is_file(), 'the BSD licence text of the zstd decoder is in LICENSES/')
notices = (root / 'THIRD-PARTY-NOTICES.md').read_text()
require('zstd_edu.c' in notices and 'ZSTD-BSD.txt' in notices, 'THIRD-PARTY-NOTICES.md lists the zstd decoder and its licence text')

# The token path: SteamSignIn's Keychain item is the only one.
for path in ['SteamOwnedLibrary.swift', 'SteamGames.swift', 'SteamInstall.swift', 'SteamDownloadBackground.swift',
             'SwiftSteam/Core/SteamSession.swift', 'SwiftSteam/Core/SteamConnection.swift',
             'SwiftSteam/Content/DepotDownloader.swift', 'SwiftSteam/Library/SteamLibraryFetcher.swift']:
    text = sources[path]
    for word in ['SteamTokenStore', 'SecItem', 'kSecClass', 'saveTokens', 'clearTokens', 'loadTokens', 'UserDefaults', 'refreshToken']:
        if path == 'SwiftSteam/Core/SteamSession.swift' and word == 'refreshToken':
            continue  # it logs on with the token SteamSignIn hands out
        if path == 'SteamGames.swift' and word == 'UserDefaults':
            continue
        require(word not in text, f'{path}: no {word} (sign-in tokens stay in SteamSignIn)')
require('SteamSignIn.credentialsForDock()' in sources['SwiftSteam/Core/SteamSession.swift'],
        "the connection logs on with SteamSignIn's stored sign-in")
# No program names, no engine or pool code, no Windows client code.
for path in library_swift:
    text = sources[path]
    require(not re.findall(r'"[^"\n]*\.exe"', text), f'{path}: no program names')
    for word in ['helperNames', 'steamwebhelper', 'steam.exe', 'setenv(', 'unsetenv(', 'runWineFullSequence', 'MADEIRA_EXE',
                 'MADEIRA_ARGS', 'jit_', 'JITPool', 'poolSize', 'FEX_', 'DXMT', 'BGTaskScheduler', 'BGContinuedProcessing',
                 'UNUserNotificationCenter', 'SteamKit2', 'JavaSteam', 'DepotDownloader\'s', 'port of the DepotDownloader']:
        # Background downloads and their notifications live in one file (SteamDownloadBackground.swift).
        if path == 'SteamDownloadBackground.swift' and word in ('BGTaskScheduler', 'BGContinuedProcessing', 'UNUserNotificationCenter'):
            continue
        require(word not in text, f'{path}: no {word}')
    require(not re.search(r'\bml\d{3,4}\b', text), f'{path}: no build-round labels')
    for line in text.splitlines():
        if re.search(r'SteamLog\.(event|trace)|LogStore|print\(|NSLog|fputs', line):
            bad = re.search(r'\\\((account|accountName|name|token|password|signIn|refresh|steamID|steamid|path|folder)\b', line)
            require(bad is None, f'{path}: no account data or path in "{line.strip()[:70]}"')
require('MadeiraConfig' not in sources['SteamOwnedLibrary.swift'] and 'SteamSignIn.flag("MADEIRA_STEAM_LIBRARY", default: true)' in sources['SteamOwnedLibrary.swift'],
        'one switch, MADEIRA_STEAM_LIBRARY, on by default')
info = (app / 'Info.plist').read_text()
permitted = re.findall(r'<key>BGTaskSchedulerPermittedIdentifiers</key>\s*<array>(.*?)</array>', info, re.S)
require(len(permitted) == 1 and re.findall(r'<string>([^<]*)</string>', permitted[0])
        == ['$(PRODUCT_BUNDLE_IDENTIFIER).download.*', '$(PRODUCT_BUNDLE_IDENTIFIER).pairing.*'],
        "Info.plist permits only the bundle's own .download.* (background downloads) and .pairing.* (JIT pairing) tasks")
require('UIBackgroundModes' not in info, 'Info.plist asks for no background mode')
background = sources['SteamDownloadBackground.swift']
require('hasSuffix(".download.*")' in background and '+ "queue"' in background and 'BGContinuedProcessingTaskRequest(identifier: identifier' in background,
        'the continued-processing task uses the permitted identifier, made concrete')
require('SteamSignIn.flag("MADEIRA_BACKGROUND_DOWNLOADS", default: true)' in background and
        'SteamSignIn.flag("MADEIRA_DOWNLOAD_NOTIFICATIONS", default: true)' in background,
        'background downloads and notifications each have a switch, on by default')
require('if #available(iOS 26.0, *), Self.continuedEnabled { submitContinued() }' in background and 'beginBackgroundTask(' in background
        and 'pauseForBackground()' in background, 'iOS 26 continues in the background; otherwise the grace period ends in a clean pause')
owned_text = sources['SteamOwnedLibrary.swift']
require('SteamDownloadBackground.shared.downloadStarted(appID: appID' in owned_text and 'SteamDownloadBackground.shared.progress(progress)' in owned_text
        and 'outcome: outcome, queueEmpty: queue.isEmpty)' in owned_text, 'every download reports its start, progress and outcome')
content_view = (app / 'ContentView.swift').read_text()
game_work = (app / 'GameSessionWork.swift').read_text()
require(game_work.count('SteamOwnedLibrary.shared.sessionChanged(active: paused)') == 1 and
        'GameSessionWork.shared.reconcile()' in content_view,
        'the shared session coordinator pauses Steam through its existing session transition (static integration boundary)')
# Madeira Dock: the app's own connection is logged off (socket closed) before the one-use sign-in
# is written, and stays off until the Dock session ended (the ordering the gate test above runs).
dock_start = block(content_view, 'private func startDock(')
order = [dock_start.find(s) for s in ('await SteamOwnedLibrary.shared.prepareDock()', 'SteamSignIn.credentialsForDock()',
                                      'try MadeiraDock.writeHandoff(', 'runWineFullSequence(profile: profile, preparation: preparation)')]
require(-1 not in order and order == sorted(order),
        'Dock start: the app connection closes, then the sign-in is read and handed over, then the session starts')
require(dock_start.count('SteamSignIn.credentialsForDock()') == 1 and dock_start.count('MadeiraDock.writeHandoff(') == 1,
        'the sign-in is read and written once, after the connection closed')
require('SteamOwnedLibrary.shared.dockEnded()' in block(dock_start, 'func fail('),
        'a failed Dock start gives the connection back')
dock_view = (app / 'MadeiraDockView.swift').read_text()
watch = block(dock_view, 'func watchReport()')
require(watch.index('MadeiraDock.cleanup()') < watch.index('SteamOwnedLibrary.shared.dockEnded()'),
        "the connection may come back only when the Dock host's report or session is over")
owned_model = sources['SteamOwnedLibrary.swift']
prepare = block(owned_model, 'func prepareDock() async')
require(prepare.index('sessionChanged(active: true)') < prepare.index('await running?.value') < prepare.index('await gate.holdForDock()'),
        'prepareDock pauses downloads, waits for the running one, then waits for the connection to close')
changed = block(owned_model, 'func sessionChanged(active running: Bool)')
require('guard gate.open || gate.reopen(sessionRunning: false) else { return }' in changed and 'gate.close()' in changed
        and 'session.resume()' not in changed and 'session.suspend()' not in changed,
        'a session ending reopens the connection only through the gate (never while Dock holds the account)')
session_source = sources['SwiftSteam/Core/SteamSession.swift']
suspend = block(session_source, 'func suspend() async')
require(suspend.index('isSuspended = true') < suspend.index('await disconnectGracefully()') < suspend.index('await connection.disconnect()'),
        'suspend: no reconnect from here on, log off, then wait for the socket to close')
require('if self.isSuspended {' in session_source and 'eMsg: .clientLogOff' in session_source[session_source.index('if self.isSuspended {'):],
        'a logon that completes after suspend() logs straight off again')
runtime = (app / 'SteamRuntime.swift').read_text()
relative_root = re.search(r'static let relativeRoot = "([^"]+)"', runtime).group(1)
require(sources['SteamInstall.swift'].count(f'"{relative_root}/steamapps"') == 1,
        "downloads go into Madeira Dock's own Steam library folder (SteamRuntimeFiles.relativeRoot + /steamapps)")
downloader = sources['SwiftSteam/Content/DepotDownloader.swift']
require('SteamLog.event("[steam-depot] install begin' in downloader and 'depot-key refused' in downloader, 'the download logs App IDs and depot IDs')
require(not re.search(r'LibraryFlags|MADEIRA_STEAM_[A-Z_]+', downloader), 'the downloader has no switches of its own')
dock = (app / 'MadeiraDock.swift').read_text()
require('static func validate(_ game: DockGame' in dock and 'game.installed' in dock, "Dock's validation reads the install record (StateFlags)")

if failures:
    print(f'check-steam-library: {failures} FAILED (static)')
    sys.exit(1)

# ---------------------------------------------------------------- fixtures (Python side)
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    def aes_ecb(key, data, decrypt=False):
        c = Cipher(algorithms.AES(key), modes.ECB())
        op = c.decryptor() if decrypt else c.encryptor()
        return op.update(data) + op.finalize()
    def aes_cbc_encrypt(key, iv, data):
        e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return e.update(data) + e.finalize()
except ImportError:
    def _openssl(args, data):
        return subprocess.run(['openssl', 'enc'] + args, input=data, capture_output=True, check=True, timeout=30).stdout
    def aes_ecb(key, data, decrypt=False):
        return _openssl(['-aes-256-ecb', '-nopad', '-K', key.hex()] + (['-d'] if decrypt else []), data)
    def aes_cbc_encrypt(key, iv, data):
        return _openssl(['-aes-256-cbc', '-nopad', '-K', key.hex(), '-iv', iv.hex()], data)

rng = random.Random(20260928)


def pkcs7(data):
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def steam_encrypt(key, data, pad_nul=False):
    """Steam's symmetric scheme: the IV encrypted with the key in ECB mode, then AES-256-CBC (PKCS7) under that IV."""
    iv = rng.randbytes(16)
    return aes_ecb(key, iv) + aes_cbc_encrypt(key, iv, pkcs7(data))


def container(kind, data):
    """One of Steam's chunk containers around already-final bytes."""
    if kind == 'zstd':
        from compression import zstd
        frame = zstd.compress(data)
        # header: magic + crc32; footer (15 bytes): crc32 + decompressed size + 4 bytes + 'zsv'
        return b'VSZa' + struct.pack('<I', zlib.crc32(data)) + frame + struct.pack('<II', zlib.crc32(data), len(data)) + bytes(4) + b'zsv'
    if kind == 'lzma':
        raw = lzma.compress(data, format=lzma.FORMAT_ALONE, filters=[{'id': lzma.FILTER_LZMA1, 'dict_size': 1 << 20, 'lc': 3, 'lp': 0, 'pb': 2}])
        props, stream = raw[:5], raw[13:]
        return b'VZa' + struct.pack('<I', zlib.crc32(data)) + props + stream + struct.pack('<II', zlib.crc32(data), len(data)) + b'zv'
    if kind == 'zip':
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('z', data)
        return buffer.getvalue()
    raise ValueError(kind)


def adler(data):
    return zlib.adler32(data, 0) & 0xffffffff


def varint(v):
    out = bytearray()
    while v > 0x7f:
        out.append((v & 0x7f) | 0x80); v >>= 7
    out.append(v)
    return bytes(out)


def field(number, wire, payload):
    return varint(number << 3 | wire) + payload


def f_varint(n, v): return field(n, 0, varint(v))
def f_bytes(n, b): return field(n, 2, varint(len(b)) + b)
def f_fixed32(n, v): return field(n, 5, struct.pack('<I', v))


class Depot:
    """A synthetic depot: files, chunks (compressed, encrypted, checksummed) and the manifest that lists them."""
    def __init__(self, depot_id, key, entries, encrypt_names=True):
        self.id, self.key, self.entries = depot_id, key, entries
        self.chunks = {}     # sha hex -> served bytes
        self.files = []      # manifest order: dict(name, flags, size, chunks=[(sha, offset, ulen, clen, adler)], data)
        for entry in entries:
            name, flags = entry['name'], entry.get('flags', 0)
            data, pieces = entry.get('data', b''), []
            offset = 0
            for kind, length in entry.get('chunks', []):
                part = data[offset:offset + length]
                served = steam_encrypt(key, container(kind, part))
                sha = hashlib.sha1(part).digest()
                self.chunks[sha.hex()] = served
                pieces.append((sha, offset, len(part), len(served), adler(part)))
                offset += length
            self.files.append(dict(name=name, flags=flags, size=len(data), chunks=pieces, data=data))
        self.encrypt_names = encrypt_names

    def payload(self):
        out = b''
        for f in self.files:
            name = f['name'].encode()
            shown = base64.b64encode(steam_encrypt(self.key, name + b'\0')) if self.encrypt_names else name
            body = f_bytes(1, shown) + f_varint(2, f['size']) + f_varint(3, f['flags'])
            for sha, offset, ulen, clen, crc in f['chunks']:
                body += f_bytes(6, f_bytes(1, sha) + f_fixed32(2, crc) + f_varint(3, offset) + f_varint(4, ulen) + f_varint(5, clen))
            out += f_bytes(1, body)
        return out

    def manifest_zip(self):
        payload = self.payload()
        meta = f_varint(1, self.id)
        binary = (struct.pack('<II', 0x71F617D0, len(payload)) + payload + struct.pack('<II', 0x1F4812BE, len(meta)) + meta +
                  struct.pack('<II', 0x1B81B817, 0) + struct.pack('<I', 0x32C415AB))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('z', binary)
        return buffer.getvalue(), binary

    def expected(self):
        """Path (as the downloader must fold it) -> content, for accepted regular files."""
        return {f['name'].replace('\\', '/'): f['data'] for f in self.files
                if f['flags'] & 0x40 == 0 and f['flags'] & 0x200 == 0 and f['chunks'] is not None}


def data(n, seed):
    r = random.Random(seed)
    a = n // 3
    return r.randbytes(a) + bytes(a) + (b'ab' * n)[:n - 2 * a]


KEY_MAIN, KEY_SHARED = rng.randbytes(32), rng.randbytes(32)
GAME_BIN = data(153333, 1)
ASSETS = data(82345, 2)
README = data(9000, 3)
NOTES = data(4321, 4)
CUSTOM = data(20000, 5)
EVIL = b'must never be written'


def main_depot(version):
    """Depot 9001. Version 2 changes one chunk of game.bin and shrinks the assets file."""
    game = GAME_BIN if version == 1 else GAME_BIN[:60000] + data(60000, 99) + GAME_BIN[120000:]
    assets = ASSETS if version == 1 else ASSETS[:70000]
    return Depot(9001, KEY_MAIN, [
        dict(name='bin', flags=0x40),
        dict(name='bin/game.bin', data=game, chunks=[('zstd', 60000), ('lzma', 60000), ('zip', 33333)]),
        dict(name='data\\Assets.dat', data=assets, chunks=[('lzma', 70000)] + ([('zstd', 12345)] if version == 1 else [])),
        dict(name='empty.txt', data=b''),
        dict(name='docs/Readme.TXT', data=README, chunks=[('zip', 9000)]),
        dict(name='Docs/notes.txt', data=NOTES, chunks=[('zstd', 4321)]),
        dict(name='..\\evil.txt', data=EVIL, chunks=[('zip', len(EVIL))]),
        dict(name='link', flags=0x200),
        dict(name='tool/custom.bin', flags=0x80, data=CUSTOM, chunks=[('lzma', 20000)]),
    ])


def shared_depot():
    return Depot(9003, KEY_SHARED, [dict(name='shared/lib.dat', data=data(30000, 6), chunks=[('zip', 30000)])])


# Known vectors for the decoders (independent of the depot above).
VEC_PLAIN = data(50000, 7)
VEC = {kind: steam_encrypt(KEY_MAIN, container(kind, VEC_PLAIN)).hex() for kind in ['zstd', 'lzma', 'zip']}
RAW = steam_encrypt(KEY_MAIN, VEC_PLAIN[:1000]).hex()


class ContentServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, mode, state):
        super().__init__(('127.0.0.1', 0), Handler)
        self.mode, self.state = mode, state
        self.corrupted = set()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_address[1]}'

    @property
    def secure_url(self):
        return f'https://{self.mode}.fixture.steamcontent.com'


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.depots = {}          # depot id -> Depot
        self.manifests = {}       # (depot id, gid) -> zip bytes
        self.request_code = 424242
        self.auth = 'tok'
        self.limit = None         # chunks served OK before every chunk request fails
        self.served_ok = []       # (host mode, sha hex)
        self.chunk_requests = []  # sha hex, every request
        self.manifest_requests = []
        self.chunk_origins = []   # (declared original origin, sha), including failed probes
        self.extra_origins = {}   # declared production origins mapped to this fixture's loopback servers
        self.chunk_refusals = []  # regional HTTP nodes that deliberately refuse chunks
        self.denied = 0

    def reset_counters(self):
        with self.lock:
            self.served_ok, self.chunk_requests, self.manifest_requests, self.chunk_origins, self.denied = [], [], [], [], 0
            self.chunk_refusals = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    def log_message(self, *args): pass

    def reply(self, status, body=b''):
        self.send_response(status); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        server, state = self.server, self.server.state
        path, _, query = self.path.partition('?')
        parts = path.strip('/').split('/')
        try:
            depot = int(parts[1])
            origin = self.headers.get('X-Madeira-Fixture-Origin', '')
            allowed = origin in (server.url, server.secure_url) or state.extra_origins.get(origin) == server.url
            secure = allowed and origin.startswith('https://')
            token = f'{state.auth}-{urlsplit(origin).hostname}-{depot}'
            if not allowed or (secure and query != f'auth={token}') or (not secure and query):
                with state.lock: state.denied += 1
                return self.reply(403)
            if parts[2] == 'manifest':
                if not secure:
                    with state.lock: state.denied += 1
                    return self.reply(403)
                gid, code = int(parts[3]), parts[5] if len(parts) > 5 else ''
                with state.lock: state.manifest_requests.append((origin, depot, gid))
                if parts[4] != '5' or code != str(state.request_code) or (depot, gid) not in state.manifests:
                    return self.reply(404)
                if server.mode == 'dead': return self.reply(503)
                return self.reply(200, state.manifests[(depot, gid)])
            if parts[2] == 'chunk':
                sha = parts[3]
                with state.lock:
                    state.chunk_requests.append(sha)
                    state.chunk_origins.append((origin, sha))
                    if server.mode == 'dead': return self.reply(503)
                    if server.mode == 'forbidden':
                        state.chunk_refusals.append((origin, sha))
                        return self.reply(403)
                    if state.limit is not None and len(state.served_ok) >= state.limit:
                        return self.reply(404)
                blob = state.depots[depot].chunks.get(sha)
                if blob is None:
                    return self.reply(404)
                if server.mode == 'flaky' and (origin, sha) not in server.corrupted:
                    server.corrupted.add((origin, sha))
                    blob = blob[:40] + bytes([blob[40] ^ 0xff]) + blob[41:]
                    return self.reply(200, blob)
                with state.lock: state.served_ok.append((server.mode, sha))
                return self.reply(200, blob)
        except (ValueError, IndexError, KeyError):
            pass
        self.reply(404)


def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------- Swift harness
CRYPTO_H = r'''
#ifndef cc_shim_h
#define cc_shim_h
#include <stddef.h>
#include <stdint.h>
typedef uint32_t CCOperation;
typedef uint32_t CCAlgorithm;
typedef uint32_t CCOptions;
typedef int32_t CCCryptorStatus;
typedef uint32_t CC_LONG;
enum { kCCEncrypt = 0, kCCDecrypt = 1 };
enum { kCCAlgorithmAES = 0 };
enum { kCCOptionPKCS7Padding = 1, kCCOptionECBMode = 2 };
enum { kCCSuccess = 0, kCCParamError = -4300, kCCBufferTooSmall = -4301, kCCDecodeError = -4304 };
enum { kCCBlockSizeAES128 = 16 };
#define CC_SHA1_DIGEST_LENGTH 20
CCCryptorStatus CCCrypt(CCOperation op, CCAlgorithm alg, CCOptions options, const void *key, size_t keyLength,
                        const void *iv, const void *dataIn, size_t dataInLength, void *dataOut,
                        size_t dataOutAvailable, size_t *dataOutMoved);
unsigned char *CC_SHA1(const void *data, CC_LONG len, unsigned char *md);
#endif
'''
CRYPTO_C = r'''
#include "cc_shim.h"
#include <openssl/evp.h>
#include <openssl/sha.h>
#include <string.h>
#include <stdlib.h>
CCCryptorStatus CCCrypt(CCOperation op, CCAlgorithm alg, CCOptions options, const void *key, size_t keyLength,
                        const void *iv, const void *dataIn, size_t dataInLength, void *dataOut,
                        size_t dataOutAvailable, size_t *dataOutMoved) {
    if (alg != kCCAlgorithmAES || keyLength != 32) return kCCParamError;
    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    const EVP_CIPHER *cipher = (options & kCCOptionECBMode) ? EVP_aes_256_ecb() : EVP_aes_256_cbc();
    if (!EVP_CipherInit_ex(ctx, cipher, NULL, key, (options & kCCOptionECBMode) ? NULL : iv, op == kCCEncrypt)) { EVP_CIPHER_CTX_free(ctx); return kCCParamError; }
    EVP_CIPHER_CTX_set_padding(ctx, (options & kCCOptionPKCS7Padding) ? 1 : 0);
    unsigned char *tmp = malloc(dataInLength + 32);
    int n1 = 0, n2 = 0;
    int ok = EVP_CipherUpdate(ctx, tmp, &n1, dataIn, (int)dataInLength) && EVP_CipherFinal_ex(ctx, tmp + n1, &n2);
    EVP_CIPHER_CTX_free(ctx);
    if (!ok) { free(tmp); return kCCDecodeError; }
    if ((size_t)(n1 + n2) > dataOutAvailable) { free(tmp); return kCCBufferTooSmall; }
    memcpy(dataOut, tmp, n1 + n2); *dataOutMoved = n1 + n2; free(tmp);
    return kCCSuccess;
}
unsigned char *CC_SHA1(const void *data, CC_LONG len, unsigned char *md) { return SHA1(data, len, md); }
'''
COMPRESSION_H = r'''
#ifndef compression_shim_h
#define compression_shim_h
#include <stddef.h>
#include <stdint.h>
typedef enum : uint32_t { COMPRESSION_LZMA = 0x306, COMPRESSION_ZLIB = 0x205 } compression_algorithm;
size_t compression_decode_buffer(uint8_t *dst, size_t dst_size, const uint8_t *src, size_t src_size, void *scratch, compression_algorithm algorithm);
#endif
'''
COMPRESSION_C = r'''
#include "compression_shim.h"
/* Not needed on the host: the bare-LZMA fallback is reported as not decoded. */
size_t compression_decode_buffer(uint8_t *dst, size_t dst_size, const uint8_t *src, size_t src_size, void *scratch, compression_algorithm algorithm) { return 0; }
'''
DECODERS_H = '#include "{app}/SwiftSteam/lzma_shim.h"\n'

STUBS = r'''
import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

final class FixtureEventLog: @unchecked Sendable {
    private let lock = NSLock()
    private var events: [String] = []
    func append(_ event: String) {
        lock.lock(); defer { lock.unlock() }
        events.append(event)
    }
    func joined(separator: String) -> String {
        lock.lock(); defer { lock.unlock() }
        return events.joined(separator: separator)
    }
}
let loggedEvents = FixtureEventLog()
enum SteamLog {
    static func trace(_ m: @autoclosure () -> String) {}
    static func event(_ m: String) { loggedEvents.append(m) }
}
enum SteamSignIn {
    static func flag(_ name: String, default fallback: Bool) -> Bool { getenv(name).map { String(cString: $0) != "0" } ?? fallback }
}
enum SteamRuntimeFiles {
    static let relativeRoot = "RELATIVE_ROOT"
    static let windowsRoot = "C:\\Program Files (x86)\\Steam"
}
enum SteamOwnedLibrary {
    REASON
}
'''

CHECKS = r'''
import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import CommonCrypto
import zlib

nonisolated(unsafe) var failures = 0
func require(_ condition: @autoclosure () -> Bool, _ label: String) {
    if condition() { print("PASS: " + label) } else { print("FAIL: " + label); failures += 1 }
}
func hexData(_ s: String) -> Data {
    var data = Data(); var i = s.startIndex
    while i < s.endIndex { let j = s.index(i, offsetBy: 2); data.append(UInt8(s[i..<j], radix: 16)!); i = j }
    return data
}
func le(_ v: UInt32) -> [UInt8] { [UInt8(v & 0xff), UInt8(v >> 8 & 0xff), UInt8(v >> 16 & 0xff), UInt8(v >> 24)] }

/// Only the transport is doubled: production URL construction, authorization,
/// manifest decoding and encrypted chunk validation run without substitutions.
/// Every URL in the downloader's two sessions is intercepted, including live
/// fallback origins, which fail closed before DNS or a socket can be opened.
final class FixtureContentProtocol: URLProtocol {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var origins: [String: String] = [:]
    nonisolated(unsafe) private static var blocked = 0
    nonisolated(unsafe) private static var sawChecking = false
    nonisolated(unsafe) private static var currentPhase = "preparing"
    nonisolated(unsafe) private static var chunksBeforeChecking = 0
    nonisolated(unsafe) private static var chunksOutsideDownloading = 0
    nonisolated(unsafe) static var directoryCacheURL = URL(fileURLWithPath: "/unused-fixture-directory-cache")
    private var fixtureTask: URLSessionDataTask?
    private var bridge: URLSession?

    static func configure(_ mapping: [String: String], cacheURL: URL) {
        lock.lock(); defer { lock.unlock() }
        origins = mapping; blocked = 0
        sawChecking = false; currentPhase = "preparing"
        chunksBeforeChecking = 0; chunksOutsideDownloading = 0
        directoryCacheURL = cacheURL
    }
    static func progressPhase(_ phase: String) {
        lock.lock(); defer { lock.unlock() }
        currentPhase = phase
        if phase == "checking" { sawChecking = true }
    }
    static var prematureChunkCount: Int { lock.lock(); defer { lock.unlock() }; return chunksBeforeChecking }
    static var wrongPhaseChunkCount: Int { lock.lock(); defer { lock.unlock() }; return chunksOutsideDownloading }
    private static func target(_ url: URL) -> (String, URL)? {
        lock.lock(); defer { lock.unlock() }
        guard var parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.user == nil, parts.password == nil, parts.fragment == nil else { blocked += 1; return nil }
        parts.path = ""; parts.query = nil
        guard let origin = parts.string, let local = origins[origin],
              let base = URLComponents(string: local), base.scheme == "http", base.host == "127.0.0.1",
              let port = base.port, port > 0 else { blocked += 1; return nil }
        var routed = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        routed.scheme = base.scheme; routed.host = base.host; routed.port = port
        guard let target = routed.url else { blocked += 1; return nil }
        return (origin, target)
    }
    static var blockedCount: Int { lock.lock(); defer { lock.unlock() }; return blocked }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let original = request.url, let (origin, local) = Self.target(original) else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL)); return
        }
        if original.path.contains("/chunk/") {
            Self.lock.lock()
            if !Self.sawChecking { Self.chunksBeforeChecking += 1 }
            if Self.currentPhase != "downloading" { Self.chunksOutsideDownloading += 1 }
            Self.lock.unlock()
        }
        var forwarded = request
        forwarded.url = local
        forwarded.setValue(origin, forHTTPHeaderField: "X-Madeira-Fixture-Origin")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [] // only the validated loopback bridge reaches native transport
        configuration.connectionProxyDictionary = [:]
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 5
        configuration.timeoutIntervalForResource = 10
        let session = URLSession(configuration: configuration, delegate: ContentRedirectPolicy(), delegateQueue: nil)
        bridge = session
        fixtureTask = session.dataTask(with: forwarded) { [weak self] data, response, error in
            guard let self else { return }
            defer { session.finishTasksAndInvalidate() }
            if let error { self.client?.urlProtocol(self, didFailWithError: error); return }
            guard let response = response as? HTTPURLResponse,
                  let originalResponse = HTTPURLResponse(url: original, statusCode: response.statusCode,
                    httpVersion: "HTTP/1.0", headerFields: response.allHeaderFields.reduce(into: [String: String]()) {
                        $0[String(describing: $1.key)] = String(describing: $1.value)
                    }) else { self.client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse)); return }
            self.client?.urlProtocol(self, didReceive: originalResponse, cacheStoragePolicy: .notAllowed)
            if let data { self.client?.urlProtocol(self, didLoad: data) }
            self.client?.urlProtocolDidFinishLoading(self)
        }
        fixtureTask?.resume()
    }
    override func stopLoading() { fixtureTask?.cancel(); bridge?.invalidateAndCancel() }
}

/// A logged-on connection whose answers are scripted from the fixture.
@MainActor final class ScriptedSession: SteamCMSession {
    var cellID: UInt32 = 7
    var steamID: UInt64 = 76561197960265728 + 4242
    var licenses: [UInt32] = []
    var packageBuffers: [UInt32: Data] = [:]
    var appInfo: [UInt32: String] = [:]
    var keys: [UInt32: Data] = [:]
    var refused: Set<UInt32> = []
    var requestCode: UInt64 = 0
    var authToken = ""
    var keyRequests: [(depot: UInt32, app: UInt32)] = []
    var codeRequests: [(app: UInt32, depot: UInt32, gid: UInt64)] = []
    var authRequests: [(depot: UInt32, host: String, app: UInt32)] = []
    var picsAppRequests: [[UInt32]] = []
    var directoryHosts: [String]?
    var directoryRequests: [(cell: UInt32, ip: String?)] = []
    var connects = 0

    func ensureConnected() async throws { connects += 1 }
    func awaitLicenseList(timeout: TimeInterval) async throws -> [UInt32] { licenses }

    private func message(_ body: Data) -> SteamMessageCodec.IncomingMessage {
        SteamMessageCodec.IncomingMessage(eMsg: .multi, rawEMsg: 0, isProtobuf: true, header: CMsgProtoBufHeader(), body: body)
    }
    private func ids(_ data: Data, field: UInt32, sub: UInt32) throws -> [UInt32] {
        var d = ProtobufDecoder(data); var out: [UInt32] = []
        while let tag = try d.readTag() {
            if tag.fieldNumber == field, tag.wireType == .lengthDelimited {
                var s = ProtobufDecoder(try d.readBytes())
                while let t = try s.readTag() {
                    if t.fieldNumber == sub, t.wireType == .varint { out.append(UInt32(truncatingIfNeeded: try s.readVarint())) } else { try s.skip(wireType: t.wireType) }
                }
            } else { try d.skip(wireType: tag.wireType) }
        }
        return out
    }
    func sendAndWait(eMsg: EMsg, body: Data, responseEMsg: EMsg, timeout: TimeInterval) async throws -> SteamMessageCodec.IncomingMessage {
        var out = ProtobufEncoder()
        switch eMsg {
        case .clientPICSAccessTokenRequest:
            var d = ProtobufDecoder(body)
            var packages: [UInt32] = [], apps: [UInt32] = []
            while let tag = try d.readTag() {
                if tag.fieldNumber == 1 { packages.append(UInt32(truncatingIfNeeded: try d.readVarint())) }
                else if tag.fieldNumber == 2 { apps.append(UInt32(truncatingIfNeeded: try d.readVarint())) }
                else { try d.skip(wireType: tag.wireType) }
            }
            for p in packages { var t = ProtobufEncoder(); t.writeUInt32(fieldNumber: 1, value: p); t.writeUInt64(fieldNumber: 2, value: 1000 + UInt64(p)); out.writeSubmessage(fieldNumber: 1, value: t.data) }
            for a in apps { var t = ProtobufEncoder(); t.writeUInt32(fieldNumber: 1, value: a); t.writeUInt64(fieldNumber: 2, value: 2000 + UInt64(a)); out.writeSubmessage(fieldNumber: 3, value: t.data) }
        case .clientGetDepotDecryptionKey:
            var d = ProtobufDecoder(body); var depot: UInt32 = 0, app: UInt32 = 0
            while let tag = try d.readTag() {
                if tag.fieldNumber == 1 { depot = UInt32(truncatingIfNeeded: try d.readVarint()) }
                else if tag.fieldNumber == 2 { app = UInt32(truncatingIfNeeded: try d.readVarint()) }
                else { try d.skip(wireType: tag.wireType) }
            }
            keyRequests.append((depot, app))
            if let key = keys[depot], !refused.contains(depot) {
                out.writeInt32(fieldNumber: 1, value: 1); out.writeUInt32(fieldNumber: 2, value: depot); out.writeBytes(fieldNumber: 3, value: key)
            } else {
                out.writeInt32(fieldNumber: 1, value: 15); out.writeUInt32(fieldNumber: 2, value: depot)
            }
        default: throw SteamError.invalidMessage
        }
        return message(out.data)
    }
    func sendAndWaitPICS(eMsg: EMsg, body: Data, timeout: TimeInterval) async throws -> [SteamMessageCodec.IncomingMessage] {
        var out = ProtobufEncoder()
        let packages = try ids(body, field: 1, sub: 1), apps = try ids(body, field: 2, sub: 1)
        if !apps.isEmpty { picsAppRequests.append(apps) }
        for p in packages {
            var t = ProtobufEncoder(); t.writeUInt32(fieldNumber: 1, value: p); t.writeBytes(fieldNumber: 5, value: packageBuffers[p] ?? Data())
            out.writeSubmessage(fieldNumber: 3, value: t.data)
        }
        for a in apps {
            if let text = appInfo[a] {
                var t = ProtobufEncoder(); t.writeUInt32(fieldNumber: 1, value: a); t.writeBytes(fieldNumber: 5, value: Data(text.utf8))
                out.writeSubmessage(fieldNumber: 1, value: t.data)
            } else { out.writeUInt32(fieldNumber: 2, value: a) }
        }
        return [message(out.data)]
    }
    func callServiceMethod(method: SteamServiceMethod, body: Data, timeout: TimeInterval) async throws -> Data {
        var d = ProtobufDecoder(body); var out = ProtobufEncoder()
        switch method {
        case .getContentServers:
            guard let directoryHosts else { throw SteamError.invalidMessage }
            var cell: UInt32 = 0, ip: String?, maximum: UInt32 = 0
            while let tag = try d.readTag() {
                if tag.fieldNumber == 1 { cell = UInt32(truncatingIfNeeded: try d.readVarint()) }
                else if tag.fieldNumber == 2 { maximum = UInt32(truncatingIfNeeded: try d.readVarint()) }
                else if tag.fieldNumber == 3 { ip = try d.readString() }
                else { try d.skip(wireType: tag.wireType) }
            }
            require(maximum == 100, "the production CM directory request asks for the complete bounded recommendation")
            directoryRequests.append((cell, ip))
            for host in directoryHosts {
                var row = ProtobufEncoder()
                row.writeString(fieldNumber: 1, value: "CDN")
                row.writeUInt32(fieldNumber: 3, value: cell)
                row.writeString(fieldNumber: 8, value: host)
                row.writeString(fieldNumber: 9, value: host)
                row.writeString(fieldNumber: 12, value: "unavailable")
                out.writeSubmessage(fieldNumber: 1, value: row.data)
            }
        case .getManifestRequestCode:
            var app: UInt32 = 0, depot: UInt32 = 0, gid: UInt64 = 0
            while let tag = try d.readTag() {
                if tag.fieldNumber == 1 { app = UInt32(truncatingIfNeeded: try d.readVarint()) }
                else if tag.fieldNumber == 2 { depot = UInt32(truncatingIfNeeded: try d.readVarint()) }
                else if tag.fieldNumber == 3 { gid = try d.readVarint() }
                else { try d.skip(wireType: tag.wireType) }
            }
            codeRequests.append((app, depot, gid))
            // The code is a fixed64 on the wire.
            out.writeFixed64(fieldNumber: 1, value: requestCode)
        case .getCDNAuthToken:
            var depot: UInt32 = 0, host = "", app: UInt32 = 0
            while let tag = try d.readTag() {
                if tag.fieldNumber == 1 { depot = UInt32(truncatingIfNeeded: try d.readVarint()) }
                else if tag.fieldNumber == 2 { host = try d.readString() }
                else if tag.fieldNumber == 3 { app = UInt32(truncatingIfNeeded: try d.readVarint()) }
                else { try d.skip(wireType: tag.wireType) }
            }
            authRequests.append((depot, host, app))
            out.writeString(fieldNumber: 1, value: "\(authToken)-\(host)-\(depot)")
        default: throw SteamError.invalidMessage
        }
        return out.data
    }
}

func appVDF(_ body: String) -> Data { Data(("\"appinfo\" { " + body + " }").utf8) }
func packageBuffer(apps: [UInt32], depots: [UInt32]) -> Data {
    var pkg: [UInt8] = [0x00] + Array("appids".utf8) + [0]
    for (i, a) in apps.enumerated() { pkg += [0x02] + Array(String(i).utf8) + [0] + le(a) }
    pkg += [0x08, 0x00] + Array("depotids".utf8) + [0]
    for (i, d) in depots.enumerated() { pkg += [0x02] + Array(String(i).utf8) + [0] + le(d) }
    pkg += [0x08]
    return Data(pkg)
}

@MainActor func units(_ fx: [String: Any]) async throws {
    // An accidental bundled fallback or undeclared loopback origin cannot escape
    // the transport boundary. These synthetic URLs never reach DNS or a socket.
    let transportConfiguration = URLSessionConfiguration.ephemeral
    transportConfiguration.protocolClasses = [FixtureContentProtocol.self]
    let transportSession = URLSession(configuration: transportConfiguration)
    defer { transportSession.invalidateAndCancel() }
    for url in ["https://undeclared.invalid/depot/9001/manifest/1/5/424242", "http://127.0.0.1:1/depot/9001/chunk/" + String(repeating: "0", count: 40)] {
        do { _ = try await transportSession.data(from: URL(string: url)!); require(false, "undeclared fixture origin is blocked") }
        catch { require((error as? URLError)?.code == .unsupportedURL, "undeclared fixture origin is blocked before native transport") }
    }
    require(FixtureContentProtocol.blockedCount == 2, "both undeclared secure and HTTP origins were intercepted")

    // ---- protobuf: hostile input throws instead of trapping
    var big = ProtobufDecoder(Data([0x0a, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x01]))
    _ = try? big.readTag()
    do { _ = try big.readBytes(); require(false, "an oversized length is rejected") } catch { require(error is SteamError, "an oversized length is rejected") }
    do { _ = try CMsgClientPICSProductInfoResponse.deserialize(from: Data([0x0a, 0x7f, 0x01])); require(false, "a truncated PICS response is rejected") }
    catch { require(error is SteamError, "a truncated PICS response is rejected") }

    // ---- message header and codec
    var header = CMsgProtoBufHeader()
    header.steamid = 0; header.clientSessionid = 0
    require(header.serialize() == Data([0x09, 0, 0, 0, 0, 0, 0, 0, 0, 0x10, 0]), "the header always carries steamid and session id, even when both are 0")
    header.steamid = 76561197960265729; header.clientSessionid = 33; header.jobidSource = 5; header.targetJobName = "Player.GetOwnedGames#1"
    let decodedHeader = try CMsgProtoBufHeader.deserialize(from: header.serialize())
    require(decodedHeader.steamid == header.steamid && decodedHeader.clientSessionid == 33 && decodedHeader.jobidSource == 5 &&
            decodedHeader.jobidTarget == UInt64.max && decodedHeader.targetJobName == "Player.GetOwnedGames#1", "header round trip")
    let wire = SteamMessageCodec.encodeClientMessage(eMsg: .clientHeartBeat, body: Data([1, 2, 3]), steamID: 9, sessionID: 4, jobID: 77)
    let back = try SteamMessageCodec.decode(wire)
    require(back.isProtobuf && back.eMsg == .clientHeartBeat && back.header.jobidSource == 77 && back.body == Data([1, 2, 3]), "protobuf message round trip")
    require(UInt32(littleEndian: wire.withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) }) == EMsg.clientHeartBeat.masked, "the protobuf flag is set on the message type")
    var oldStyle = Data(le(EMsg.multi.rawValue)); oldStyle += Data(repeating: 0x11, count: 16); oldStyle += Data([9, 9])
    let old = try SteamMessageCodec.decode(oldStyle)
    require(!old.isProtobuf && old.header.jobidTarget == 0x1111111111111111 && old.body == Data([9, 9]), "old-style message with the simple header")
    for bad in [Data(), Data([1, 2]), Data(le(EMsg.multi.masked) + le(0xffffffff) + [1])] {
        do { _ = try SteamMessageCodec.decode(bad); require(false, "a truncated or hostile message is rejected (\(bad.count) bytes)") }
        catch { require(error as? SteamError == .invalidMessage, "a truncated or hostile message is rejected (\(bad.count) bytes)") }
    }

    // ---- logon and response messages
    var logon = CMsgClientLogon(); logon.accountName = "user"; logon.accessToken = "tok"; logon.machineName = "Device"
    var d = ProtobufDecoder(logon.serialize()); var seen: [UInt32: Int] = [:]
    while let tag = try d.readTag() { seen[tag.fieldNumber, default: 0] += 1; try d.skip(wireType: tag.wireType) }
    require(Set(seen.keys) == [1, 5, 6, 7, 8, 11, 50, 96, 102, 108], "the logon carries Valve's field numbers: \(seen.keys.sorted())")
    var resp = ProtobufEncoder(); resp.writeInt32(fieldNumber: 1, value: 1); resp.writeInt32(fieldNumber: 3, value: 45); resp.writeUInt32(fieldNumber: 7, value: 12)
    let logonResponse = try CMsgClientLogonResponse.deserialize(from: resp.data)
    require(logonResponse.eresult == 1 && logonResponse.heartbeatSeconds == 45 && logonResponse.cellID == 12, "logon response")
    var lic = ProtobufEncoder(); lic.writeInt32(fieldNumber: 1, value: 1)
    for p in [100, 200, 300] as [UInt32] { var l = ProtobufEncoder(); l.writeUInt32(fieldNumber: 1, value: p); lic.writeSubmessage(fieldNumber: 2, value: l.data) }
    let licenseList = try CMsgClientLicenseList.deserialize(from: lic.data)
    require(licenseList.licenses.map(\.packageID) == [100, 200, 300], "license list")
    var multi = ProtobufEncoder(); multi.writeUInt32(fieldNumber: 1, value: 7); multi.writeBytes(fieldNumber: 2, value: Data([1, 2]))
    let parsedMulti = try CMsgMulti.deserialize(from: multi.data)
    require(parsedMulti.sizeUnzipped == 7 && parsedMulti.messageBody == Data([1, 2]), "multi message")

    // ---- Steam's playtime record
    var games = ProtobufEncoder()
    for (app, minutes, last) in [(4242, 90, 1_700_000_000), (5000, 0, 0), (5001, 0, 1_600_000_000), (5002, 30, 0)] as [(UInt64, UInt64, UInt64)] {
        var g = ProtobufEncoder(); g.writeUInt64(fieldNumber: 1, value: app); g.writeUInt64(fieldNumber: 4, value: minutes); g.writeUInt64(fieldNumber: 11, value: last)
        games.writeSubmessage(fieldNumber: 2, value: g.data)
    }
    games.writeUInt32(fieldNumber: 1, value: 4)
    let playtime = try SteamPlaytime.parse(games.data)
    require(playtime.keys.sorted() == [4242, 5001, 5002], "playtime lists played or last-played apps only")
    require(playtime[4242]?.minutes == 90 && playtime[4242]?.lastPlayed == 1_700_000_000, "playtime and last played")
    require(playtime[4242]?.played == "1.5 hrs played" && playtime[5002]?.played == "30 min played" && SteamPlaytime(minutes: 1200, lastPlayed: 0).played == "20 hrs played",
            "playtime text")
    require(playtime[5001]?.played == nil && playtime[5001]?.lastPlayedText() != nil, "a game only last played has no playtime text")
    do { _ = try SteamPlaytime.parse(Data([0x12, 0x7f, 0x01])); require(false, "a truncated playtime record is rejected") }
    catch { require(error is SteamError, "a truncated playtime record is rejected") }

    // ---- the account between the app's connection and a game session (SteamConnectionGate)
    var events: [String] = []
    let gate = SteamConnectionGate(close: {
        events.append("logoff-sent")
        try? await Task.sleep(nanoseconds: 60_000_000)       // the socket takes a while to close
        events.append("socket-closed")
    }, reopen: { events.append("reopened") })
    require(gate.open && !gate.heldForDock, "the app's connection is open without a session")
    // Madeira Dock: the sign-in transfer is written only after holdForDock() returns.
    await gate.holdForDock()
    events.append("sign-in-transfer-written")
    require(events == ["logoff-sent", "socket-closed", "sign-in-transfer-written"],
            "Dock: the app's connection logs off and its socket closes before the sign-in is handed over: \(events)")
    // runWineFullSequence's own sessionChanged(true) after that closes nothing again.
    gate.close()
    await Task.yield()
    require(events.filter { $0 == "logoff-sent" }.count == 1, "closing again is a no-op")
    // Nothing reopens the connection while Dock holds the account, even with no session detected yet.
    require(!gate.reopen(sessionRunning: false) && !events.contains("reopened"),
            "while Dock holds the account the connection stays closed (no session seen yet)")
    gate.releaseDock()
    require(!gate.reopen(sessionRunning: true) && !events.contains("reopened"), "the Dock session still runs: still closed")
    require(gate.reopen(sessionRunning: false) && events.last == "reopened" && gate.open, "the session ended: the connection may come back")
    require(!gate.reopen(sessionRunning: false) && events.filter { $0 == "reopened" }.count == 1, "reopening is idempotent")
    // A session that starts while a close is under way: Dock waits for that same close.
    events.removeAll()
    let first = gate.close()
    await gate.holdForDock()
    events.append("sign-in-transfer-written")
    await first.value
    require(events == ["logoff-sent", "socket-closed", "sign-in-transfer-written"],
            "a Dock start during a session's close waits for that close: \(events)")
    gate.releaseDock(); gate.reopen(sessionRunning: false)

    // ---- app info: depot selection
    let full = appVDF(#"""
    "common" { "name" "Fixture" "type" "game" "oslist" "windows,macos" "library_assets_full" { "library_capsule" { "image" { "english" "cap/1.jpg" } } } "parent" "10" }
    "config" { "installdir" "Fixture Game" }
    "depots" {
      "101" { "config" { "oslist" "windows" } "manifests" { "public" { "gid" "1001" "size" "500" "download" "300" } } }
      "102" { "config" { "oslist" "windows" "language" "english" } "manifests" { "public" { "gid" "1002" "download" "40" } } }
      "103" { "config" { "oslist" "windows" "language" "german" } "manifests" { "public" { "gid" "1003" "download" "40" } } }
      "104" { "config" { "oslist" "windows" "osarch" "32" } "manifests" { "public" { "gid" "1004" "download" "70" } } }
      "105" { "config" { "oslist" "windows" "osarch" "64" } "manifests" { "public" { "gid" "1005" "download" "80" } } }
      "106" { "config" { "oslist" "windows" "lowviolence" "1" } "manifests" { "public" { "gid" "1006" } } }
      "107" { "dlcappid" "999" "manifests" { "public" { "gid" "1007" } } }
      "108" { "sharedinstall" "1" "manifests" { "public" { "gid" "1008" } } }
      "109" { "config" { "oslist" "windows" } }
      "110" { "config" { "oslist" "macos" } "manifests" { "public" { "gid" "1010" } } }
      "branches" { "public" { "buildid" "4242" } }
    }
    """#)
    let info = SteamAppInfo.parse(appID: 10, from: full)!
    require(info.type == .game, "PICS type names match without case")
    require(info.installDepots().map(\.depotID) == [101, 102, 105], "install depots: common, English, 64-bit only")
    require(info.downloadSize(for: "windows") == 420, "the download size counts only the selected depots")
    require(info.buildID == 4242 && info.installableOnWindows && info.installDir == "Fixture Game", "build id, install folder and Windows installability")
    require(info.libraryCapsule == "cap/1.jpg" && info.parentID == nil, "artwork name; a parent equal to itself is not kept")
    require(info.depotSelectionSummary().contains("103[-]lang") && info.depotSelectionSummary().contains("107[-]dlc"), "the selection log names the rule that left a depot out")
    let legacy = SteamAppInfo.parse(appID: 10, from: appVDF(#"""
    "common" { "name" "Old" "type" "Game" "oslist" "windows" } "config" { "installdir" "Old" }
    "depots" { "201" { "config" { "oslist" "windows" "osarch" "32" } "manifests" { "public" "2001" } } "202" { "manifests" { "public" "2002" } } }
    """#))!
    require(legacy.installDepots().map(\.depotID) == [201, 202], "32-bit-only apps fall back to 32-bit depots")
    let mac = SteamAppInfo.parse(appID: 10, from: appVDF(#""common" { "name" "Mac" "type" "Game" "oslist" "macos" } "depots" { "301" { "config" { "oslist" "macos" } "manifests" { "public" "3001" } } }"#))!
    require(!mac.installableOnWindows, "apps without a Windows build are not offered")
    let tool = SteamAppInfo.parse(appID: 10, from: appVDF(#""common" { "name" "Tool" "type" "Tool" } "depots" { "401" { "manifests" { "public" "4001" } } }"#))!
    require(!tool.installableOnWindows, "tools and redistributables are not offered")
    let huge = SteamAppInfo.parse(appID: 10, from: appVDF(#"""
    "common" { "name" "Huge" "type" "Game" "oslist" "windows" }
    "depots" { "501" { "manifests" { "public" { "gid" "1" "download" "18446744073709551615" } } } "502" { "manifests" { "public" { "gid" "2" "download" "5" } } } }
    """#))!
    require(huge.downloadSize(for: "windows") == UInt64.max, "size sums saturate")
    require(SteamAppInfo.parse(appID: 10, from: appVDF(#""common" { "type" "Game" }"#)) == nil, "app info without a name is dropped")
    let pkg = packageBuffer(apps: [7000], depots: [7001, 7002])
    require(VDFParser.parsePackageIDs(key: "depotids", from: pkg) == [7001, 7002] && VDFParser.parsePackageAppIDs(from: pkg) == [7000], "package app and depot ids")
    var owners: [UInt32: SteamAppInfo] = [:]
    owners[7100] = SteamAppInfo.parse(appID: 7100, from: appVDF(#""common" { "name" "Owner" "type" "Game" "oslist" "windows" } "depots" { "7109" { "config" { "oslist" "windows" } "manifests" { "public" { "gid" "5" "download" "9" } } } }"#))!
    var sharing = SteamAppInfo.parse(appID: 7200, from: appVDF(#""common" { "name" "User" "type" "Game" "oslist" "windows" } "depots" { "7109" { "depotfromapp" "7100" } "7201" { "manifests" { "public" "71" } } }"#))!
    require(sharing.depots.first { $0.depotID == 7109 }?.fromApp == 7100 && sharing.installDepots().map(\.depotID) == [7201], "a depot from another app has no manifest of its own")
    require(sharing.inheritDepots(from: owners) == 1 && sharing.installDepots().map(\.depotID) == [7109, 7201], "it inherits the owner's manifest")

    // ---- decrypt and decompress the known vectors
    let key = hexData(fx["key"] as! String)
    let plainVector = hexData(fx["vectorPlain"] as! String)
    let vectors = fx["vectors"] as! [String: String]
    for kind in ["zstd", "lzma", "zip"] {
        let encrypted = hexData(vectors[kind]!)
        let out = try ContentDecryptor.processChunk(encryptedData: encrypted, depotKey: key, expectedCRC: ContentDecryptor.adler32(plainVector), expectedSize: plainVector.count)
        require(out == plainVector, "the \(kind) container decodes to the original bytes")
        do { _ = try ContentDecryptor.processChunk(encryptedData: encrypted, depotKey: key, expectedCRC: ContentDecryptor.adler32(plainVector) ^ 1, expectedSize: plainVector.count); require(false, "\(kind): a wrong checksum is rejected") }
        catch { require(error as? SteamError == .checksumMismatch, "\(kind): a wrong checksum is rejected") }
        // The decoded length is what the downloader compares with the manifest's size (it rejects a chunk that disagrees).
        do { let o = try ContentDecryptor.processChunk(encryptedData: encrypted, depotKey: key, expectedCRC: 0, expectedSize: plainVector.count + 1); require(o.count != plainVector.count + 1, "\(kind): a size that disagrees is visible in the decoded length") }
        catch { require(error is SteamError, "\(kind): a size that disagrees is rejected (\(error))") }
        var wrong = key; wrong[0] ^= 1
        do { let o = try ContentDecryptor.processChunk(encryptedData: encrypted, depotKey: wrong, expectedCRC: ContentDecryptor.adler32(plainVector), expectedSize: plainVector.count); require(o != plainVector, "\(kind): a wrong key does not decode") }
        catch { require(error is SteamError, "\(kind): a wrong key is rejected") }
        // Cut into the actual payload. Removing only the last 40 bytes of a
        // ZIP can remove its optional central directory while leaving the
        // complete local entry/deflate stream that Steam's chunk reader uses.
        var cut = encrypted; cut.removeLast(max(40, encrypted.count / 2))
        do { let o = try ContentDecryptor.processChunk(encryptedData: cut, depotKey: key, expectedCRC: 0, expectedSize: plainVector.count); require(o != plainVector, "\(kind): a truncated chunk does not decode") }
        catch { require(error is SteamError, "\(kind): a truncated chunk is rejected") }
    }
    // Steam seeds Adler-32 with 0: for one byte b that is b | (b << 16).
    require(ContentDecryptor.adler32(Data([0x41])) == 0x00410041 && ContentDecryptor.adler32(Data()) == 0, "Steam's Adler-32 is seeded with 0")
    let raw = hexData(fx["rawEncrypted"] as! String)
    let decrypted = try ContentDecryptor.decryptChunk(encryptedData: raw, depotKey: key)
    require(decrypted == plainVector.prefix(1000), "AES: the IV is ECB-decrypted, the rest CBC with PKCS7")
    do { _ = try ContentDecryptor.decryptChunk(encryptedData: raw, depotKey: Data(count: 16)); require(false, "a short depot key is rejected") }
    catch { require(error is SteamError, "a short depot key is rejected") }
    do { _ = try ContentDecryptor.decompressChunk(compressedData: Data("ZZZZ-not-a-container".utf8), expectedSize: 999); require(false, "an unknown container is rejected") }
    catch { require(error as? SteamError == .chunkDecodeFailed("ZZZZ"), "an unknown container is rejected and named by its tag (\(error))") }
    // A hostile footer size may not ask for a huge buffer; malformed zstd may not exit the process.
    var hostile = Data("VZa".utf8) + Data(count: 4) + Data([0x5d, 0, 0, 0, 1]) + Data(count: 30) + Data(le(0)) + Data(le(0xffffffff)) + Data("zv".utf8)
    do { _ = try ContentDecryptor.decompressChunk(compressedData: hostile, expectedSize: 0); require(false, "a hostile VZip size is rejected") }
    catch { require(error as? SteamError == .chunkDecodeFailed("vzip"), "a hostile VZip size is rejected") }
    hostile = Data("VSZa".utf8) + Data(count: 4) + Data([0x28, 0xb5, 0x2f, 0xfd, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff]) + Data(count: 30) + Data(count: 15)
    do { _ = try ContentDecryptor.decompressChunk(compressedData: hostile, expectedSize: 4096); require(false, "a malformed zstd frame is rejected") }
    catch { require(error as? SteamError == .chunkDecodeFailed("vzstd"), "a malformed zstd frame is rejected without ending the process") }
    let noSize = try? ContentDecryptor.decompressChunk(compressedData: Data("VSZa".utf8) + Data(count: 30), expectedSize: 0)
    require(noSize == nil, "zstd without a manifest size is rejected")

    // ---- manifest names and paths
    var folded: [String: String] = [:]
    require(SteamInstallFiles.safeRelativePath("Docs\\Readme.txt", folded: &folded) == "Docs/Readme.txt", "backslashes become separators")
    require(SteamInstallFiles.safeRelativePath("docs/notes.txt", folded: &folded) == "Docs/notes.txt", "a directory keeps the first spelling seen (Windows semantics)")
    for bad in ["../x", "a/../../x", "..\\x", "/abs", "a//b/../c", "C:\\x", "a/b:c", "", "a\u{01}b", String(repeating: "a/", count: 70) + "b"] {
        var f: [String: String] = [:]
        let result = SteamInstallFiles.safeRelativePath(bad, folded: &f)
        require(result == nil || !(result!.contains("..") || result!.hasPrefix("/") || result!.contains(":")), "a hostile manifest path stays inside the folder: \(bad.prefix(20).debugDescription) -> \(String(describing: result))")
    }
    require(SteamInstallFiles.safeFolderName("Fixture Game") == "Fixture Game" && SteamInstallFiles.safeFolderName("../../etc") == "etc" &&
            SteamInstallFiles.safeFolderName("a\\b\\Last") == "Last" && SteamInstallFiles.safeFolderName("..") == "app" && SteamInstallFiles.safeFolderName("") == "app" &&
            SteamInstallFiles.safeFolderName("c:") == "app", "install folders are one safe component")
    require(DepotDownloader.usableContentHost("cache1.steamcontent.com") && DepotDownloader.usableContentHost("a.b.akamaized.net") &&
            DepotDownloader.usableContentHost("steamcdn-a.akamaihd.net") &&
            !DepotDownloader.usableContentHost("evil.example.com") && !DepotDownloader.usableContentHost("x.steamcontent.com:8080") &&
            !DepotDownloader.usableContentHost("a b.steamcontent.com") && !DepotDownloader.usableContentHost("cache1.steamcontent.com/../") &&
            !DepotDownloader.usableContentHost("127.0.0.1") && !DepotDownloader.usableContentHost("localhost") &&
            !DepotDownloader.usableContentHost("cdn.local"), "content host syntax admits public CDN vendors and rejects paths, ports, IPs and local names")
    let server: [String: Any] = ["https_support": "unavailable"]
    require(DepotDownloader.serverEligibility(server, appID: 1) == .noHTTPS &&
            DepotDownloader.serverEligibility(["use_as_proxy": true], appID: 1) == .other &&
            DepotDownloader.serverEligibility(["allowed_app_ids": [5]], appID: 1) == .other &&
            DepotDownloader.serverEligibility(["allowed_app_ids": [1], "https_support": "mandatory"], appID: 1) == .usable &&
            DepotDownloader.serverEligibility([:], appID: 1) == .usable, "content server eligibility")
    let health = ContentHostHealth()
    for _ in 0..<3 { health.recordFailure("https://bad", reason: "x") }
    require(health.order(["https://bad", "https://good"], seed: 0).first == "https://good", "a server that keeps failing moves to the back of the rotation")

    // ---- the library fetcher, through a scripted connection
    let session = ScriptedSession()
    session.licenses = [11, 12]
    session.packageBuffers = [11: packageBuffer(apps: [9500, 9501], depots: [9502]), 12: packageBuffer(apps: [9503], depots: [9504])]
    let gameVDF = { (name: String, type: String, os: String) in "\"appinfo\" { \"common\" { \"name\" \"\(name)\" \"type\" \"\(type)\" \"oslist\" \"\(os)\" } \"config\" { \"installdir\" \"\(name)\" } " +
        "\"depots\" { \"9999\" { \"config\" { \"oslist\" \"windows\" } \"manifests\" { \"public\" { \"gid\" \"1\" \"download\" \"5\" } } } } }" }
    session.appInfo = [9500: gameVDF("Alpha", "Game", "windows"), 9501: gameVDF("Soundtrack", "Music", "windows"), 9503: gameVDF("Mac Only", "Game", "macos")]
    let fetcher = SteamLibraryFetcher(session: session)
    let ownedApps = try await fetcher.fetchOwnedApps()
    require(ownedApps.map(\.appID).sorted() == [9500, 9503], "the owned library: games only, from the license list's packages (\(ownedApps.map(\.appID)))")
    require(ownedApps.filter(\.installableOnWindows).map(\.appID) == [9500], "and only what installs for Windows")
    require(session.picsAppRequests.flatMap { $0 }.sorted() == [9500, 9501, 9503], "product info was asked for every app of the packages")
    let licensedDepots = try await fetcher.ownedDepotIDs()
    require(licensedDepots == [9502, 9504], "depot ids of the licenses")
}

@MainActor func install(_ fx: [String: Any], phase: String) async throws {
    let tmp = URL(fileURLWithPath: fx["tmp"] as! String)
    let drive = tmp.appendingPathComponent("drive_c")
    let steamApps = SteamInstallPaths.steamApps(drive: drive)
    let hosts = fx["hosts"] as! [String]
    let session = ScriptedSession()
    session.keys = [9001: hexData(fx["key"] as! String), 9003: hexData(fx["keyShared"] as! String)]
    session.requestCode = UInt64(fx["requestCode"] as! Int)
    session.authToken = fx["auth"] as! String
    if phase == "directory-fallback" { session.directoryHosts = fx["directoryHosts"] as? [String] }
    session.licenses = [21]
    session.packageBuffers = [21: packageBuffer(apps: [9000, 9100], depots: [9001, 9003])]
    let version = phase == "update" ? 2 : 1
    let gid = (fx["gid\(version)"] as! Int)
    let gidShared = fx["gidShared"] as! Int
    let build = version == 1 ? 1000 : 1001
    session.appInfo = [
        9000: "\"appinfo\" { \"common\" { \"name\" \"Fixture Game\" \"type\" \"Game\" \"oslist\" \"windows\" } \"config\" { \"installdir\" \"Fixture Game\" } " +
            "\"depots\" { " +
            "\"9001\" { \"config\" { \"oslist\" \"windows\" } \"manifests\" { \"public\" { \"gid\" \"\(gid)\" \"size\" \"1\" \"download\" \"1\" } } } " +
            "\"9002\" { \"config\" { \"oslist\" \"windows\" \"language\" \"german\" } \"manifests\" { \"public\" { \"gid\" \"222\" } } } " +
            "\"9003\" { \"config\" { \"oslist\" \"windows\" } \"depotfromapp\" \"9100\" } " +
            "\"9004\" { \"config\" { \"oslist\" \"windows\" } \"manifests\" { \"public\" { \"gid\" \"444\" } } } " +
            "\"branches\" { \"public\" { \"buildid\" \"\(build)\" } } } }",
        9100: "\"appinfo\" { \"common\" { \"name\" \"Fixture Base\" \"type\" \"Game\" \"oslist\" \"windows\" } \"config\" { \"installdir\" \"Fixture Game\" } " +
            "\"depots\" { \"9003\" { \"config\" { \"oslist\" \"windows\" } \"manifests\" { \"public\" { \"gid\" \"\(gidShared)\" \"download\" \"1\" } } } " +
            "\"branches\" { \"public\" { \"buildid\" \"7\" } } } }"]
    let fetcher = SteamLibraryFetcher(session: session)
    guard let app = try await fetcher.fetchInstallInfo(appID: 9000) else { require(false, "app info"); return }
    require(app.depots.first { $0.depotID == 9003 }?.publicManifestID == UInt64(gidShared), "the shared depot got its manifest from the owning app")
    require(app.sharedOwners[9100]?.installDir == "Fixture Game", "the owner of the shared depot is known for the record")
    let sourceSuite = "madeira-install-sources-" + UUID().uuidString
    let sourceDefaults = UserDefaults(suiteName: sourceSuite)!
    defer { sourceDefaults.removePersistentDomain(forName: sourceSuite) }
    let sourceStore = SteamContentSourceStore(defaults: sourceDefaults)
    require(sourceStore.select("missing.steamcontent.com"), "a trusted former manual choice is saved")
    let downloader = DepotDownloader(session: session, sourceStore: sourceStore)
    if phase != "directory-fallback" { downloader.contentHosts = { _ in hosts } }
    var lastProgress = SteamDownloadProgress()
    var progressEvents: [SteamDownloadProgress] = []
    let licensed: Set<UInt32> = phase == "refused-licensed" ? [9001, 9003, 9004] : [9001, 9003]
    do {
        _ = try await downloader.install(app, steamApps: steamApps, ownedDepots: { licensed }) {
            lastProgress = $0
            progressEvents.append($0)
            FixtureContentProtocol.progressPhase($0.phase.rawValue)
        }
        print("RESULT install=ok")
    } catch {
        print("RESULT install=failed reason=\(SteamOwnedLibrary.reason(error))")
        require(phase == "interrupt" || phase == "refused-licensed", "install failed only where the phase expects it (\(error))")
        if phase == "refused-licensed" { require(error as? SteamError == .depotKeyNotFound(9004), "a refusal for a depot the account is licensed for fails the install") }
    }
    let found = MadeiraDock.games(drive: drive)
    if phase != "refused-licensed" {
        require(progressEvents.contains { $0.phase.rawValue == "checking" },
                "the install reports local checking before transferring game chunks")
        require(FixtureContentProtocol.prematureChunkCount == 0 && FixtureContentProtocol.wrongPhaseChunkCount == 0,
                "all actual chunk requests, including CDN probes, follow local checking in the downloading phase")
    }
    if phase == "recheck" {
        require(!progressEvents.contains { $0.phase == .downloading },
                "an unchanged completed install never reports another game download")
        require(progressEvents.allSatisfy { $0.bytesPerSecond == 0 },
                "reused local chunks never appear as network speed")
        require(lastProgress.reusedBytes == lastProgress.totalBytes && lastProgress.networkBytes == 0,
                "an unchanged completed install accounts for all bytes as reuse and none as network")
        require(progressEvents.contains { $0.phase.rawValue == "checking" && $0.totalBytes > 0 && $0.doneBytes == $0.totalBytes },
                "local checking accounts for every verified byte before finishing an unchanged install")
    }
    require(lastProgress.sourceNotice?.contains("missing.steamcontent.com") == true,
            "the full install reports automatic fallback for a missing manual source")
    require(session.authRequests.allSatisfy { $0.host != "missing.steamcontent.com" },
            "a stale manual host never receives a depot authorization request")
    if phase == "directory-fallback" {
        require(session.directoryRequests.count == 2 && session.directoryRequests[0].cell == session.cellID &&
                session.directoryRequests[0].ip == nil && session.directoryRequests[1].cell == 0 &&
                session.directoryRequests[1].ip == "114.114.114.114",
                "the real directory path queries both the session cell and mainland supplement through production CM messages")
    }
    print("RESULT listed=\(found.filter { $0.id == 9000 && $0.installed }.count)")
    if phase != "interrupt" && phase != "refused-licensed" {
        require(lastProgress.phase == .finishing && lastProgress.doneBytes == lastProgress.totalBytes && lastProgress.totalBytes > 0, "progress ends complete: \(lastProgress.doneBytes)/\(lastProgress.totalBytes)")
        require(lastProgress.reusedBytes + lastProgress.networkBytes == lastProgress.totalBytes,
                "verified reuse and actual fetched chunks account for the entire install without double counting")
        if let firstDownload = progressEvents.first(where: { $0.phase == .downloading }) {
            require(firstDownload.networkBytes == 0 && firstDownload.doneBytes == firstDownload.reusedBytes,
                    "the downloading phase begins with the fully checked local reuse, before any transfer")
        }
        // Keys are asked for with the app being installed; manifest codes and tokens with the app that owns the content.
        require(session.keyRequests.contains { $0.depot == 9001 && $0.app == 9000 } && session.keyRequests.contains { $0.depot == 9003 && $0.app == 9000 }, "depot keys are requested for the installed app")
        require(session.codeRequests.contains { $0.depot == 9003 && $0.app == 9100 } && session.codeRequests.contains { $0.depot == 9001 && $0.app == 9000 }, "manifest codes are requested for the app that owns the depot")
        let secureHostnames = Set((fx["authorizedSecureHosts"] as? [String]) ?? ["dead.fixture.steamcontent.com", "flaky.fixture.steamcontent.com", "good.fixture.steamcontent.com"])
        require(session.authRequests.contains { $0.depot == 9003 && $0.app == 9100 && secureHostnames.contains($0.host) }, "content authorization names the bare secure host and owning app")
        require(session.authRequests.allSatisfy { secureHostnames.contains($0.host) }, "credential-free HTTP chunk origins never receive CDN token requests")
        require(!session.keyRequests.contains { $0.depot == 9002 }, "a depot for another language is never asked for")
        guard let game = found.first(where: { $0.id == 9000 }) else { require(false, "Madeira Dock's discovery finds the downloaded game"); return }
        require(game.installed && game.installDir == "Fixture Game" && game.library == "Program Files (x86)/Steam/steamapps" && game.name == "Fixture Game",
                "Madeira Dock's discovery finds the downloaded game as fully installed")
        require(game.customExecutables, "the record lists the per-user executables Valve's client prepares (CheckGuid)")
        require(SteamInstallPaths.isManaged(library: game.library), "it is in the library Madeira manages")
        do { try MadeiraDock.validate(game, drive: drive, bundled: true) ; require(false, "validate needs the client files") }
        catch { require(error.localizedDescription.contains("client"), "Dock's launch check passes the record and stops at the missing client files only") }
        require(SteamInstallFiles.buildID(appID: 9000, steamApps: steamApps) == build, "the record's build id")
        require(found.first(where: { $0.id == 9100 })?.installed == true, "the owning app has its own record for the shared depot, as Valve's client requires")
        let events = loggedEvents.joined(separator: "\n")
        require(!events.contains("Fixture") && !events.contains("7656119") && !events.contains("tok"), "no name, account or token in the log")
        require(events.contains("[steam-depot] license app=9000 skipped=9004"), "a depot the account neither has a key for nor a license for is left out")
    }
    if phase == "update" {
        let expectedNetwork = UInt64(fx["updateNetworkBytes"] as! Int)
        require(lastProgress.networkBytes == expectedNetwork &&
                lastProgress.reusedBytes == lastProgress.totalBytes - expectedNetwork,
                "an update counts only its changed chunk as network and all other depots as verified reuse")
        require(progressEvents.first(where: { $0.phase == .downloading })?.reusedBytes == lastProgress.reusedBytes,
                "an update checks unchanged chunks in every depot before fetching its first missing chunk")
        require(SteamInstallFiles.buildID(appID: 9000, steamApps: steamApps) == 1001, "the update changed the recorded build")
        require(SteamInstallFiles.buildID(appID: 4242, steamApps: steamApps) == nil, "no record, no build")
        let ownedNow = SteamOwnedGame(app)
        require(ownedNow.buildID == 1001 && ownedNow.folderName == "Fixture Game", "the owned game carries the build")
    }
    if phase == "uninstall" {
        SteamInstallFiles.delete(appID: 9000, folderName: "Fixture Game", steamApps: steamApps)
        require(MadeiraDock.games(drive: drive).isEmpty, "an uninstalled game is no longer found (its shared owner record went with it)")
        require(!FileManager.default.fileExists(atPath: steamApps.appendingPathComponent("common/Fixture Game").path), "its folder is gone")
        require(FileManager.default.fileExists(atPath: steamApps.appendingPathComponent("common").path) , "the common folder stays")
    }
}

@main struct Harness {
    @MainActor static func main() async {
        setvbuf(stdout, nil, _IONBF, 0)
        let args = CommandLine.arguments
        guard args.count >= 3, let raw = FileManager.default.contents(atPath: args[2]),
              let fx = try? JSONSerialization.jsonObject(with: raw) as? [String: Any] else { print("FAIL: fixture"); exit(2) }
        guard let origins = fx["transportOrigins"] as? [String: String] else { print("FAIL: fixture transport origins"); exit(2) }
        FixtureContentProtocol.configure(origins, cacheURL: URL(fileURLWithPath: fx["tmp"] as! String).appendingPathComponent("directory-cache.json"))
        do {
            if args[1] == "units" { try await units(fx) } else { try await install(fx, phase: args[1]) }
        } catch {
            print("FAIL: \(args[1]) threw \(error)"); failures += 1
        }
        require(FixtureContentProtocol.blockedCount == (args[1] == "units" ? 2 : 0), "the complete production harness requested only declared local fixture origins")
        if failures > 0 { print("FAILURES: \(failures)"); exit(1) }
        print("PASS: \(args[1]) phase")
    }
}
'''



# ---------------------------------------------------------------- Part B driver
owned_source = sources['SteamOwnedLibrary.swift']
owned_game = owned_source[owned_source.index('struct SteamOwnedGame:'):owned_source.index('// MARK: - Playtime')]
playtime_source = owned_source[owned_source.index('// MARK: - Playtime'):owned_source.index('// MARK: - Model')]
reason = block(owned_source, 'static func reason(_ error: Error) -> String')
dock_head = dock[dock.index('enum DockPerformancePolicy {'):dock.index('enum MadeiraDock {')]
dock_body = (dock[dock.index('enum MadeiraDock {'):dock.index('    @MainActor static func beginReportSession(')] +
             dock[dock.index("    /// The host's environment for one launch."):])
# URLSession lives in FoundationNetworking on Linux; the production file imports Foundation only.
downloader_host = downloader.replace('import Foundation\n', 'import Foundation\n#if canImport(FoundationNetworking)\nimport FoundationNetworking\n#endif\n', 1)
# Install a fail-closed fixture transport in both normal and probe sessions.
# This preserves the entire production fetchManifest and download implementations.
assert downloader_host.count('let config = URLSessionConfiguration.ephemeral') == 2
downloader_host = downloader_host.replace('let config = URLSessionConfiguration.ephemeral',
    'let config = URLSessionConfiguration.ephemeral\n        config.protocolClasses = [FixtureContentProtocol.self]')
# The actual directory pipeline also stays behind the fixture transport. Its
# persistent cache is redirected only in the generated host copy, preserving all
# production authority/fallback logic while leaving the user's cache untouched.
directory_host = sources['SwiftSteam/Content/SteamDownloadSources.swift']
assert directory_host.count('let config = URLSessionConfiguration.ephemeral') == 1
directory_host = directory_host.replace('let config = URLSessionConfiguration.ephemeral',
    'let config = URLSessionConfiguration.ephemeral\n        config.protocolClasses = [FixtureContentProtocol.self]')
directory_cache = ('static let shared = Cache(fileURL: FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]\n'
                   '            .appendingPathComponent("Madeira/SteamContentDirectory/last-good.json"))')
assert directory_cache in directory_host
directory_host = directory_host.replace(directory_cache,
    'static let shared = Cache(fileURL: FixtureContentProtocol.directoryCacheURL)')
# The free-space query is Darwin's (volumeAvailableCapacityForImportantUsage); the host has none, so the check is skipped there.
space = ('let values = try? installURL.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])\n'
         '            let available = UInt64(max(0, values?.volumeAvailableCapacityForImportantUsage ?? Int64.max))')
assert space in downloader_host
downloader_host = downloader_host.replace(space, 'let available = UInt64.max')
fetcher_source = sources['SwiftSteam/Library/SteamLibraryFetcher.swift']

work = Path(tempfile.mkdtemp(prefix='madeira-steam-library-'))
servers = []
try:
    shim = work / 'shim'
    native_apple = sys.platform == 'darwin'
    shims = [] if native_apple else [('CommonCrypto', CRYPTO_H, CRYPTO_C), ('Compression', COMPRESSION_H, COMPRESSION_C)]
    for name, header, csource in shims:
        directory = shim / name
        directory.mkdir(parents=True)
        stem = 'cc_shim' if name == 'CommonCrypto' else 'compression_shim'
        (directory / f'{stem}.h').write_text(header)
        (directory / f'{stem}.c').write_text(csource)
        (directory / 'module.modulemap').write_text(f'module {name} [system] {{ header "{stem}.h" export * }}\n')
    if not native_apple:
        (shim / 'zlib').mkdir()
        (shim / 'zlib/module.modulemap').write_text('module zlib [system] { header "/usr/include/zlib.h" link "z" export * }\n')
    (work / 'decoders.h').write_text(DECODERS_H.format(app=app))

    objects = []
    def compile_c(source, extra=()):
        obj = work / (Path(source).stem + '.o')
        result = subprocess.run([CC, '-c', '-g', '-fsanitize=address', '-w', str(source), '-o', str(obj)] + list(extra), capture_output=True, text=True, timeout=120)
        require(result.returncode == 0, f'{Path(source).name} compiles on the host')
        if result.returncode: sys.stdout.write(result.stderr[-3000:])
        objects.append(str(obj))
    if not native_apple:
        compile_c(shim / 'CommonCrypto/cc_shim.c', ['-I', str(shim / 'CommonCrypto')])
        compile_c(shim / 'Compression/compression_shim.c', ['-I', str(shim / 'Compression')])
    for name in ['zstd_edu.c', 'lzma_shim.c', 'chunk_zip.c']:
        compile_c(steam / name)

    (work / 'stubs.swift').write_text(STUBS.replace('RELATIVE_ROOT', relative_root).replace('REASON', reason))
    (work / 'checks.swift').write_text(CHECKS)
    (work / 'dock.swift').write_text(('import Foundation\n#if canImport(Glibc)\nimport Glibc\n#else\nimport Darwin\n#endif\n'
        '#if canImport(Network)\nimport Network\n#else\n'
        '/* Linux: no Network framework; DockOffline only needs these names. */\n'
        'final class NWPathMonitor { struct Path { enum Status { case satisfied, unsatisfied, requiresConnection }; '
        'var status = Status.satisfied }; var currentPath = Path(); func start(queue: DispatchQueue) {} }\n#endif\n') + dock_head + dock_body)
    (work / 'owned.swift').write_text('import Foundation\n' + owned_game + playtime_source)
    (work / 'downloader.swift').write_text(downloader_host)
    (work / 'directory.swift').write_text(directory_host)
    production = [steam / 'Proto/SteamProtoMessages.swift', steam / 'Core/SteamError.swift', steam / 'Core/SteamProtocol.swift',
                  steam / 'Core/SteamMessageCodec.swift', steam / 'Core/SteamCMSession.swift',
                  steam / 'Content/ContentDecryptor.swift', steam / 'Content/DepotManifest.swift',
                  steam / 'Library/SteamAppInfo.swift', steam / 'Library/SteamLibraryFetcher.swift',
                  steam / 'Install/AppManifestWriter.swift', app / 'SteamInstall.swift', app / 'SteamKeyValues.swift']
    exe = work / 'check'
    module_flags = [] if native_apple else ['-I', str(shim / 'CommonCrypto'), '-I', str(shim / 'Compression'), '-I', str(shim / 'zlib')]
    crypto_flags = [] if native_apple else ['-Xlinker', '-lcrypto']
    build = subprocess.run([SWIFTC, '-j1', '-parse-as-library', '-swift-version', '5', '-sanitize=address', '-g', '-o', str(exe),
                            *module_flags,
                            '-import-objc-header', str(work / 'decoders.h'),
                            str(work / 'stubs.swift'), str(work / 'checks.swift'), str(work / 'dock.swift'), str(work / 'owned.swift'),
                            str(work / 'downloader.swift'), str(work / 'directory.swift')] + [str(x) for x in production] + objects + crypto_flags +
                           ['-Xlinker', '-llzma', '-Xlinker', '-lz'], capture_output=True, text=True, timeout=300)
    require(build.returncode == 0, 'the production Steam library, download and decoder code compile on the host')
    if build.returncode:
        sys.stdout.write(build.stderr[-6000:])
        print(f'check-steam-library: {failures} FAILED')
        sys.exit(1)

    # The Swift crash handler would sit on the pipes for 30 s after a trap; AddressSanitizer reports on its own.
    env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0', SWIFT_BACKTRACE='enable=no')
    output = {}
    def run(phase, fixture, timeout=300):
        path = work / f'fixture-{phase}.json'
        path.write_text(json.dumps(fixture))
        result = subprocess.run([str(exe), phase, str(path)], env=env, capture_output=True, text=True, timeout=timeout)
        sys.stdout.write(result.stdout)
        if result.returncode:
            sys.stdout.write(result.stderr[-3000:])
        require(result.returncode == 0, f'Swift phase "{phase}" ran clean under AddressSanitizer')
        results = {}
        for line in result.stdout.splitlines():
            if line.startswith('RESULT '):
                for kv in line[len('RESULT '):].split():
                    k, _, v = kv.partition('=')
                    results[k] = v
        output[phase] = results
        return results

    # ---- all transport ends at loopback; HTTPS exists only as a declared fixture origin
    state = State()
    version1, version2, shared = main_depot(1), main_depot(2), shared_depot()
    class Merged: pass
    merged = Merged(); merged.chunks = {**version1.chunks, **version2.chunks}
    state.depots = {9001: merged, 9003: shared}
    gid1, gid2, gid_shared = 111111, 222222, 333333
    m1, binary1 = version1.manifest_zip()
    m2, binary2 = version2.manifest_zip()
    ms, _ = shared.manifest_zip()
    state.manifests = {(9001, gid1): m1, (9001, gid2): m2, (9003, gid_shared): ms}
    dead, flaky, good = ContentServer('dead', state), ContentServer('flaky', state), ContentServer('good', state)
    servers = [dead, flaky, good]
    for server in servers: serve(server)
    fixture = dict(key=KEY_MAIN.hex(), keyShared=KEY_SHARED.hex(), vectorPlain=VEC_PLAIN.hex(), vectors=VEC, rawEncrypted=RAW,
                   hosts=[server.url for server in servers] + [server.secure_url for server in servers],
                   transportOrigins={origin: server.url for server in servers for origin in (server.url, server.secure_url)},
                   requestCode=state.request_code, auth=state.auth,
                   updateNetworkBytes=len(version2.chunks[hashlib.sha1(data(60000, 99)).hexdigest()]),
                   gid1=gid1, gid2=gid2, gidShared=gid_shared, tmp=str(work / 'root'))

    def require_all_node_probe(sha, label, expected_hosts=None):
        origins = [origin for origin, requested in state.chunk_origins if requested == sha]
        require(sorted(origins) == sorted(fixture['hosts'] if expected_hosts is None else expected_hosts),
                label + ': exactly one verified-chunk attempt per declared node')

    run('units', fixture)

    # ---- an install that is interrupted: chunks were finished, no install record yet
    root_dir = work / 'root'
    drive = root_dir / 'drive_c'
    steam_apps = drive / 'Program Files (x86)/Steam/steamapps'
    folder = steam_apps / 'common/Fixture Game'
    state.limit = 4
    result = run('interrupt', fixture)
    require(result.get('install') == 'failed' and result.get('listed') == '0',
            'an interrupted install fails, and a game without an install record is not listed as installed')
    require(not (steam_apps / 'appmanifest_9000.acf').exists(), 'no install record before the install finishes')
    journals = list((steam_apps / 'downloading/9000').glob('*.journal'))
    require(journals, 'finished chunks are journaled')
    done = set()
    for journal in journals:
        m = re.match(r'depot_(\d+)_(\d+)\.journal', journal.name)
        depot = {9001: version1, 9003: shared}[int(m.group(1))]
        for line in journal.read_text().split():
            key = int(line, 16)
            done.add(depot.files[key >> 32]['chunks'][key & 0xffffffff][0].hex())
    require(len(done) >= 1 and len(state.served_ok) >= 1, f'{len(done)} chunks journaled after {len(state.served_ok)} served')
    require_all_node_probe(hashlib.sha1(GAME_BIN[:60000]).hexdigest(), 'the interrupted install benchmarks all six HTTP/HTTPS nodes')

    # ---- a journal is a resume hint, not proof that its bytes still exist.
    # Mutate one completed chunk in a copy of the interrupted install. Trusting
    # its journal would keep the wrong byte and omit the required chunk fetch.
    state.limit = None
    corrupt_root = work / 'corrupt-journal-root'
    shutil.copytree(root_dir, corrupt_root)
    corrupt_folder = corrupt_root / 'drive_c/Program Files (x86)/Steam/steamapps/common/Fixture Game'
    corrupted_sha = None
    for journal in journals:
        m = re.match(r'depot_(\d+)_(\d+)\.journal', journal.name)
        depot = {9001: version1, 9003: shared}[int(m.group(1))]
        for line in journal.read_text().split():
            key = int(line, 16)
            entry = depot.files[key >> 32]
            sha, offset, length, _, _ = entry['chunks'][key & 0xffffffff]
            if length:
                relative = entry['name'].replace('\\', '/').casefold()
                target = next(p for p in corrupt_folder.rglob('*') if p.is_file() and
                              p.relative_to(corrupt_folder).as_posix().casefold() == relative)
                with target.open('r+b') as stream:
                    stream.seek(offset)
                    byte = stream.read(1)
                    stream.seek(offset)
                    stream.write(bytes([byte[0] ^ 0xff]))
                corrupted_sha = sha.hex()
                break
        if corrupted_sha is not None:
            break
    require(corrupted_sha is not None, 'the interrupted fixture has a completed nonempty chunk to corrupt')
    state.reset_counters()
    result = run('resume-corrupt', dict(fixture, tmp=str(corrupt_root)))
    require(result.get('install') == 'ok' and set(state.chunk_requests) & done == {corrupted_sha},
            'resume re-fetches only the corrupted journaled chunk and preserves the other completed chunks')
    require(target.read_bytes() == entry['data'], 'resume repairs the actual corrupted bytes before marking the game installed')

    # ---- resume: the unchanged journaled chunks are not fetched again; the install finishes
    state.reset_counters()
    result = run('resume', fixture)
    require(result.get('install') == 'ok' and result.get('listed') == '1', "the install finishes and Madeira Dock's own scanner lists the game as installed")
    require(not (set(state.chunk_requests) & done), 'chunks finished before the interruption are not fetched again')
    evil_sha = hashlib.sha1(EVIL).hexdigest()
    require(evil_sha not in state.chunk_requests, 'a manifest path that escapes the install folder is never downloaded')
    expected1 = {'bin/game.bin': GAME_BIN, 'data/Assets.dat': ASSETS, 'empty.txt': b'', 'docs/Readme.TXT': README, 'docs/notes.txt': NOTES,
                 'tool/custom.bin': CUSTOM, 'shared/lib.dat': data(30000, 6)}
    def tree_matches(expected, target_folder=folder):
        ok = True
        for name, content in expected.items():
            file = target_folder / name
            if not file.is_file() or file.read_bytes() != content:
                print('  mismatch:', name); ok = False
        return ok
    require(tree_matches(expected1), 'every downloaded file has exactly the bytes of its depot (chunks in five encodings, encrypted names, empty file)')
    found = sorted(str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file())
    require(found == sorted(expected1), f'and nothing else is in the install folder: {found}')
    outside = [str(p.relative_to(work)) for p in work.rglob('*') if p.is_file() and p.name in ('evil.txt', 'link')]
    require(not outside, f'no file named by a hostile or symlink manifest entry exists anywhere: {outside}')
    require(not (steam_apps / 'downloading/9000').exists(), 'the resume journal is removed when the install is complete')
    record = (steam_apps / 'appmanifest_9000.acf').read_text()
    require('"StateFlags"\t\t"4"' in record and '"appid"\t\t"9000"' in record and '"installdir"\t\t"Fixture Game"' in record and '"buildid"\t\t"1000"' in record,
            'the install record: fully installed, folder, build')
    require(f'"9001"\n\t\t{{\n\t\t\t"manifest"\t\t"{gid1}"\n\t\t\t"size"\t\t"{sum(f["size"] for f in version1.files)}"' in record, 'InstalledDepots names the depot, manifest and size')
    require('"SharedDepots"\n\t{\n\t\t"9003"\t\t"9100"' in record, 'SharedDepots names the depot taken from another app')
    require('"CheckGuid"\n\t{\n\t\t"0"\t\t"tool\\\\custom.bin"' in record, 'CheckGuid lists the per-user executable, Windows style')
    require('"SizeOnDisk"\t\t"%d"' % sum(len(c) for c in expected1.values()) in record, 'SizeOnDisk is the size of the files written')
    owner = (steam_apps / 'appmanifest_9100.acf').read_text()
    require('"appid"\t\t"9100"' in owner and '"9003"' in owner and '"installdir"\t\t"Fixture Game"' in owner, "the depot's owning app has its own record")
    require((steam_apps / f'depotcache/9001_{gid1}.manifest').read_bytes() == binary1, "the manifest of a depot with per-user executables is kept in depotcache for Valve's client")
    require(not (steam_apps / f'depotcache/9003_{gid_shared}.manifest').exists(), 'other depots leave depotcache alone')
    require(state.denied == 0 and state.manifest_requests and
            all(origin.startswith('https://') for origin, _, _ in state.manifest_requests),
            'production manifests use secure per-origin authorization; HTTP chunks carry no credential')
    require(any(origin.startswith('http://') for origin, _ in state.chunk_origins) and
            any(origin.startswith('https://') for origin, _ in state.chunk_origins),
            'the resume actually exercised encrypted chunks over both fixture transports')

    # ---- a repair of a complete unchanged game checks disk before any probes.
    state.reset_counters()
    result = run('recheck', fixture)
    require(result.get('install') == 'ok' and not state.chunk_requests,
            'checking an unchanged completed install sends zero chunk requests, including CDN probes')
    require(tree_matches(expected1), 'checking an unchanged install preserves every existing game byte')
    import urllib.request, urllib.error
    def fixture_get(path, origin):
        request = urllib.request.Request(good.url + path, headers={'X-Madeira-Fixture-Origin': origin})
        try:
            with urllib.request.urlopen(request, timeout=5) as response: return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    manifest_path = f'/depot/9001/manifest/{gid1}/5/{state.request_code}'
    secure_token = f'{state.auth}-good.fixture.steamcontent.com-9001'
    require(fixture_get(manifest_path, good.secure_url)[0] == 403,
            'the fixture refuses a secure manifest without the CDN token')
    require(fixture_get(manifest_path + f'?auth={state.auth}-flaky.fixture.steamcontent.com-9001', good.secure_url)[0] == 403,
            'the fixture refuses a token issued for another origin')
    require(fixture_get(manifest_path + f'?auth={state.auth}-good.fixture.steamcontent.com-9003', good.secure_url)[0] == 403,
            'the fixture refuses a token issued for another depot')
    require(fixture_get(manifest_path + f'?auth={secure_token}', good.secure_url) == (200, m1),
            'the secure manifest requires the correct origin token and manifest request code')
    require(fixture_get(f'/depot/9001/manifest/{gid1}/5/1?auth={secure_token}', good.secure_url)[0] == 404,
            'a secure manifest with the wrong request code is refused')
    require(fixture_get(manifest_path, good.url)[0] == 403,
            'the fixture never serves a manifest through the HTTP chunk origin')
    sample_sha = hashlib.sha1(GAME_BIN[:60000]).hexdigest()
    chunk_path = '/depot/9001/chunk/' + sample_sha
    require(fixture_get(chunk_path, good.url) == (200, state.depots[9001].chunks[sample_sha]),
            'an HTTP chunk is served as encrypted bytes without any token')
    require(fixture_get(chunk_path + f'?auth={secure_token}', good.url)[0] == 403,
            'the fixture refuses credentials on an HTTP chunk origin')

    # ---- update: only what changed is fetched, files shrink
    state.reset_counters()
    result = run('update', fixture)
    new_sha = hashlib.sha1(data(60000, 99)).hexdigest()
    require(result.get('install') == 'ok' and set(state.chunk_requests) == {new_sha},
            f'an update fetches only the chunk that changed ({len(set(state.chunk_requests))} chunk(s))')
    require_all_node_probe(new_sha, 'the update probes all nodes and reuses a valid changed chunk')
    expected2 = dict(expected1); expected2['bin/game.bin'] = GAME_BIN[:60000] + data(60000, 99) + GAME_BIN[120000:]; expected2['data/Assets.dat'] = ASSETS[:70000]
    require(tree_matches(expected2), 'the updated files have the new bytes, and the file that shrank has no stale tail')
    require('"buildid"\t\t"1001"' in (steam_apps / 'appmanifest_9000.acf').read_text(), 'the record has the new build')

    # ---- uninstall: everything the install wrote goes; the scanner no longer finds the game
    run('uninstall', fixture)
    require(not folder.exists() and not (steam_apps / 'appmanifest_9000.acf').exists() and not (steam_apps / 'appmanifest_9100.acf').exists()
            and not (steam_apps / 'downloading/9000').exists(), 'uninstall removes the folder, both records and the journal')

    # ---- a depot the account is licensed for but Steam refuses fails the install and writes no record
    other = dict(fixture, tmp=str(work / 'root2'))
    state.reset_counters()
    result = run('refused-licensed', other)
    require(result.get('install') == 'failed' and result.get('listed') == '0' and
            not list((work / 'root2').rglob('appmanifest_*.acf')), 'a refused licensed depot fails the install without a record')

    # ---- the production directory path: both CM recommendations contain the
    # same three HTTP CN nodes, which refuse chunks; permitted bundled HTTPS
    # nodes must remain in the transfer pool and complete the actual install.
    # These are URL identities only: the fail-closed transport maps EVERY one
    # directly to loopback, without resolving or contacting any Steam/CDN host.
    forbidden = ContentServer('forbidden', state)
    servers.append(forbidden)
    serve(forbidden)
    cn_hosts = ['st.dl.eccdnx.com', 'dl.steam.clngaa.com', 'xz.pphimalayanrt.com']
    fallback_hosts = ['cache2-hkg1.steamcontent.com', 'cache4-hkg1.steamcontent.com',
                      'fastly.cdn.steampipe.steamcontent.com', 'steampipe.akamaized.net']
    cn_origins = ['http://' + host for host in cn_hosts]
    fallback_origins = ['https://' + host for host in fallback_hosts]
    regression_origins = {**{origin: forbidden.url for origin in cn_origins},
                          **{origin: good.url for origin in fallback_origins}}
    state.extra_origins = regression_origins
    state.limit = None
    state.reset_counters()
    regression = dict(fixture, tmp=str(work / 'directory-root'), directoryHosts=cn_hosts,
                      authorizedSecureHosts=fallback_hosts,
                      hosts=cn_origins + fallback_origins, transportOrigins=regression_origins)
    result = run('directory-fallback', regression, timeout=120)
    require(result.get('install') == 'ok' and result.get('listed') == '1',
            'the real directory-to-install path survives three CN HTTP 403 nodes using permitted HTTPS transfer fallbacks')
    first_sha = hashlib.sha1(GAME_BIN[:60000]).hexdigest()
    shared_sha = hashlib.sha1(data(30000, 6)).hexdigest()
    require_all_node_probe(first_sha, 'production discovery preserves all three CN nodes and four secure fallbacks', regression['hosts'])
    require_all_node_probe(shared_sha, 'the shared depot also benchmarks the discovered and secure fallback pool', regression['hosts'])
    require(sorted(origin for origin, sha in state.chunk_refusals if sha == first_sha) == sorted(cn_origins),
            'each declared CN HTTP node actually returned 403 for the representative encrypted chunk')
    require(state.denied == 0 and state.manifest_requests and
            all(origin in fallback_origins for origin, _, _ in state.manifest_requests),
            'directory regression manifests and CDN tokens stay on authorized secure fallback origins')
    regression_apps = work / 'directory-root/drive_c/Program Files (x86)/Steam/steamapps'
    regression_folder = regression_apps / 'common/Fixture Game'
    require(tree_matches(expected1, regression_folder),
            'HTTPS fallback installs every file with the independent original depot bytes')
    require(sorted(str(p.relative_to(regression_folder)) for p in regression_folder.rglob('*') if p.is_file()) == sorted(expected1),
            'the directory regression creates exactly the permitted files and excludes hostile paths')
    regression_record = regression_apps / 'appmanifest_9000.acf'
    require(regression_record.is_file() and '"StateFlags"\t\t"4"' in regression_record.read_text() and
            '"buildid"\t\t"1000"' in regression_record.read_text() and
            (regression_apps / 'appmanifest_9100.acf').is_file() and
            not (regression_apps / 'downloading/9000').exists(),
            'the production directory regression commits both installed records and retires its journal')
finally:
    for server in servers:
        server.shutdown(); server.server_close()
    if os.environ.get('MADEIRA_KEEP_WORK'):
        print('kept:', work)
    else:
        shutil.rmtree(work, ignore_errors=True)

if failures:
    print(f'check-steam-library: {failures} FAILED')
    sys.exit(1)
print('check-steam-library: PASS')
