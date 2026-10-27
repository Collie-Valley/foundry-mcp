"""Cliente REST de la API de Foundry para el MCP (login JWT + endpoints de tableros).

Mismo contrato público que usa el frontend. **La obtención del token es externa**: el servidor
inyecta un callback `reauth` (que la primera vez suele pedir credenciales a Claude Code por
elicitation). El cliente solo mantiene el token en memoria y reautentica una vez ante un 401.
La cuenta usada debe ser miembro (colaborador/dueño) de los espacios que quiera **mutar**.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

import httpx


class FoundryError(RuntimeError):
    """Error legible de la API (status + detalle). Se devuelve como error de la herramienta MCP."""


# Callback que devuelve un token FRESCO (interactivo o desde overrides de entorno).
Reauth = Callable[[], Awaitable[str]]

_agent_source: ContextVar[str | None] = ContextVar("foundry_agent_source", default=None)
_agent_model: ContextVar[str | None] = ContextVar("foundry_agent_model", default=None)


def set_agent_identity(source: str | None, model: str | None) -> None:
    """Fija la identidad declarada para la tarea async MCP actual.

    ContextVar evita mezclar agentes/modelos si el servidor atiende herramientas concurrentes.
    """

    _agent_source.set(source)
    _agent_model.set(model)


class FoundryClient:
    """Envuelve la API v1 de Foundry. El token lo provee `reauth` (inyectado por el servidor)."""

    def __init__(self, base: str, *, timeout: float = 30.0) -> None:
        self._base = base.rstrip("/")
        self._c = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
        self.token: str | None = None  # sesión en memoria (o sembrada del keyring por el servidor)
        self.reauth: Reauth | None = None  # lo fija el servidor en cada llamada (captura el ctx)

    # ── autenticación ────────────────────────────────────────────────────────
    async def obtain_token(self, email: str, password: str) -> str:
        """POST /auth/login → access_token. No fija estado (lo usa el flujo de reauth del servidor)."""
        r = await self._c.post(
            f"{self._base}/auth/login", json={"email": email, "password": password}
        )
        if r.status_code == 401:
            raise FoundryError("Login rechazado (401): email o contraseña incorrectos.")
        if r.status_code >= 400:
            raise FoundryError(_error_message(r))
        return r.json()["access_token"]

    async def _ensure_token(self) -> None:
        if self.token is None:
            if self.reauth is None:
                raise FoundryError("No hay sesión. Usa la herramienta `login` para iniciar sesión.")
            self.token = await self.reauth()

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self.token}"}
        source = _agent_source.get()
        model = _agent_model.get()
        if source:
            headers["X-Foundry-Agent-Source"] = source
        if model:
            headers["X-Foundry-Agent-Model"] = model
        r = await self._c.request(method, f"{self._base}{path}", headers=headers, **kw)
        if r.status_code == 401 and self.reauth is not None:  # token expirado/ inválido
            self.token = await self.reauth()  # reautentica (interactivo si hace falta) y reintenta
            headers["Authorization"] = f"Bearer {self.token}"
            r = await self._c.request(method, f"{self._base}{path}", headers=headers, **kw)
        if r.status_code >= 400:
            raise FoundryError(_error_message(r))
        return r

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._req(method, path, **kw)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    # ── cuenta / espacios / miembros ─────────────────────────────────────────
    async def me(self) -> dict[str, Any]:
        return await self._json("GET", "/me")

    async def list_spaces(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/spaces")

    async def list_members(self, space_id: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/spaces/{space_id}/members")

    # ── tableros / iniciativas ───────────────────────────────────────────────
    async def list_personal_boards(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/me/boards")

    async def list_space_initiatives(self, space_id: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/spaces/{space_id}/initiatives")

    async def get_initiative(self, initiative_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/initiatives/{initiative_id}")

    async def list_board_presets(self) -> list[dict[str, Any]]:
        """Catálogo de plantillas de columnas para el alta (FNV-257): key, label, template,
        is_default y las columnas de cada una con su categoría."""
        return await self._json("GET", "/board-presets")

    async def create_personal_board(
        self,
        title: str,
        preset: str | None = None,
        columns: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await self._json("POST", "/me/boards", json=_board_body(title, preset, columns))

    async def create_space_board(
        self,
        space_id: str,
        title: str,
        preset: str | None = None,
        columns: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await self._json(
            "POST", f"/spaces/{space_id}/boards", json=_board_body(title, preset, columns)
        )

    async def move_board(self, initiative_id: str, space_id: str) -> dict[str, Any]:
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/move", json={"space_id": space_id}
        )

    # ── columnas del tablero (ADR-0014 E2·CRUD; en el MCP desde FNV-267) ──────
    # Las reglas (tope de 10, nombres sin repetir, ≥1 de entrada y ≥1 de cierre, y a dónde van
    # las tarjetas al borrar) las hace cumplir el servidor: aquí se llama y se deja hablar al error.
    async def add_column(
        self, initiative_id: str, name: str, category: str, color: str | None = None
    ) -> None:
        body: dict[str, Any] = {"name": name, "category": category}
        if color:
            body["color"] = color
        await self._json("POST", f"/initiatives/{initiative_id}/columns", json=body)

    async def rename_column(self, initiative_id: str, column_id: str, name: str) -> None:
        await self._json(
            "POST", f"/initiatives/{initiative_id}/columns/{column_id}/rename", json={"name": name}
        )

    async def set_column_category(
        self, initiative_id: str, column_id: str, category: str
    ) -> None:
        await self._json(
            "POST",
            f"/initiatives/{initiative_id}/columns/{column_id}/category",
            json={"category": category},
        )

    async def set_column_color(
        self, initiative_id: str, column_id: str, color: str | None
    ) -> None:
        await self._json(
            "POST",
            f"/initiatives/{initiative_id}/columns/{column_id}/color",
            json={"color": color},
        )

    async def reorder_columns(self, initiative_id: str, column_ids: list[str]) -> None:
        await self._json(
            "POST",
            f"/initiatives/{initiative_id}/columns/reorder",
            json={"column_ids": column_ids},
        )

    async def delete_column(
        self, initiative_id: str, column_id: str, reassign_to: str | None = None
    ) -> None:
        params = {"reassign_to": reassign_to} if reassign_to else None
        await self._json(
            "DELETE", f"/initiatives/{initiative_id}/columns/{column_id}", params=params
        )

    # ── tarjetas (Kanban) ────────────────────────────────────────────────────
    async def create_task(self, initiative_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", f"/initiatives/{initiative_id}/tasks", json=body)

    async def move_task(
        self, initiative_id: str, task_id: str, column_id: str, order: int
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/move",
            json={"column_id": column_id, "order": order},
        )

    async def edit_task(
        self, initiative_id: str, task_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/tasks/{task_id}/edit", json=body
        )

    async def delete_task(self, initiative_id: str, task_id: str) -> None:
        await self._json("DELETE", f"/initiatives/{initiative_id}/tasks/{task_id}")

    # ── jerarquía / campos ricos (ADR-0014, E3/E6) ───────────────────────────
    async def set_task_hierarchy(
        self, initiative_id: str, task_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/tasks/{task_id}/hierarchy", json=body
        )

    async def set_task_details(
        self, initiative_id: str, task_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/tasks/{task_id}/details", json=body
        )

    # ── etiquetas (ADR-0014, E3·D/E6) ────────────────────────────────────────
    async def list_labels(self, initiative_id: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/initiatives/{initiative_id}/labels")

    async def create_label(
        self, initiative_id: str, name: str, color: str | None = None
    ) -> dict[str, Any]:
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/labels", json={"name": name, "color": color}
        )

    async def set_task_labels(
        self, initiative_id: str, task_id: str, label_ids: list[str]
    ) -> list[dict[str, Any]]:
        return await self._json(
            "PUT",
            f"/initiatives/{initiative_id}/tasks/{task_id}/labels",
            json={"label_ids": label_ids},
        )

    # ── vínculos (ADR-0014, E3·E/E6) ─────────────────────────────────────────
    async def link_tasks(
        self, initiative_id: str, task_id: str, target_id: str, link_type: str
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/links",
            json={"target_id": target_id, "link_type": link_type},
        )

    async def list_task_links(self, initiative_id: str, task_id: str) -> list[dict[str, Any]]:
        return await self._json(
            "GET", f"/initiatives/{initiative_id}/tasks/{task_id}/links"
        )

    async def unlink_tasks(
        self, initiative_id: str, task_id: str, link_id: str
    ) -> list[dict[str, Any]]:
        """DELETE de un vínculo por su id; devuelve los vínculos restantes de la tarjeta."""
        return await self._json(
            "DELETE", f"/initiatives/{initiative_id}/tasks/{task_id}/links/{link_id}"
        )

    # ── vínculos de repositorio (ADR-0016, Opción A) ─────────────────────────
    async def link_repo(
        self, initiative_id: str, task_id: str, url: str, kind: str, title: str
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/repo-links",
            json={"url": url, "kind": kind, "title": title},
        )

    async def list_repo_links(self, initiative_id: str, task_id: str) -> list[dict[str, Any]]:
        return await self._json(
            "GET", f"/initiatives/{initiative_id}/tasks/{task_id}/repo-links"
        )

    async def unlink_repo(
        self, initiative_id: str, task_id: str, link_id: str
    ) -> list[dict[str, Any]]:
        """DELETE de un vínculo de repo por su id; devuelve los restantes de la tarjeta."""
        return await self._json(
            "DELETE", f"/initiatives/{initiative_id}/tasks/{task_id}/repo-links/{link_id}"
        )

    # ── historial estructurado (ADR-0014, E4/E6) ─────────────────────────────
    async def get_task_history(self, initiative_id: str, task_id: str) -> list[dict[str, Any]]:
        return await self._json(
            "GET", f"/initiatives/{initiative_id}/tasks/{task_id}/history"
        )

    # ── sprints (ADR-0014, E5/E6) ────────────────────────────────────────────
    async def list_sprints(self, initiative_id: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/initiatives/{initiative_id}/sprints")

    async def create_sprint(self, initiative_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", f"/initiatives/{initiative_id}/sprints", json=body)

    async def start_sprint(self, initiative_id: str, sprint_id: str) -> dict[str, Any]:
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/sprints/{sprint_id}/start"
        )

    async def complete_sprint(self, initiative_id: str, sprint_id: str) -> dict[str, Any]:
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/sprints/{sprint_id}/complete", json={}
        )

    async def assign_task_sprint(
        self, initiative_id: str, task_id: str, sprint_id: str | None
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/sprint",
            json={"sprint_id": sprint_id},
        )

    async def create_tasks_from_seeds(
        self, initiative_id: str, seed_ids: list[str]
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/tasks/from-seeds", json={"seed_ids": seed_ids}
        )

    # ── comentarios / historial de la tarjeta ────────────────────────────────
    async def list_task_comments(
        self, initiative_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        return await self._json(
            "GET", f"/initiatives/{initiative_id}/tasks/{task_id}/comments"
        )

    async def add_task_comment(
        self,
        initiative_id: str,
        task_id: str,
        content: str,
        *,
        as_agent: bool = True,
        entry_kind: str = "nota",
        meta: dict[str, Any] | None = None,
        agent_source: str | None = None,
        agent_model: str | None = None,
    ) -> dict[str, Any]:
        """Agrega un aporte al historial de la tarjeta. `as_agent=True`: lo marca como del
        agente (default para el MCP; procedencia `ia`); la API igual guarda quién actúa como
        `author_id`. `entry_kind` tipa el aporte (ADR-0017): avance/criterio/decision/bloqueo/
        verificacion/pregunta/nota. `meta` es JSON libre (p. ej. refs de commit).
        `agent_source` identifica el cliente MCP que originó el aporte y `agent_model` el modelo
        concreto declarado para esa llamada."""
        payload: dict[str, Any] = {
            "content": content,
            "as_agent": as_agent,
            "entry_kind": entry_kind,
        }
        if meta is not None:
            payload["meta"] = meta
        if as_agent and agent_source is not None:
            payload["agent_source"] = agent_source
        if as_agent and agent_model is not None:
            payload["agent_model"] = agent_model
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/comments",
            json=payload,
        )

    # ── datos promovidos con gate: criterios + decisiones (ADR-0017 F2) ──────
    async def add_task_criterion(
        self, initiative_id: str, task_id: str, text: str, *, as_agent: bool = True
    ) -> dict[str, Any]:
        """Añade un criterio de aceptación. `as_agent=True` (default del MCP) ⇒ nace `proposed`
        (procedencia `ia`); una persona lo acepta/descarta en el tablero."""
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/criteria",
            json={"text": text, "as_agent": as_agent},
        )

    async def add_task_decision(
        self,
        initiative_id: str,
        task_id: str,
        title: str,
        rationale: str = "",
        *,
        as_agent: bool = True,
        repo_link_id: str | None = None,
    ) -> dict[str, Any]:
        """Registra una decisión (ADR-lite). `as_agent=True` ⇒ nace `proposed`. `repo_link_id` la
        ata a un vínculo de repo (ADR-0016) para la trazabilidad IA→código."""
        payload: dict[str, Any] = {
            "title": title,
            "rationale": rationale,
            "as_agent": as_agent,
        }
        if repo_link_id:
            payload["repo_link_id"] = repo_link_id
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/decisions",
            json=payload,
        )

    async def list_task_criteria(
        self, initiative_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        """Criterios de aceptación de una tarjeta con su estado del gate (proposed/accepted/
        rejected), procedencia y si ya se cumplen (`checked`)."""
        return await self._json(
            "GET", f"/initiatives/{initiative_id}/tasks/{task_id}/criteria"
        )

    async def review_task_criterion(
        self, initiative_id: str, task_id: str, criterion_id: str, *, accept: bool
    ) -> dict[str, Any]:
        """Gate humano de un criterio: `accept=True` lo vuelve oficial; `False` lo descarta."""
        verb = "accept" if accept else "reject"
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/criteria/{criterion_id}/{verb}",
        )

    async def list_task_decisions(
        self, initiative_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        """Decisiones de diseño de una tarjeta con su estado del gate (proposed/accepted/
        rejected), procedencia y el vínculo de repo al que atan (si lo hay)."""
        return await self._json(
            "GET", f"/initiatives/{initiative_id}/tasks/{task_id}/decisions"
        )

    async def review_task_decision(
        self, initiative_id: str, task_id: str, decision_id: str, *, accept: bool
    ) -> dict[str, Any]:
        """Gate humano de una decisión: `accept=True` la vuelve oficial; `False` la descarta."""
        verb = "accept" if accept else "reject"
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/decisions/{decision_id}/{verb}",
        )

    async def review_task_proposal(
        self, initiative_id: str, task_id: str, *, accept: bool
    ) -> dict[str, Any]:
        """Gate humano de una tarjeta propuesta por la IA (`status_agent="proposed"`):
        `accept=True` la vuelve oficial; `False` la marca `rejected` (el front la oculta)."""
        verb = "accept" if accept else "reject"
        return await self._json(
            "POST", f"/initiatives/{initiative_id}/tasks/{task_id}/{verb}"
        )

    # ── casos de prueba (FNV-127) ────────────────────────────────────────────
    async def add_test_case(
        self,
        initiative_id: str,
        task_id: str,
        title: str,
        steps: str = "",
        expected: str = "",
        *,
        as_agent: bool = True,
    ) -> dict[str, Any]:
        """Deja un caso de prueba en un entregable (historia, o tarea/bug/incidencia suelta; el
        backend rechaza épicas y subtareas). `as_agent=True` (default del MCP) ⇒ procedencia `ia`.
        Nace `pending`: no hay gate, se valida corriéndolo."""
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/test-cases",
            json={"title": title, "steps": steps, "expected": expected, "as_agent": as_agent},
        )

    async def list_test_cases(
        self, initiative_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        """Casos de prueba de una tarjeta con su estado (`pending`/`passed`/`failed`), pasos,
        resultado esperado y procedencia."""
        return await self._json(
            "GET", f"/initiatives/{initiative_id}/tasks/{task_id}/test-cases"
        )

    async def mark_test_case(
        self, initiative_id: str, task_id: str, test_case_id: str, status: str
    ) -> dict[str, Any]:
        """Registra el resultado de correr un caso: `status` = passed | failed | pending."""
        return await self._json(
            "PATCH",
            f"/initiatives/{initiative_id}/tasks/{task_id}/test-cases/{test_case_id}",
            json={"status": status},
        )

    async def list_test_executions(
        self, initiative_id: str, task_id: str, test_case_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Bitácora de corridas de los casos de prueba (FNV-159): todas las de la tarjeta o, con
        `test_case_id`, las de un caso. Cada corrida lleva resultado, autor, etapa y fecha."""
        params = {"test_case_id": test_case_id} if test_case_id else None
        return await self._json(
            "GET",
            f"/initiatives/{initiative_id}/tasks/{task_id}/test-executions",
            params=params,
        )

    # ── adjuntos (E-attachments) ─────────────────────────────────────────────
    async def list_task_attachments(
        self, initiative_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        return await self._json(
            "GET", f"/initiatives/{initiative_id}/tasks/{task_id}/attachments"
        )

    async def read_task_attachment(
        self, initiative_id: str, task_id: str, attachment_id: str
    ) -> tuple[bytes, str, str]:
        """Descarga los bytes de un adjunto. Devuelve (data, content_type, filename).

        Usa `_req` (no `_json`): el endpoint sirve bytes crudos con el `content_type` real en el
        header, no JSON. El backend valida que el adjunto pertenezca a la tarea (404 si no).
        """
        r = await self._req(
            "GET",
            f"/initiatives/{initiative_id}/tasks/{task_id}/attachments/{attachment_id}",
        )
        content_type = (
            r.headers.get("content-type") or "application/octet-stream"
        ).split(";")[0].strip()
        filename = _filename_from_disposition(r.headers.get("content-disposition")) or attachment_id
        return r.content, content_type, filename

    async def add_task_attachment(
        self,
        initiative_id: str,
        task_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        """Sube un adjunto a una tarjeta (multipart, como el formulario del navegador).

        No usa `json=`: el endpoint espera un `UploadFile`, así que va en `files=`. El backend
        rechaza lo vacío (400) y lo que pase de 10 MB (413).
        """
        return await self._json(
            "POST",
            f"/initiatives/{initiative_id}/tasks/{task_id}/attachments",
            files={"file": (filename, data, content_type)},
        )

    async def delete_task_attachment(
        self, initiative_id: str, task_id: str, attachment_id: str
    ) -> None:
        await self._json(
            "DELETE",
            f"/initiatives/{initiative_id}/tasks/{task_id}/attachments/{attachment_id}",
        )

    # ── notas (contexto docs; FNV-131/132) ───────────────────────────────────
    async def list_documents(self, space_id: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/spaces/{space_id}/documents")

    async def get_document(self, document_id: str) -> dict[str, Any]:
        """El documento + sus bloques materializados (la foto que también ve la búsqueda)."""
        return await self._json("GET", f"/documents/{document_id}")

    async def create_document(
        self, space_id: str, title: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"/spaces/{space_id}/documents",
            json={"title": title, "parent_id": parent_id},
        )

    async def update_document(self, document_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json("PATCH", f"/documents/{document_id}", json=body)

    async def delete_document(self, document_id: str) -> None:
        await self._json("DELETE", f"/documents/{document_id}")

    async def write_document(
        self, document_id: str, markdown: str, mode: str
    ) -> dict[str, Any]:
        """PUT /documents/{id}/content (FNV-130): el servidor traduce el markdown a bloques."""
        return await self._json(
            "PUT",
            f"/documents/{document_id}/content",
            json={"markdown": markdown, "mode": mode},
        )

    async def link_document_mention(
        self, document_id: str, target_id: str, lead: str = ""
    ) -> dict[str, Any]:
        """POST /documents/{id}/mentions (FNV-132): ata la nota a una tarjeta/semilla existente."""
        return await self._json(
            "POST",
            f"/documents/{document_id}/mentions",
            json={"target_id": target_id, "lead": lead},
        )

    async def search_documents(self, space_id: str, query: str) -> list[dict[str, Any]]:
        return await self._json(
            "GET", f"/spaces/{space_id}/documents/search", params={"q": query}
        )

    async def document_backlinks(self, document_id: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/documents/{document_id}/backlinks")

    async def extract_tasks_from_document(
        self, document_id: str, initiative_id: str
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"/documents/{document_id}/extract-tasks",
            json={"initiative_id": initiative_id},
        )

    async def task_origin(self, task_id: str) -> dict[str, Any] | None:
        return await self._json("GET", f"/tasks/{task_id}/origin")

    async def task_mentions(self, task_id: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/tasks/{task_id}/mentions")

    async def close(self) -> None:
        await self._c.aclose()


def _board_body(
    title: str, preset: str | None, columns: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Cuerpo del alta de un tablero. Cada parte solo viaja si se eligió: sin `preset` manda el que
    el backend tenga por omisión, y sin `columns` las de la plantilla. Con columnas a medida el
    preset sigue yendo, porque es quien decide la plantilla del tablero —y con ella los sprints."""
    body: dict[str, Any] = {"title": title}
    if preset:
        body["preset"] = preset
    if columns:
        body["columns"] = columns
    return body


def _error_message(r: httpx.Response) -> str:
    """Construye un mensaje de error accionable a partir de la respuesta (usa `detail` si viene)."""
    detail: Any = None
    try:
        body = r.json()
        if isinstance(body, dict):
            # FastAPI manda el motivo en `detail`; las reglas de negocio de Foundry viajan en
            # `error.message` (el handler de DomainError). Mirar solo `detail` dejaba un
            # «400: None» que no dice QUÉ regla se rompió — justo lo que hay que contar.
            detail = body.get("detail")
            if detail is None:
                error = body.get("error")
                detail = error.get("message") if isinstance(error, dict) else error
        else:
            detail = body
    except Exception:  # respuesta no-JSON
        detail = (r.text or "").strip()[:300]
    if detail is None or detail == "":
        detail = (r.text or "").strip()[:300] or "sin detalle"
    hint = {
        401: "no autenticado",
        403: "permiso denegado (¿rol observador, o la cuenta no es miembro del espacio?)",
        404: "no encontrado (¿id correcto? ¿la cuenta ve ese tablero/espacio?)",
        422: "cuerpo inválido",
    }.get(r.status_code, "")
    tail = f" — {hint}" if hint else ""
    return f"Foundry API {r.status_code}: {detail}{tail}"


def _filename_from_disposition(value: str | None) -> str | None:
    """Extrae el nombre de archivo de un header Content-Disposition (`… filename="foo.png"`)."""
    if not value:
        return None
    quoted = re.search(r'filename="([^"]+)"', value)  # forma que emite el backend
    if quoted:
        return quoted.group(1)
    bare = re.search(r"filename=([^;]+)", value)
    return bare.group(1).strip() if bare else None
