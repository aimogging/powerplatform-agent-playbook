#!/usr/bin/env python3
"""Run every tool's offline self-test (no network, no tokens). Exit 1 if any fails.

    python tools/run-self-tests.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = [
    ['scrub-check.py', '--self-test'],
    ['flowcheck.py', '--self-test'],
    ['canvas-lint.py', '--self-test'],
    ['msapp-tool.py', '--self-test'],
    ['build-flow-package.py', '--self-test'],
    ['appchecker-sarif.py', '--self-test'],
    ['parse-appcheck.py', '--self-test'],
    ['add-copy-buttons.py', '--self-test'],
    ['deploy.py', '--self-test'],
    ['-m', 'devtenant', 'self-test'],
]


def main():
    failed = []
    for t in TESTS:
        r = subprocess.run([sys.executable] + t, cwd=HERE, capture_output=True, text=True)
        verdict = 'PASS' if r.returncode == 0 and 'self-test: PASS' in r.stdout else 'FAIL'
        print('%s  %s' % (verdict, ' '.join(t)))
        if verdict == 'FAIL':
            failed.append(t)
            print(r.stdout[-3000:] + r.stderr[-3000:])
    print('%d/%d self-tests passed' % (len(TESTS) - len(failed), len(TESTS)))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
