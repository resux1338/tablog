#!/usr/bin/env bash
# Runs every test. Nothing outside a temporary directory is touched.
set -u
here=$(cd "$(dirname "$0")" && pwd)
rc=0
python3 "$here/test_report.py" || rc=1
bash "$here/test_shell.sh" || rc=1
exit $rc
