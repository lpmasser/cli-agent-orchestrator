"""Compatibility shim for running CAO with psmux on Windows.

psmux is a Windows-native tmux replacement with these known differences:

1. Only handles U+001E as separator (not U+241E used by libtmux default).
2. Does not support ``-F<value>`` (no space) — requires ``-F <value>``.
3. ``new-session -PF`` may return fewer format values than expected.

We patch libtmux at import time to work around all three.
"""

import os
import sys


def apply():
    """Apply psmux compatibility patches."""
    if sys.platform != "win32":
        return  # Only needed on Windows with psmux

    # Patch 1: Use control character as format separator
    if "LIBTMUX_TMUX_FORMAT_SEPARATOR" not in os.environ:
        os.environ["LIBTMUX_TMUX_FORMAT_SEPARATOR"] = "\x1e"

    try:
        from libtmux import neo as _neo
        from libtmux import pane as _pane
        from libtmux import server as _server
        from libtmux import session as _session
        from libtmux import window as _window
        from libtmux.formats import FORMAT_SEPARATOR

        # Patch 2: Make parse_output lenient for value count mismatches
        _original_parse_output = _neo.parse_output

        def _lenient_parse_output(output: str, *args, **kwargs):
            if output.endswith(FORMAT_SEPARATOR):
                output = output[: -len(FORMAT_SEPARATOR)]
            try:
                return _original_parse_output(output, *args, **kwargs)
            except ValueError:
                formats, _ = _neo.get_output_format()
                values = output.split(FORMAT_SEPARATOR)
                n = len(formats)
                if len(values) < n:
                    values.extend([""] * (n - len(values)))
                return dict(zip(formats, values[:n]))

        _neo.parse_output = _lenient_parse_output
        for mod in (_server, _session, _window, _pane):
            if hasattr(mod, "parse_output"):
                setattr(mod, "parse_output", _lenient_parse_output)

        # Patch 3: Fix fetch_objs to use "-F" "<value>" (with space)
        # instead of "-F<value>" (without space) which psmux doesn't support.
        _original_fetch_objs = _neo.fetch_objs

        def _patched_fetch_objs(server, list_cmd, list_extra_args=None):
            from libtmux import exc
            from libtmux.common import tmux_cmd

            _fields, format_string = _neo.get_output_format()

            cmd_args: list = []
            if server.socket_name:
                cmd_args.extend(["-L", server.socket_name])
            if server.socket_path:
                cmd_args.extend(["-S", str(server.socket_path)])

            tmux_cmds = [*cmd_args, list_cmd]
            if list_extra_args:
                # psmux doesn't support $N session IDs in -t.
                # Replace -t $N with -t <session_name> by looking it up.
                extra = list(list_extra_args)
                for i, arg in enumerate(extra):
                    if (
                        arg == "-t"
                        and i + 1 < len(extra)
                        and str(extra[i + 1]).startswith("$")
                    ):
                        # Look up session name from id
                        sid = str(extra[i + 1])
                        try:
                            import subprocess as _sp

                            r = _sp.run(
                                ["tmux", "list-sessions", "-F",
                                 "#{session_id} #{session_name}"],
                                capture_output=True, text=True,
                            )
                            for line in r.stdout.strip().split("\n"):
                                parts = line.split(" ", 1)
                                if len(parts) == 2 and parts[0] == sid:
                                    extra[i + 1] = parts[1]
                                    break
                        except Exception:
                            pass
                tmux_cmds.extend(extra)
            # KEY FIX: use "-F" and format_string as separate args
            tmux_cmds.extend(["-F", format_string])

            proc = tmux_cmd(*tmux_cmds, tmux_bin=server.tmux_bin)

            if proc.stderr:
                raise exc.LibTmuxException(proc.stderr)

            return [_lenient_parse_output(line) for line in proc.stdout]

        _neo.fetch_objs = _patched_fetch_objs
        # Replace in all modules that import fetch_objs
        for mod in (_server, _session, _window, _pane):
            if hasattr(mod, "fetch_objs"):
                setattr(mod, "fetch_objs", _patched_fetch_objs)

        # Patch 4: Fix Server.new_session — fallback to list-sessions
        # when -PF output parsing fails (psmux returns fewer values).
        _original_new_session = _server.Server.new_session

        def _patched_new_session(self, *args, **kwargs):
            """psmux-compatible new_session: create via subprocess, fetch via list."""
            import subprocess as _sp

            session_name = kwargs.get("session_name")
            if session_name is None and args:
                session_name = args[0]
            window_name = kwargs.get("window_name")
            start_directory = kwargs.get("start_directory")
            x = kwargs.get("x")
            y = kwargs.get("y")

            # Build command with proper spacing (psmux needs -s <name>, not -s<name>)
            cmd = ["tmux", "new-session", "-d"]
            if session_name:
                cmd.extend(["-s", session_name])
            if window_name:
                cmd.extend(["-n", window_name])
            if x is not None:
                cmd.extend(["-x", str(x)])
            if y is not None:
                cmd.extend(["-y", str(y)])
            if start_directory:
                cmd.extend(["-c", str(start_directory)])

            result = _sp.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"tmux new-session failed: {result.stderr.strip()}"
                )

            # Fetch the created session via list-sessions
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
