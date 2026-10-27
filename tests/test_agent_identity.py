"""FNV-167: identidad declarada del agente en los aportes del MCP."""

from __future__ import annotations

import inspect
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx

from foundry_mcp import server
from foundry_mcp.config import load_config
from foundry_mcp.foundry import FoundryClient, set_agent_identity
from foundry_mcp.server import _resolve_agent_model, _resolve_agent_source


class ResolveAgentSourceTests(TestCase):
    def test_known_clients_are_normalized(self) -> None:
        cases = {
            "Claude Code": "claude_code",
            "OpenAI Codex": "codex",
            "opencode": "opencode",
            "Gemini CLI": "gemini",
        }
        for client_name, expected in cases.items():
            with self.subTest(client_name=client_name):
                self.assertEqual(_resolve_agent_source("auto", client_name), expected)

    def test_configured_source_wins_over_session(self) -> None:
        self.assertEqual(_resolve_agent_source("codex", "Claude Code"), "codex")

    def test_unknown_and_other_are_distinct(self) -> None:
        self.assertEqual(_resolve_agent_source("auto"), "unknown")
        self.assertEqual(_resolve_agent_source("auto", "Cursor"), "other")

    def setUp(self) -> None:
        server._declared_model = ""  # cada prueba empieza con una sesión sin modelo declarado

    def tearDown(self) -> None:
        server._declared_model = ""

    def test_without_declaration_config_or_unknown(self) -> None:
        with patch("foundry_mcp.server.load_config") as config:
            config.return_value.agent_model = "configured-model"
            self.assertEqual(_resolve_agent_model(), "configured-model")
            config.return_value.agent_model = ""
            self.assertIsNone(_resolve_agent_model())

    def test_declared_model_stays_for_the_rest_of_the_session(self) -> None:
        """FNV-311: lo declarado una vez vale después, aunque no se repita y haya configuración."""
        with patch("foundry_mcp.server.load_config") as config:
            config.return_value.agent_model = "configured-model"
            self.assertEqual(_resolve_agent_model("per-call-model"), "per-call-model")
            self.assertEqual(_resolve_agent_model(), "per-call-model")
            self.assertEqual(_resolve_agent_model("  "), "per-call-model")  # vacío no borra

    def test_a_new_declaration_replaces_the_previous_one(self) -> None:
        """Se puede cambiar de modelo a mitad de sesión (`/model`): manda el último declarado."""
        _resolve_agent_model("gpt-6-luna")
        self.assertEqual(_resolve_agent_model("gpt-5.6-terra"), "gpt-5.6-terra")
        self.assertEqual(_resolve_agent_model(), "gpt-5.6-terra")

    def test_declared_model_is_capped(self) -> None:
        self.assertEqual(len(_resolve_agent_model("m" * 500) or ""), 160)

    def test_config_detects_client_model_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {"FOUNDRY_AGENT_MODEL": "", "CODEX_MODEL": "gpt-codex-test"},
            clear=False,
        ):
            self.assertEqual(load_config().agent_model, "gpt-codex-test")

    def test_history_mutations_expose_per_call_model_override(self) -> None:
        mutation_names = (
            "move_task",
            "create_tasks_from_seeds",
            "report_progress",
            "flag_blocker",
            "propose_criterion",
            "record_decision",
            "add_test_case",
            "mark_test_case",
            "transition_task",
            "set_task_hierarchy",
            "set_task_details",
            "set_task_labels",
            "create_epic",
            "link_tasks",
            "unlink_tasks",
            "link_repo",
            "unlink_repo",
            "create_sprint",
            "start_sprint",
            "complete_sprint",
            "assign_task_to_sprint",
        )
        for name in mutation_names:
            with self.subTest(tool=name):
                self.assertIn("agent_model", inspect.signature(getattr(server, name)).parameters)


class FoundryClientAgentIdentityTests(IsolatedAsyncioTestCase):
    async def test_all_requests_include_context_local_agent_headers(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"id": "task-1"})

        client = FoundryClient("https://foundry.example/api/v1")
        await client._c.aclose()
        client._c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client.token = "test-token"
        set_agent_identity("opencode", "qwen3-coder")
        try:
            await client.move_task("initiative-1", "task-1", "column-2", 0)
            self.assertEqual(seen["x-foundry-agent-source"], "opencode")
            self.assertEqual(seen["x-foundry-agent-model"], "qwen3-coder")
        finally:
            set_agent_identity(None, None)
            await client._c.aclose()

    async def test_comment_payload_includes_agent_source_and_model(self) -> None:
        client = FoundryClient("https://foundry.example/api/v1")
        client._json = AsyncMock(return_value={"id": "c1"})  # type: ignore[method-assign]
        try:
            result = await client.add_task_comment(
                "initiative-1",
                "task-1",
                "avance",
                entry_kind="avance",
                agent_source="gemini",
                agent_model="gemini-2.5-pro",
            )
            self.assertEqual(result, {"id": "c1"})
            client._json.assert_awaited_once_with(
                "POST",
                "/initiatives/initiative-1/tasks/task-1/comments",
                json={
                    "content": "avance",
                    "as_agent": True,
                    "entry_kind": "avance",
                    "agent_source": "gemini",
                    "agent_model": "gemini-2.5-pro",
                },
            )
        finally:
            await client._c.aclose()

    async def test_compound_progress_uses_override_for_link_and_comment(self) -> None:
        client = AsyncMock(spec=FoundryClient)
        client.add_task_comment.return_value = {"id": "comment-1"}
        ctx = object()
        with (
            patch("foundry_mcp.server._prepare", return_value=client) as prepare,
            patch("foundry_mcp.server._agent_source", return_value="opencode"),
        ):
            result = await server.report_progress(
                ctx,  # type: ignore[arg-type]
                "initiative-1",
                "task-1",
                "avance",
                repo_url="https://example.test/commit/1",
                agent_model="per-call-model",
            )

        self.assertEqual(result, {"id": "comment-1"})
        server._declared_model = ""
        prepare.assert_called_once_with(ctx, "per-call-model")
        client.link_repo.assert_awaited_once()
        client.add_task_comment.assert_awaited_once_with(
            "initiative-1",
            "task-1",
            "avance",
            entry_kind="avance",
            meta={"repo_url": "https://example.test/commit/1"},
            agent_source="opencode",
            agent_model="per-call-model",
        )
