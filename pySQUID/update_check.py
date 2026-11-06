'''
Check whether the git clone pySQUID was installed from is behind its upstream.

Called at the start of Camera.__init__. Never raises on a failed check: any
problem is reported as a warning and startup continues. The only way this
stops startup is if the user answers Y to quitting so they can pull and
reinstall.

Assumes pySQUID was installed with `pip install .` from a git clone. The
clone's location is read from the direct_url.json file pip records at install
time, falling back to the package's parent directory when running from a clone.
'''

import importlib.metadata
import json
import os
import subprocess
from urllib.parse import urlparse
from urllib.request import url2pathname

FETCH_TIMEOUT_S = 5  # Max seconds to wait on the network before giving up


def _git(args, cwd, timeout=FETCH_TIMEOUT_S):
    '''Run a git command in `cwd` and return its stdout (stripped).

    Raises RuntimeError with git's message on failure. GIT_TERMINAL_PROMPT=0
    stops git from waiting for a username or password.
    '''
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    try:
        result = subprocess.run(
            ['git', *args], cwd=cwd, env=env, capture_output=True,
            text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {timeout} s contacting the remote")
    if result.returncode != 0:
        first_line = (result.stderr.strip().splitlines() or [f"'git {args[0]}' failed"])[0]
        raise RuntimeError(first_line)
    return result.stdout.strip()


def _find_clone():
    '''Return the path of the git clone pySQUID was installed from, or None.'''
    candidates = []
    try:
        dist = importlib.metadata.distribution('pySQUID')
        text = dist.read_text('direct_url.json')
        if text:
            url = json.loads(text).get('url', '')
            if url.startswith('file:'):
                candidates.append(url2pathname(urlparse(url).path))
    except importlib.metadata.PackageNotFoundError:
        pass
    # Fallback: running from a clone, where this file sits in <clone>/pySQUID/
    candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    for path in candidates:
        if os.path.exists(os.path.join(path, '.git')):
            return path
    return None


def _commits_behind():
    '''Fetch from the remote and return (number of new upstream commits, upstream name).

    Raises RuntimeError if the check can't be completed.
    '''
    clone = _find_clone()
    if clone is None:
        raise RuntimeError("no git clone found for this installation")

    try:
        upstream = _git(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'], clone)
    except RuntimeError:
        raise RuntimeError("the current branch has no upstream branch set")

    _git(['fetch', '--quiet'], clone)
    behind = int(_git(['rev-list', '--count', 'HEAD..@{u}'], clone))
    return behind, upstream


def check_for_updates():
    '''Report whether pySQUID is up to date; offer to quit if it isn't.

    Prints one of:
      - "pySQUID is up to date."
      - a warning if the check could not be completed (startup continues)
      - a prompt to quit so the user can git pull and pip install .

    Raises SystemExit only if the user answers Y to that prompt.
    '''
    try:
        behind, upstream = _commits_behind()
    except Exception as e:
        print(f"\nWARNING: could not check for pySQUID updates ({e}). Continuing.")
        return

    if behind == 0:
        print("pySQUID is up to date.")
        return

    print(f"\npySQUID is {behind} commit(s) behind {upstream}.")
    try:
        answer = input('Quit now so you can git pull and pip install . ?  Y/[N] > ').strip()
    except EOFError:
        answer = ""
    if answer.upper() == 'Y':
        print("Quitting. In your pySQUID clone, run `git pull` and `pip install .`, then start again.")
        raise SystemExit(0)
    print("Continuing with the installed version.")
