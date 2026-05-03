# psmux v3.3.4+ provides native libtmux compatibility, so the compatibility
# shim in _psmux_compat is no longer wired up at import time. The module is
# kept in-tree for reference / regression fallback only.
#
# To re-enable for older psmux on Windows:
#     from cli_agent_orchestrator._psmux_compat import apply
#     apply()  # before any libtmux import
