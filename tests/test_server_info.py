"""FNV-315: el servidor se anuncia con SU versión, no con la del SDK de MCP."""

from __future__ import annotations

import re
from importlib.metadata import version as installed_version
from pathlib import Path
from unittest import TestCase

from foundry_mcp import __version__, server

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version() -> str:
    # Con regex y no con tomllib: las pruebas corren también en Python 3.10, que no lo trae.
    match = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "pyproject.toml sin `version`"
    return match.group(1)


class ServerInfoTests(TestCase):
    def test_announced_version_is_the_package_one(self) -> None:
        """Lo que ve el cliente en `serverInfo.version` es lo que se publicó en PyPI."""
        options = server.mcp._mcp_server.create_initialization_options()
        self.assertEqual(options.server_name, "foundry-boards")
        self.assertEqual(options.server_version, _pyproject_version())

    def test_it_is_not_the_sdk_version(self) -> None:
        """El síntoma del bug: se anunciaba 1.30.0, que es la versión del paquete `mcp`."""
        options = server.mcp._mcp_server.create_initialization_options()
        self.assertNotEqual(options.server_version, installed_version("mcp"))

    def test_dunder_version_follows_pyproject(self) -> None:
        """`pyproject.toml` es la única fuente: `__version__` ya no se copia a mano."""
        self.assertEqual(__version__, _pyproject_version())
