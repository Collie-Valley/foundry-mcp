"""Un commit citado dos veces en la misma tarjeta no puede tumbar el aporte que lo cita.

Salió cerrando FNV-317: una decisión enlazó el commit y el avance que la contaba, con la misma URL,
falló entero con «400: Esa URL ya está vinculada a la tarjeta». El avance era lo que importaba.
"""

from __future__ import annotations

from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from foundry_mcp import server
from foundry_mcp.foundry import FoundryClient

URL = "https://github.com/acme/repo/commit/5cb22aa"


def _cliente(ya_enlazado: bool) -> AsyncMock:
    client = AsyncMock(spec=FoundryClient)
    existentes = [{"id": "rl1", "url": URL, "kind": "commit"}] if ya_enlazado else []
    client.list_repo_links.return_value = existentes
    client.link_repo.return_value = [{"id": "rl1", "url": URL, "kind": "commit"}]
    client.add_task_comment.return_value = {"id": "c1"}
    client.add_task_decision.return_value = {"id": "d1"}
    return client


class RepeatedRepoLinkTests(IsolatedAsyncioTestCase):
    async def _progreso(self, client: AsyncMock) -> Any:
        with (
            patch("foundry_mcp.server._prepare", return_value=client),
            patch("foundry_mcp.server._agent_source", return_value="codex"),
        ):
            return await server.report_progress(
                object(),  # type: ignore[arg-type]
                "ini",
                "t1",
                "avance",
                repo_url=URL,
            )

    async def test_progress_with_an_already_linked_commit_is_still_recorded(self) -> None:
        client = _cliente(ya_enlazado=True)
        self.assertEqual(await self._progreso(client), {"id": "c1"})
        client.link_repo.assert_not_awaited()  # no se repite el enlace que daría 400
        meta = client.add_task_comment.await_args.kwargs["meta"]
        self.assertEqual(meta["repo_url"], URL)  # la entrada sigue enseñando su commit

    async def test_a_new_commit_is_linked_once(self) -> None:
        client = _cliente(ya_enlazado=False)
        await self._progreso(client)
        client.link_repo.assert_awaited_once_with("ini", "t1", URL, "commit", "")

    async def test_a_decision_reuses_the_existing_link(self) -> None:
        client = _cliente(ya_enlazado=True)
        with patch("foundry_mcp.server._prepare", return_value=client):
            await server.record_decision(
                object(),  # type: ignore[arg-type]
                "ini",
                "t1",
                "Elegí A",
                repo_url=URL,
            )
        client.link_repo.assert_not_awaited()
        self.assertEqual(client.add_task_decision.await_args.kwargs["repo_link_id"], "rl1")

    async def test_linking_twice_with_the_tool_is_harmless(self) -> None:
        client = _cliente(ya_enlazado=True)
        with patch("foundry_mcp.server._prepare", return_value=client):
            links = await server.link_repo(object(), "ini", "t1", URL, "commit")  # type: ignore[arg-type]
        client.link_repo.assert_not_awaited()
        self.assertEqual([lk["url"] for lk in links], [URL])
