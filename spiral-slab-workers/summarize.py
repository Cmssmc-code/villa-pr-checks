#!/usr/bin/env python3
"""Write the run summary as Markdown (stdout and <work dir>/logs/summary.md) and exit 1 when
a check fails.

usage: python summarize.py <main tree> <branch tree> <work dir>

Reads <work dir>/extraction.json (check_extraction.py), <work dir>/predictions.json
(fetch_predictions.py) and the pytest results <work dir>/tests-{main,branch}.xml and .rc.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib import metadata
from pathlib import Path

PACKAGES = ('numpy', 'zarr', 'numcodecs', 'kimimaro', 'connected-components-3d', 'networkx',
            'vc-reader', 'pytest')


def git(tree, *args):
    return subprocess.run(['git', '-C', str(tree), *args], capture_output=True,
                          text=True).stdout.strip()


def tests(work, tree):
    rc_file, xml_file = work / f'tests-{tree}.rc', work / f'tests-{tree}.xml'
    if not rc_file.exists() or not xml_file.exists():
        return None
    counts = dict(rc=int(rc_file.read_text().strip() or -1), tests=0, failures=0, errors=0,
                  skipped=0, time=0.0)
    for suite in ET.parse(xml_file).getroot().iter('testsuite'):
        for key in ('tests', 'failures', 'errors', 'skipped'):
            counts[key] += int(suite.get(key, 0))
        counts['time'] += float(suite.get('time', 0))
    counts['passed'] = counts['tests'] - counts['failures'] - counts['errors'] - counts['skipped']
    failed = []
    for case in ET.parse(xml_file).getroot().iter('testcase'):
        if case.find('failure') is not None or case.find('error') is not None:
            failed.append(f'{case.get("classname", "")}::{case.get("name", "")}')
    counts['failed'] = failed
    return counts


def main():
    main_tree, branch_tree, work = (Path(p) for p in sys.argv[1:4])
    linux = sys.platform.startswith('linux')
    extraction = json.loads((work / 'extraction.json').read_text())
    runs, pairs = extraction['runs'], {(p['a'], p['b']): p for p in extraction['pairs']}
    predictions = json.loads((work / 'predictions.json').read_text())
    test_results = {tree: tests(work, tree) for tree in ('main', 'branch')}

    checks = []

    def check(text, ok):
        checks.append((text, bool(ok)))

    def same_tracks(a, b):
        p = pairs[(a, b)]
        return 'skipped' not in p and p['same_keys'] and p['identical_tracks'] == p['common']

    def same_bytes(a, b):
        p = pairs[(a, b)]
        return 'skipped' not in p and p['same_keys'] and p['identical_bytes'] == p['common']

    def workers(name, how):
        return runs[name]['children'].get(how, 0) >= 2 and runs[name]['most_children'] >= 2

    main_vc, branch_vc = git(main_tree, 'rev-parse', 'HEAD:vc_reader'), git(branch_tree, 'rev-parse', 'HEAD:vc_reader')
    check('vc_reader/ is the same in both trees (git tree hash), so one build serves both',
          main_vc and main_vc == branch_vc)
    branch_tests = test_results['branch']
    check('tests pass on the branch',
          branch_tests and branch_tests['rc'] == 0 and branch_tests['tests'] > 0)
    for name, result in runs.items():
        if name != 'main-sw2':
            check(f'{name} finishes (exit code 0)', result['rc'] == 0)
    check('main-default and branch-default: same keys, same tracks for every key',
          same_tracks('main-default', 'branch-default'))
    check('main-1proc and branch-1proc: same keys, identical pickled bytes for every key',
          same_bytes('main-1proc', 'branch-1proc'))
    check('branch-1proc and branch-sw2: same keys, identical pickled bytes for every key',
          same_bytes('branch-1proc', 'branch-sw2'))
    check('main-1proc and branch-sw2: same keys, identical pickled bytes for every key',
          same_bytes('main-1proc', 'branch-sw2'))
    if linux:
        check('branch-sw2 ran two forked slab workers at once',
              workers('branch-sw2', "forked (the run's command line)"))
    else:
        check('branch-sw2 ran two spawned slab workers at once',
              workers('branch-sw2', 'spawned (spawn_main)'))
    # On main, parse_args reads sys.platform without importing sys: NameError on every OS.
    check('main-sw2 (main with --slab-workers 2) stops with an error',
          runs['main-sw2']['rc'] != 0)

    system = platform.system()
    if system == 'Darwin':
        system = f'macOS {platform.mac_ver()[0]}'
    else:
        try:
            system = platform.freedesktop_os_release().get('PRETTY_NAME', system)
        except OSError:
            pass
    versions = []
    for package in PACKAGES:
        try:
            versions.append(f'{package} {metadata.version(package)}')
        except metadata.PackageNotFoundError:
            versions.append(f'{package} (not installed)')
    env = os.environ
    out = []
    ok = all(passed for _, passed in checks)
    out.append(f'## spiral-fitting `--slab-workers` on {system} {platform.machine()}: '
               f'{"all checks pass" if ok else "CHECK FAILED"}\n')
    out.append(f'- main: [ScrollPrize/villa@{git(main_tree, "rev-parse", "--short=9", "HEAD")}]'
               f'(https://github.com/ScrollPrize/villa/commit/{git(main_tree, "rev-parse", "HEAD")})')
    out.append(f'- branch: [Cmssmc-code/villa@{git(branch_tree, "rev-parse", "--short=9", "HEAD")}]'
               f'(https://github.com/Cmssmc-code/villa/commit/{git(branch_tree, "rev-parse", "HEAD")})'
               f' (spiral-slab-workers-windows)')
    out.append(f'- Python {platform.python_version()}, {os.cpu_count()} CPUs; {", ".join(versions)}')
    out.append(f'- Data: `{env.get("PREDICTIONS", "")}/{predictions["level"]}`, z '
               f'{predictions["z_min"]}..{predictions["z_max"]} (chunk rows {predictions["rows"]}: '
               f'{predictions["chunk_files"]} chunk files, {predictions["chunk_bytes"] / 1e6:.1f} MB, '
               f'SHA-256 of .zarray and chunks `{predictions["sha256"][:16]}`)')
    out.append('- Command: `python extract_surface_tracks.py --backend cpu --predictions <array> '
               '--out <run>.dbm --z-min Z0 --z-max Z1 --no-packed-store [options]`, run in each '
               "tree's spiral-fitting/\n")

    out.append('### Checks\n')
    out.append('| check | result |\n|---|---|')
    out += [f'| {text} | {"PASS" if passed else "FAIL"} |' for text, passed in checks]

    out.append('\n### Extraction runs\n')
    out.append('| run | tree | options | exit code | time (s) | child processes seen | keys | '
               'tracks | vertices | fingerprint |\n|---|---|---|---|---|---|---|---|---|---|')
    for name, r in runs.items():
        children = ', '.join(f'{n} {k}' for k, n in sorted(r['children'].items())) or 'none'
        if r['rc'] == 0:
            cells = f'{r["keys"]} | {r["tracks"]} | {r["vertices"]} | `{r["fingerprint"]}`'
        else:
            cells = f'`{r["error"][:120]}` | | |'
        out.append(f'| {name} | {r["tree"]} | `{r["options"]}` | {r["rc"]} | {r["seconds"]} | '
                   f'{children} (at most {r["most_children"]} at once) | {cells} |')

    out.append('\n### Comparisons (logs/compare-*.txt)\n')
    out.append('| a | b | same keys | keys with identical pickled bytes | '
               'keys with identical track sets |\n|---|---|---|---|---|')
    for p in extraction['pairs']:
        if 'skipped' in p:
            out.append(f'| {p["a"]} | {p["b"]} | not compared: {p["skipped"]} | | |')
        else:
            out.append(f'| {p["a"]} | {p["b"]} | {p["same_keys"]} | '
                       f'{p["identical_bytes"]}/{p["common"]} | {p["identical_tracks"]}/{p["common"]} |')

    out.append('\n### Tests: `pytest tests/test_cc_labels.py tests/test_extract_surface_tracks.py`'
               ' in spiral-fitting/\n')
    out.append('| tree | exit code | passed | failed | errors | skipped | time (s) | failing tests |'
               '\n|---|---|---|---|---|---|---|---|')
    for tree, t in test_results.items():
        if t is None:
            out.append(f'| {tree} | did not run | | | | | | |')
        else:
            out.append(f'| {tree} | {t["rc"]} | {t["passed"]} | {t["failures"]} | {t["errors"]} | '
                       f'{t["skipped"]} | {t["time"]:.1f} | {", ".join(t["failed"]) or "none"} |')
    text = '\n'.join(out) + '\n'
    (work / 'logs' / 'summary.md').write_text(text)
    print(text)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
