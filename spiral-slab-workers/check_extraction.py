#!/usr/bin/env python3
"""Run spiral-fitting/extract_surface_tracks.py (CPU backend) from two villa trees on the
same predictions, and compare the tracks DBMs the runs write.

usage: python check_extraction.py <main tree> <branch tree> <predictions array> <z min> <z max> <work dir>

Each run is logged to <work dir>/logs/<run>.log, each comparison to
<work dir>/logs/compare-<a>-vs-<b>.txt, and <work dir>/extraction.json gathers the results.
While a run is going, its child processes are listed with ps every second: a worker
started by multiprocessing's spawn runs spawn_main, a forked worker keeps the command line
of the run.
"""
from __future__ import annotations

import dbm
import hashlib
import json
import pickle
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

# (run, tree, options). "1proc": one slab at a time and one Kimimaro process, so the whole
# extraction runs in a single process.
RUNS = [
    ('main-default', 'main', []),
    ('branch-default', 'branch', []),
    ('main-1proc', 'main', ['--kimimaro-parallel', '1']),
    ('branch-1proc', 'branch', ['--kimimaro-parallel', '1']),
    ('branch-sw2', 'branch', ['--slab-workers', '2', '--kimimaro-parallel', '1']),
    ('main-sw2', 'main', ['--slab-workers', '2', '--kimimaro-parallel', '1']),
]
PAIRS = [
    ('main-default', 'branch-default'),
    ('main-1proc', 'branch-1proc'),
    ('branch-1proc', 'branch-sw2'),
    ('main-1proc', 'main-sw2'),
    ('branch-sw2', 'main-sw2'),
    ('main-1proc', 'main-default'),
]
TIMEOUT = 40 * 60


def load(path):
    with dbm.open(str(path), 'r') as db:
        return {key.decode(): bytes(db[key]) for key in db.keys()}


def canonical(raw):
    # The set of tracks of a key, independent of their order.
    return sorted(track.tolist() for track in pickle.loads(raw))


def describe(entries):
    """Keys, tracks, vertices and a fingerprint of the sorted track sets of a DBM."""
    digest = hashlib.sha256()
    tracks = vertices = 0
    for key in sorted(entries):
        loaded = pickle.loads(entries[key])
        tracks += len(loaded)
        vertices += sum(len(track) for track in loaded)
        digest.update(key.encode())
        digest.update(repr(canonical(entries[key])).encode())
    return dict(keys=len(entries), tracks=tracks, vertices=vertices,
                fingerprint=digest.hexdigest()[:16])


def compare(a_name, a, b_name, b):
    common = sorted(a.keys() & b.keys())
    same_bytes = sum(a[k] == b[k] for k in common)
    differing = [k for k in common if canonical(a[k]) != canonical(b[k])]
    info = describe({k: a[k] for k in common})
    lines = [
        f'=== compare {a_name} vs {b_name}',
        f'{a_name}.dbm: {len(a)} keys; {b_name}.dbm: {len(b)} keys',
        f'same keys: {a.keys() == b.keys()}',
        f'keys with identical pickled bytes: {same_bytes}/{len(common)}',
        f'keys with identical track sets: {len(common) - len(differing)}/{len(common)}; '
        f'differing: {differing[:10]}',
        f'tracks: {info["tracks"]}, vertices: {info["vertices"]}',
        f'fingerprint of sorted track sets ({a_name}): {info["fingerprint"]}',
    ]
    result = dict(a=a_name, b=b_name, same_keys=a.keys() == b.keys(), common=len(common),
                  identical_bytes=same_bytes, identical_tracks=len(common) - len(differing),
                  differing=differing[:10])
    return result, '\n'.join(lines) + '\n'


def processes_under(root):
    """The command line of root and (pid, command line) of every process under it, from ps."""
    listing = subprocess.run(['ps', '-ww', '-A', '-o', 'pid=,ppid=,command='],
                             capture_output=True, text=True).stdout
    parent, command = {}, {}
    for line in listing.splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            pid = int(fields[0])
            parent[pid] = int(fields[1])
            command[pid] = fields[2] if len(fields) > 2 else ''
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        for child in (p for p, pp in parent.items() if pp == pid):
            found.append((child, command[child]))
            todo.append(child)
    return command.get(root, ''), found


