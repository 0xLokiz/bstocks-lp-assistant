"""Low-level I/O: the public HTTP client (`_get`) and the `baw` CLI subprocess wrapper.

Every other module that needs to call `_get`/`baw` imports this module qualified
(`from bstocks_lp import api`, then `api._get(...)` / `api.baw(...)`) rather than
`from bstocks_lp.api import _get, baw` -- see the package README note / MODEL.md's sibling
architecture note for why: it's what keeps `monkeypatch.setattr(api, "baw", fake)` reach every
caller, not just ones that happen to import this module's names directly.
"""

import json
import os
import random
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "binance-web3/1.1 (Skill)"

HTTP_MAX_RETRIES = 3
HTTP_BASE_RETRY_DELAY = 0.5  # seconds; exponential backoff with jitter, see _get


def _get(url, params, max_retries=HTTP_MAX_RETRIES):
    """GET url?params as JSON, with bounded retry-with-jitter on transient failures (timeout,
    connection error, 5xx, 429 rate-limit) -- not on a 4xx client error otherwise, which won't
    fix itself on retry, and not on a malformed/wrong-shaped response body, which more likely
    means a real API contract problem than a network blip. Raises RuntimeError with the
    failure classified and enough context to diagnose (url, status if any, attempt count) once
    retries are exhausted, instead of a bare urllib traceback.
    """
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(full_url, headers={"Accept-Encoding": "identity", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"GET {full_url} returned non-JSON response: {raw[:200]!r}") from e
            if not isinstance(body, dict):
                raise RuntimeError(f"GET {full_url} returned unexpected JSON shape "
                                    f"(expected an object, got {type(body).__name__})")
            return body
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_error = e
            else:
                raise RuntimeError(f"GET {full_url} failed: HTTP {e.code} {e.reason}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
        if attempt < max_retries:
            delay = HTTP_BASE_RETRY_DELAY * (2 ** (attempt - 1)) * (1 + random.random())
            time.sleep(delay)
    raise RuntimeError(f"GET {full_url} failed after {max_retries} attempts: {last_error}") from last_error


_BAW_SHELL_METACHARACTERS = set('&|<>^%!"\'\r\n\0')
_BAW_PATH = None


def _validate_baw_arg(arg):
    """Reject anything that could be interpreted as a shell metacharacter.

    Belt-and-suspenders even with shell=False: on Windows, `baw` resolves to a
    `baw.cmd` npm shim, and CreateProcess's documented fallback for .bat/.cmd targets
    internally re-invokes cmd.exe regardless of the Python-level shell= flag, so a
    value built from API data or a CLI flag (investmentId, defiProtocolId, ticker)
    could still reach cmd.exe's own parser. Blocking its metacharacters here closes
    that gap without depending on exactly how Windows dispatches the child process.
    """
    s = str(arg)
    if not s:
        raise ValueError("baw() received an empty argument")
    bad = _BAW_SHELL_METACHARACTERS & set(s)
    if bad:
        raise ValueError(f"baw() argument contains disallowed character(s) {sorted(bad)!r}: {s!r}")
    return s


def _resolve_baw_path():
    global _BAW_PATH
    if _BAW_PATH is None:
        path = shutil.which("baw")
        if not path:
            raise RuntimeError("baw CLI not found on PATH -- install the Binance Agentic Wallet CLI first")
        _BAW_PATH = path
    return _BAW_PATH


def baw(*args):
    """Shell out to the `baw` CLI and parse its --json output.

    Runs with shell=False against baw's resolved absolute path. On Windows, baw
    resolves to a `baw.cmd` npm shim, which needs cmd.exe as an interpreter; letting
    Windows' own CreateProcess .cmd fallback invoke that cmd.exe implicitly was tried
    and measured to corrupt non-ASCII output (confirmed live: real pool names came
    back as e.g. 'USDT-\u0163\ufffd\ufffd' instead of their Chinese text), because that
    implicit cmd.exe uses the system OEM codepage rather than UTF-8. So cmd.exe is
    invoked explicitly here with `chcp 65001` first, same as before -- the difference
    from the old implementation is that every argument is validated by
    _validate_baw_arg to exclude shell metacharacters before being embedded in the
    command string, which is what actually closes the injection risk (list2cmdline
    only does CRT-style argv quoting, not shell-metacharacter escaping).
    """
    baw_path = _resolve_baw_path()
    validated = [_validate_baw_arg(a) for a in args]
    if os.name == "nt":
        inner = subprocess.list2cmdline([baw_path, *validated, "--json"])
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        cmd = [comspec, "/d", "/c", f"chcp 65001>nul & {inner}"]
    else:
        cmd = [baw_path, *validated, "--json"]
    # check=False (explicit): a nonzero exit is handled below via stdout/returncode inspection,
    # not by letting subprocess.run raise CalledProcessError -- baw can exit nonzero while still
    # writing a useful --json error body to stdout, which check=True would discard.
    result = subprocess.run(cmd, capture_output=True, timeout=30, shell=False, check=False,
                             text=True, encoding="utf-8", errors="replace")
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(
            f"baw {' '.join(args)} produced no output (exit code {result.returncode}, stderr: {result.stderr.strip()})"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    # Recovery path only: some stray text before the real --json payload (a warning/deprecation
    # line, say) is the one case the strict parse above doesn't handle. `find("{")` isn't a safe
    # first choice, though -- if that leading text itself contains a literal `{` before the
    # payload starts, slicing from it produces a truncated/wrong-boundary string instead of the
    # real JSON. Only fall back to it after the honest parse has already failed, and only as a
    # best-effort recovery, not the primary path.
    first_brace = stdout.find("{")
    if first_brace > 0:
        try:
            return json.loads(stdout[first_brace:])
        except json.JSONDecodeError:
            pass
    raise RuntimeError(
        f"baw {' '.join(args)} produced non-JSON output (exit code {result.returncode}): {stdout[:500]!r}"
    )
