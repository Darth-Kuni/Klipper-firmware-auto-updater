#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this launcher as your normal Linux user, without sudo.'
    printf '%s\n' 'The setup guide requests sudo only for the steps that need it.'
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.10 or newer is required. Install Python 3 and run this launcher again.' >&2
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    printf '%s\n' 'Python 3.10 or newer is required. Use a supported Linux Python version.' >&2
    exit 1
fi

KCM_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# Load this checkout first while retaining the caller's working directory for
# relative command-line paths.
exec python3 -c '
import runpy
import sys
sys.path.insert(0, sys.argv.pop(1))
runpy.run_module("kcm", run_name="__main__", alter_sys=True)
' "${KCM_DIR}" "$@"
