"""FNV-304: `create_note` con contenido devuelve la nota tal como queda, no como nació.

Se creaba la nota, se le escribía el markdown y se devolvía la ficha de ANTES de escribir:
`seed_pending: false` y procedencia humana. El agente leía que no quedaba nada por sembrar justo
cuando acababa de dejarlo pendiente.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from foundry_mcp import server
from foundry_mcp.foundry import FoundryClient


class CreateNoteTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.calls: list[str] = []
        self.written = False

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(f"{request.method} {request.url.path}")
            if request.method == "POST" and request.url.path.endswith("/documents"):
                return httpx.Response(201, json=self._doc())
            if request.method == "PUT":
                self.written = True
                return httpx.Response(200, json={"blocks_written": 2, "seed_pending": True})
            return httpx.Response(200, json={"document": self._doc(), "blocks": []})

        self.client = FoundryClient("https://foundry.example/api/v1")
        await self.client._c.aclose()
        self.client._c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.client.token = "test-token"
        self._patch = patch("foundry_mcp.server._prepare", return_value=self.client)
        self._patch.start()

    async def asyncTearDown(self) -> None:
        self._patch.stop()
        await self.client._c.aclose()

    def _doc(self) -> dict[str, Any]:
        """La ficha que da el servidor: cambia en cuanto se escribe el contenido."""
        return {
            "id": "d1",
            "title": "Plan",
            "seed_pending": self.written,
            "content_provenance": "ia" if self.written else "humano",
        }

    async def test_with_markdown_it_returns_the_note_after_writing(self) -> None:
        result = await server.create_note(SimpleNamespace(), "s1", "Plan", markdown="## Plan\n\nAlgo.")
        self.assertEqual(result["blocks_written"], 2)
        self.assertTrue(result["document"]["seed_pending"])
        self.assertEqual(result["document"]["content_provenance"], "ia")

    async def test_without_markdown_it_does_not_ask_again(self) -> None:
        result = await server.create_note(SimpleNamespace(), "s1", "Plan")
        self.assertEqual(result["blocks_written"], 0)
        self.assertFalse(result["document"]["seed_pending"])
        self.assertEqual(self.calls, ["POST /api/v1/spaces/s1/documents"])
