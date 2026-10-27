"""FNV-278: con qué backend habla el MCP recién instalado, antes de que nadie lo configure."""

from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from foundry_mcp.config import DEFAULT_API_BASE, load_config


class DefaultApiBaseTests(TestCase):
    def test_without_configuration_it_talks_to_the_public_foundry(self) -> None:
        """Quien instala el paquete no tiene un backend en su máquina esperando conexiones."""
        with patch.dict(os.environ, {}, clear=True):
            api_base = load_config().api_base
        self.assertEqual(api_base, DEFAULT_API_BASE)
        self.assertNotIn("localhost", api_base)  # el default de antes: un fallo sin explicación
        self.assertTrue(api_base.startswith("https://"))
        self.assertTrue(api_base.endswith("/api/v1"))  # el prefijo va incluido, no se añade solo

    def test_pointing_somewhere_else_is_said_on_purpose(self) -> None:
        """Un backend local sigue estando a una variable de distancia; la barra final sobra."""
        local = "http://localhost:8000/api/v1"
        with patch.dict(os.environ, {"FOUNDRY_API_BASE": f"{local}/"}, clear=True):
            self.assertEqual(load_config().api_base, local)
