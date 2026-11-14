#!/usr/bin/env python3
"""Download the chunk rows of a public zarr v2 array that a z range reads.

usage: python fetch_predictions.py <zarr group URL> <local .zarr dir> <level> <z min> <z max> [--json FILE]
       python fetch_predictions.py --digest-only <local .zarr dir> <level> <z min> <z max>

Downloads <level>/.zarray, the group's .zgroup and .zattrs, and every chunk of the chunk
rows that intersect [z min, z max), as listed by the bucket. zarr reads a missing chunk as
the fill value, and an extraction with --z-min/--z-max in that range reads no other row.

Prints the number of chunks, their size and a SHA-256 digest over the relative path, size
and SHA-256 of <level>/.zarray and of every chunk file of those rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def fetch(url, attempts=4):
    """The body of url, or None for a 404; retries other errors."""
    error = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            error = exc
        except OSError as exc:
            error = exc
        time.sleep(2 + 3 * attempt)
    raise RuntimeError(f'{url}: {error}')


def list_keys(bucket, prefix):
    """(key, size) of every object under prefix (S3 ListObjectsV2, anonymous)."""
    token = None
    while True:
        query = {'list-type': '2', 'prefix': prefix, 'max-keys': '1000'}
        if token:
            query['continuation-token'] = token
        page = fetch(bucket + '?' + urllib.parse.urlencode(query)).decode()
        for key, size in re.findall(r'<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>', page, re.S):
            yield key, int(size)
        match = re.search(r'<NextContinuationToken>([^<]+)</NextContinuationToken>', page)
        if not match:
            return
        token = match.group(1)


def chunk_rows(zarray, z_min, z_max):
    if zarray.get('dimension_separator', '.') != '/':
        raise SystemExit('only arrays with "/" chunk keys are supported')
    depth = zarray['chunks'][0]
    return range(z_min // depth, (z_max - 1) // depth + 1)


def digest(local, level, rows):
    """(chunk count, chunk bytes, SHA-256 hex) of <level>/.zarray and the chunks of rows."""
    files = [local / level / '.zarray']
    for row in rows:
        files += sorted(p for p in (local / level / str(row)).rglob('*') if p.is_file())
    lines = []
    for path in files:
        data = path.read_bytes()
        lines.append(f'{path.relative_to(local).as_posix()}\t{len(data)}\t'
                     f'{hashlib.sha256(data).hexdigest()}\n')
    lines.sort()
    chunks = len(files) - 1
    size = sum(p.stat().st_size for p in files[1:])
    return chunks, size, hashlib.sha256(''.join(lines).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--digest-only', action='store_true',
                        help='compute the digest of an existing local copy, download nothing')
    parser.add_argument('--json', metavar='FILE', help='also write the result to FILE')
    parser.add_argument('args', nargs='+')
    opts = parser.parse_args()
    if opts.digest_only:
        local, level, z_min, z_max = opts.args
        url = None
    else:
        url, local, level, z_min, z_max = opts.args
    local, z_min, z_max = Path(local), int(z_min), int(z_max)

    if url is not None:
        parts = urllib.parse.urlsplit(url.rstrip('/'))
        bucket = f'{parts.scheme}://{parts.netloc}/'
        prefix = parts.path.lstrip('/') + '/'
        for name in ('.zgroup', '.zattrs', f'{level}/.zarray'):
            data = fetch(bucket + urllib.parse.quote(prefix + name))
            if data is not None:
                (local / name).parent.mkdir(parents=True, exist_ok=True)
                (local / name).write_bytes(data)
        zarray = json.loads((local / level / '.zarray').read_text())
        rows = chunk_rows(zarray, z_min, z_max)
        jobs = []
        for row in rows:
            for key, size in list_keys(bucket, f'{prefix}{level}/{row}/'):
                jobs.append((key, size, local / key[len(prefix):]))

        def download(job):
            key, size, dest = job
            data = fetch(bucket + urllib.parse.quote(key))
            if data is None or len(data) != size:
                raise RuntimeError(f'{key}: expected {size} bytes')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        start = time.monotonic()
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(download, jobs))
        print(f'downloaded {len(jobs)} chunks of rows {list(rows)} in '
              f'{time.monotonic() - start:.0f} s from {url}/{level}')

    zarray = json.loads((local / level / '.zarray').read_text())
    rows = chunk_rows(zarray, z_min, z_max)
    chunks, size, sha = digest(local, level, rows)
    result = dict(level=level, z_min=z_min, z_max=z_max, shape=zarray['shape'],
                  chunks=zarray['chunks'], dtype=zarray['dtype'], rows=list(rows),
                  chunk_files=chunks, chunk_bytes=size, sha256=sha)
    print(f'level {level}, shape {zarray["shape"]}, chunks {zarray["chunks"]}, '
          f'z {z_min}..{z_max} -> chunk rows {list(rows)}: {chunks} chunk files, '
          f'{size / 1e6:.1f} MB, SHA-256 of .zarray and chunks {sha}')
    if opts.json:
        Path(opts.json).write_text(json.dumps(result, indent=1) + '\n')


if __name__ == '__main__':
    sys.exit(main())
