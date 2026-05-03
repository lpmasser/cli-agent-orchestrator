"""Compatibility shim for running CAO with psmux on Windows.

Status: dormant since 2026-04-28 — verified unnecessary on psmux v3.3.4+
and removed from auto-import in ``__init__.py``. The patches below remain
intact as a fallback for older psmux versions or future regressions.

Historically psmux had these differences from real tmux that broke libtmux:

1. Only handled U+001E as format separator (not U+241E).
2. Did not support concatenated flags (``-Fvalue``); required ``-F value``.
3. Did not support ``$N`` session IDs in ``-t`` arguments.
4. ``new-session -PF`` could return fewer format values than expected.

All four are addressed natively in psmux v3.3.4+. To re-enable the shim
for an older psmux version, import and invoke ``apply()`` before any
libtmux call::

    from cli_agent_orchestrator._psmux_compat import apply
    apply()

All patches are guarded by ``sys.platform == "win32"`` and have no effect
on Linux/macOS.
"""

import os
import shutil
import subprocess
import sys


def apply():
    """Apply psmux compatibility patches. No-op on non-Windows.

    Verified unnecessary on psmux v3.3.4+ (2026-04-28), which natively
    handles libtmux's default U+241E separator, ``-Fvalue`` concatenated
    flags, ``$N`` session ID lookups, and ``new-session -PF`` field counts.
    Retained for fallback if regression appears or for older psmux versions.
    """
    if sys.platform != "win32":
        return

    # Patch 1: Use U+001E control character as format separator.
    if "LIBTMUX_TMUX_FORMAT_SEPARATOR" not in os.environ:
        os.environ["LIBTMUX_TMUX_FORMAT_SEPARATOR"] = "\x1e"

    try:
        import libtmux.formats as _formats
        from libtmux import exc as _exc
        from libtmux import neo as _neo
        from libtmux import pane as _pane
        from libtmux import server as _server
        from libtmux import session as _session
        from libtmux import window as _window
        from libtmux.common import tmux_cmd as _tmux_cmd

        # Force separator in case libtmux was imported before our env var.
        _formats.FORMAT_SEPARATOR = "\x1e"
        _neo.get_output_format.cache_clear()
        _SEP = _formats.FORMAT_SEPARATOR

        # ------------------------------------------------------------------
        # Patch 2: Lenient parse_output for value count mismatches.
        # ------------------------------------------------------------------
        _original_parse_output = _neo.parse_output

        def _lenient_parse_output(output, *args, **kwargs):
            if output.endswith(_SEP):
                output = output[: -len(_SEP)]
            try:
                return _original_parse_output(output, *args, **kwargs)
            except ValueError:
                formats, _ = _neo.get_output_format()
                values = output.split(_SEP)
                n = len(formats)
                if len(values) < n:
                    values.extend([""] * (n - len(values)))
                return dict(zip(formats, values[:n]))

        _neo.parse_output = _lenient_parse_output
        for mod in (_server, _session, _window, _pane):
            if hasattr(mod, "parse_output"):
                setattr(mod, "parse_output", _lenient_parse_output)

        # ------------------------------------------------------------------
        # Patch 3: Fix fetch_objs — split concatenated flags and translate
        # $N session IDs. Delegates to tmux_cmd directly but mirrors the
        # original fetch_objs logic for forward-compatibility.
        # ------------------------------------------------------------------

        def _patched_fetch_objs(server, list_cmd, list_extra_args=None):
            _fields, format_string = _neo.get_output_format()

            cmd_args = []
            if server.socket_name:
                cmd_args.extend(["-L", server.socket_name])
            if server.socket_path:
                cmd_args.extend(["-S", str(server.socket_path)])

            tmux_cmds = [*cmd_args, list_cmd]

            if list_extra_args:
                extra = list(list_extra_args)
                # Translate $N session IDs → session names
                for i, arg in enumerate(extra):
                    if (
                        arg == "-t"
                        and i + 1 < len(extra)
                        and str(extra[i + 1]).startswith("$")
                    ):
                        sid = str(extra[i + 1])
                        for s in server.sessions:
                            if s.id == sid:
                                extra[i + 1] = s.name
                                break
                # Split any concatenated flags (e.g., "-tvalue" → "-t", "value")
                split_extra = []
                for a in extra:
                    s = str(a)
                    if len(s) > 2 and s[:2] in ("-t", "-s", "-F") and s[2:]:
                        split_extra.extend([s[:2], s[2:]])
                    else:
                        split_extra.append(a)
                tmux_cmds.extend(split_extra)

            # Pass -F and format string as separate args
            tmux_cmds.extend(["-F", format_string])

            proc = _tmux_cmd(*tmux_cmds, tmux_bin=server.tmux_bin)
            if proc.stderr:
                raise _exc.LibTmuxException(proc.stderr)
            return [_lenient_parse_output(line) for line in proc.stdout]

        _neo.fetch_objs = _patched_fetch_objs
        for mod in (_server, _session, _window, _pane):
            if hasattr(mod, "fetch_objs"):
                setattr(mod, "fetch_objs", _patched_fetch_objs)

        # ------------------------------------------------------------------
        # Patch 4: Server.new_session — create via subprocess with properly
        # spaced flags, then fetch the Session object via list-sessions.
        # ------------------------------------------------------------------

        def _patched_new_session(self, *args, **kwargs):
            session_name = kwargs.get("session_name")
            if session_name is None and args:
                session_name = args[0]

            tmux_bin = self.tmux_bin or shutil.which("tmux") or "tmux"
            cmd = [tmux_bin, "new-session", "-d"]
            if session_name:
                cmd.extend(["-s", session_name])
            if kwargs.get("window_name"):
                cmd.extend(["-n", kwargs["window_name"]])
            if kwargs.get("x") is not None:
                cmd.extend(["-x", str(kwargs["x"])])
            if kwargs.get("y") is not None:
                cmd.extend(["-y", str(kwargs["y"])])
            if kwargs.get("start_directory"):
                cmd.extend(["-c", str(kwargs["start_directory"])])

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"tmux new-session failed: {result.stderr.strip()}"
                )

            if session_name:
                for s in self.sessions:
                    if s.name == session_name:
                        return s

            raise RuntimeError(
                f"Session '{session_name}' created but not found in list"
            )

        _server.Server.new_session = _patched_new_session

    except ImportError:
        pass
