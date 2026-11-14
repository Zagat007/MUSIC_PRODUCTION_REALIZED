"""Suno Release Master - local, offline-first audio QA, mastering and release packaging.

The DSP engine (srm.ingest / srm.analysis / srm.dsp / srm.release) is independent of
the GUI (srm.gui), the CLI (srm.cli) and the MCP bridge (srm.mcp_server). All front-ends
call the same facade in srm.service.engine.
"""

__version__ = "0.1.0"
APP_NAME = "Suno Release Master"