def kind(command, root_command):
    if 'resource_tracker' in command:
        return 'resource tracker'
    if 'spawn_main' in command:
        return 'spawned (spawn_main)'
    if root_command and command == root_command:
        return "forked (the run's command line)"
    return 'other'


def run(name, tree, options, trees, predictions, z_min, z_max, work):
    out = work / 'tracks' / f'{name}.dbm'
    log_path = work / 'logs' / f'{name}.log'
    cmd = [sys.executable, 'extract_surface_tracks.py', '--backend', 'cpu',
           '--predictions', predictions, '--out', str(out), '--z-min', z_min, '--z-max', z_max,
           '--no-packed-store', *options]
    cwd = trees[tree] / 'spiral-fitting'
    commit = subprocess.run(['git', '-C', str(trees[tree]), 'rev-parse', 'HEAD'],
                            capture_output=True, text=True).stdout.strip()
    seen, most = {}, 0
    with open(log_path, 'w') as log:
        log.write(f'# {name}: {tree} tree, commit {commit}\n# cwd: {cwd}\n'
                  f'# command: {shlex.join(cmd)}\n')
        log.flush()
        start = time.monotonic()
        process = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        while True:
            try:
                rc = process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - start > TIMEOUT:
                process.kill()
                process.wait()
                rc = 'timeout'
                break
            root_command, children = processes_under(process.pid)
            sample = {pid: kind(c, root_command) for pid, c in children}
            most = max(most, sum(k != 'resource tracker' for k in sample.values()))
            for pid, k in sample.items():
                seen.setdefault(pid, k)
        seconds = time.monotonic() - start
        kinds = {}
        for k in seen.values():
            kinds[k] = kinds.get(k, 0) + 1
        children = ', '.join(f'{n} {k}' for k, n in sorted(kinds.items())) or 'none seen'
        log.write(f'\n# exit code {rc}, {seconds:.1f} s\n'
                  f'# child processes seen (ps every second): {children}; '
                  f'at most {most} at once, resource tracker excluded\n')
    text = log_path.read_text(errors='replace').splitlines()
    error = ''
    if rc != 0:
        # The exception or argparse error that stopped the run, else its last line.
        body = [line.strip() for line in text if line.strip() and not line.startswith('#')]
        errors = [line for line in body
                  if re.match(r'[A-Za-z_][\w.]*(Error|Exception)\b', line) or ': error: ' in line]
        error = (errors or body or [''])[-1]
    result = dict(tree=tree, commit=commit, options=' '.join(options) or '(defaults)', rc=rc,
                  seconds=round(seconds, 1), children=kinds, most_children=most, error=error)
    if rc == 0:
        result.update(describe(load(out)))
    print(f'--- {name}: exit code {rc}, {seconds:.1f} s, children: {children}, at most {most}'
          + (f', {error}' if error else ''))
    print('\n'.join(text[-6:]))
    return result


def main():
    main_tree, branch_tree, predictions, z_min, z_max, work = sys.argv[1:7]
    trees = dict(main=Path(main_tree).resolve(), branch=Path(branch_tree).resolve())
    work = Path(work).resolve()
    (work / 'logs').mkdir(parents=True, exist_ok=True)
    (work / 'tracks').mkdir(parents=True, exist_ok=True)
    runs = {}
    for name, tree, options in RUNS:
        runs[name] = run(name, tree, options, trees, predictions, z_min, z_max, work)
    pairs = []
    for a, b in PAIRS:
        if runs[a]['rc'] != 0 or runs[b]['rc'] != 0:
            pairs.append(dict(a=a, b=b, skipped='a run did not finish'))
            continue
        result, text = compare(a, load(work / 'tracks' / f'{a}.dbm'),
                               b, load(work / 'tracks' / f'{b}.dbm'))
        (work / 'logs' / f'compare-{a}-vs-{b}.txt').write_text(text)
        print(text, end='')
        pairs.append(result)
    (work / 'extraction.json').write_text(json.dumps(dict(runs=runs, pairs=pairs), indent=1) + '\n')


if __name__ == '__main__':
    main()
