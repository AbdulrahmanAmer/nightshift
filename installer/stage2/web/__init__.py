"""The setup window's server (P1, component C1; docs/design/installer-ui.md).

Standard library only: it runs on the bare uv-managed Python before any venv exists. Start it with
``python -m installer.stage2.web``. Keep this file free of imports so the wizard (P2) can use the helpers in
``samples`` without pulling in the server.
"""
