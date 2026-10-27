"""Servidor MCP `foundry-boards`: administra los tableros Kanban de Foundry desde agentes de IA.

Expone por *stdio* herramientas para listar/crear tableros y gestionar sus tarjetas contra la
API REST de Foundry. Los permisos los valida el backend por rol: **dueño/colaborador** pueden
mutar; **observador** solo lee.

Autenticación **interactiva**: la primera vez que se necesita, el servidor le pide credenciales
al usuario a través del cliente MCP (MCP *elicitation*: un diálogo con email + contraseña, o un
token). No se guardan credenciales en `.env`; solo se cachea el **token** resultante en memoria y,
si el usuario lo acepta, en el **gestor de credenciales del sistema operativo** (nunca en archivos).
`FOUNDRY_TOKEN`/`FOUNDRY_EMAIL`/`FOUNDRY_PASSWORD` siguen disponibles como override para automatización.

Conveniencias para el agente:
- columnas por **nombre** ("En progreso") o por id;
- **responsable** por nombre/email de un miembro, o por id (en tableros de Área);
- `edit_task` es **parcial**: solo cambian los campos que pases.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Image, MCPServer, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from foundry_mcp import __version__
from foundry_mcp.config import load_config
from foundry_mcp.foundry import FoundryClient, FoundryError, set_agent_identity

# El contexto de cada llamada, con los parámetros que pide mypy estricto: un lifespan que no usamos
# y el tipo de petición, que depende del transporte. El SDK lo inyecta reconociendo esta anotación,
# así que cambiarla puede convertir `ctx` en un argumento de todas las herramientas: lo vigila
# tests/test_context_injection.py.
Ctx = Context[Any, Any]

# Prioridad de 5 niveles (ADR-0014, E3·B). Se aceptan también los 3 antiguos (baja/media/alta): el
# backend los normaliza a low/medium/high.
PRIORITIES = ("highest", "high", "medium", "low", "lowest", "baja", "media", "alta")
KINDS = ("tarea", "bug", "incidencia")
# Tipo de work item en la jerarquía ágil (ADR-0014). Epic es raíz; la subtarea exige padre.
ISSUE_TYPES = ("epic", "historia", "tarea", "bug", "incidencia", "subtarea")
# Vínculos entre tarjetas (ADR-0014, E3·E). `is_blocked_by` es el inverso de `blocks`.
LINK_TYPES = ("blocks", "is_blocked_by", "relates_to")
# Recursos de repositorio que una tarjeta puede referenciar (ADR-0016, Opción A).
REPO_KINDS = ("repository", "branch", "pull_request", "commit")
# Resultado de una verificación (ADR-0017): qué dio la prueba/evidencia del trabajo hecho.
VERIFICATION_STATUSES = ("ok", "fallo", "parcial")
# Tipo de una columna (ADR-0014). Es lo único estable de una columna y lo que decide el
# comportamiento: por dónde entra el trabajo, qué está en curso y qué lo da por cerrado.
COLUMN_CATEGORIES = ("todo", "in_progress", "done")

# ── Qué clase de operación es cada herramienta (FNV-268) ─────────────────────
# El cliente MCP usa estas etiquetas para decidir qué puede aprobar solo y dónde conviene pararse a
# preguntar. **Callar no es neutral**: lo no declarado se asume destructivo, así que un servidor sin
# etiquetas hace que leer un tablero pese lo mismo que borrar una tarjeta — y quien aprueba deja de
# mirar. Describen la operación; no protegen nada: los permisos siguen siendo del backend.
SOLO_LEE = ToolAnnotations(read_only_hint=True)
CREA = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
# Deja un valor concreto: repetirla con los mismos datos no cambia nada más (se puede reintentar).
ACTUALIZA = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
BORRA = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
# Las columnas NO son un juego fijo (FNV-256): cada tablero nace de una de las siete plantillas o
# de unas columnas montadas a mano, y además se cambian cuando se quiera. No se suponen —se leen del
# tablero con `get_board`— y, al crear sin `column`, la de entrada se deduce con
# `_default_column_id`, que mira la CATEGORÍA de cada columna y nunca su nombre.
COLUMN_SETS_HINT = (
    "siete plantillas —de «To Do · In Progress · In QA · Pending To Deploy · Done» a "
    "«Backlog · Por hacer · En progreso · En revisión · Hecho»— o las que haya montado el equipo"
)
# Tope por adjunto que impone el backend (10 MB). Se comprueba antes de subir para poder explicarlo
# en vez de comerse un 413 a medio camino.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
# MIME (no imagen) que el MCP puede devolver como texto legible (además de cualquier text/*).
_TEXTUAL_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/csv",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/javascript",
        "application/sql",
    }
)

# Servicio bajo el que se guarda el token en el gestor de credenciales del SO (si el usuario acepta).
_KEYRING_SERVICE = "foundry-boards-mcp"
_KEYRING_USER = "token"

mcp = MCPServer(
    "foundry-boards",
    # Sin ella, el servidor se anunciaba con la versión del SDK y los clientes que la registran no
    # distinguían una 0.1.0 de una 0.1.1 (FNV-315).
    version=__version__,
    website_url="https://foundry.collie.digital",
    instructions=(
        "Administra los tableros de Foundry (vivero colaborativo de ideas → iniciativas) con un "
        "modelo de gestión de trabajo ágil. Úsalo para ver/crear/reorganizar tableros y sus "
        "tarjetas (work items), asignar responsables, transicionar por estados, gestionar "
        "jerarquía (épica → historia/tarea/bug/incidencia → subtarea; una historia agrupa "
        "tareas y bugs), etiquetas, vínculos, sprints (Scrum) y leer el "
        "historial estructurado. Ubica ids con `list_spaces`/`list_boards`; `get_board` trae "
        "columnas y tarjetas en resumen (sin las de cierre salvo que las pidas), `get_task` lee una "
        "tarjeta entera por su clave («FNV-308») y `search_tasks` busca. "
        "La autenticación es interactiva: si no hay sesión, la primera acción "
        "abrirá un diálogo de inicio de sesión (o usa `login`). "
        f"Cada tablero tiene SUS columnas ({COLUMN_SETS_HINT}): míralas con `get_board` en vez de "
        "suponerlas, y fíjate en la `category` de cada una (`todo`/`in_progress`/`done`), que es lo "
        "único estable — el nombre lo cambia quien quiera. Al crear una tarjeta sin `column`, nace "
        "en la columna de entrada del tablero. Declara tu modelo una vez, al empezar, con "
        "`whoami(agent_model=...)`: el servidor lo recuerda y lo pone en todo lo que hagas en la "
        "sesión. Todo lo que escribes queda marcado como de IA (procedencia `ia`): no firmes los "
        "textos. Lo que escribe exige rol colaborador o dueño: el backend valida permisos por rol. "
        "Los gates humanos (`review_*`) son decisión de una persona: úsalos solo si te lo piden."
    ),
)


def _solo_lo_que_admite_el_spec(schema: dict[str, Any]) -> None:
    # Pydantic pone en la raíz el nombre de la clase (`title`) y su docstring (`description`), y el
    # SDK los deja pasar. La especificación solo admite `$schema`, `type`, `properties` y `required`:
    # Claude Code no se queja, pero Codex no sabe pintar el formulario y lo cancela sin enseñarlo
    # (FNV-314). Los campos sí llevan su título y descripción, que es lo que se ve en el diálogo.
    schema.pop("title", None)
    schema.pop("description", None)


class Formulario(BaseModel):
    """Lo que el MCP le pregunta a la persona por el cliente (elicitation)."""

    model_config = ConfigDict(json_schema_extra=_solo_lo_que_admite_el_spec)


class LoginForm(Formulario):
    """Formulario que el cliente muestra al usuario para iniciar sesión en Foundry."""

    email: str = Field(default="", description="Email de tu cuenta de Foundry.")
    password: str = Field(default="", description="Contraseña (o déjala vacía y pega un token abajo).")
    token: str = Field(
        default="", description="Alternativa: un JWT ya emitido. Si lo pones, se ignora email/contraseña."
    )
    remember: bool = Field(
        default=False,
        description="Recordar la sesión en el gestor de credenciales del SO (no en archivos ni en el repo).",
    )


# ── cliente perezoso ─────────────────────────────────────────────────────────
_client: FoundryClient | None = None


def _get_client() -> FoundryClient:
    global _client
    if _client is None:
        cfg = load_config()
        _client = FoundryClient(cfg.api_base, timeout=cfg.timeout)
    return _client


# ── keyring (best-effort; nunca bloquea la auth) ─────────────────────────────
def _keyring_get() -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
    except Exception:
        return None


def _keyring_set(token: str) -> None:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, token)
    except Exception:
        pass


def _keyring_clear() -> None:
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
    except Exception:
        pass


# ── flujo de autenticación ───────────────────────────────────────────────────
def _token_alive(token: str) -> bool:
    """¿El JWT sigue vivo? Solo mira su `exp` (sin verificar la firma: eso lo hace el backend).

    Con margen de 60 s para no usar un token que caduca a mitad de la petición. Un token que no
    se deja decodificar se da por muerto (mejor pedir login que reintentar a ciegas)."""
    import base64
    import json
    import time

    try:
        payload_b64 = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        return float(payload.get("exp", 0)) > time.time() + 60
    except Exception:
        return False


class ConfirmForm(Formulario):
    """Lo que se le enseña a la persona antes de algo que no se puede deshacer (FNV-269)."""

    confirmar: bool = Field(
        default=False, description="Sí, adelante. Esta acción no se puede deshacer."
    )


_PRONOMBRES = {"el": "lo", "la": "la", "los": "los", "las": "las"}


def _pronombre(que_se_pierde: str) -> str:
    """«La nota…» → «¿La borro?». Manda el artículo con el que empieza la frase.

    Preguntar «La nota … ¿Lo borro?» delata que la frase la pegó una plantilla, y quien lee eso
    justo antes de perder algo merece una pregunta escrita en su idioma. Sin artículo delante,
    el genérico «lo».
    """
    primera = que_se_pierde.lstrip().split(" ", 1)[0].lower()
    return _PRONOMBRES.get(primera, "lo")


def _puede_preguntar(ctx: Ctx) -> bool:
    """¿Declaró el cliente que sabe preguntar (elicitation)? Un contexto sin petición, no."""
    try:
        caps = ctx.client_capabilities
    except Exception:
        return False
    return caps is not None and caps.elicitation is not None


def _sin_canal_de_vuelta(ctx: Ctx) -> bool:
    """Desde 2026-07-28 el servidor ya no puede preguntar a mitad de una llamada: se pregunta antes
    del cuerpo, con un resolver, y el cliente reintenta con la respuesta (Multi Round-Trip)."""
    version = getattr(ctx, "protocol_version", None)
    return isinstance(version, str) and version >= "2026-07-28"


@dataclass(frozen=True)
class _Borrado:
    """Lo que se va a borrar, ya leído, y cómo se le cuenta a la persona antes de hacerlo."""

    que_se_pierde: str
    datos: dict[str, Any]


def _preguntar_borrado(borrado: _Borrado, ctx: Ctx) -> Elicit[ConfirmForm] | ConfirmForm:
    """La pregunta antes de borrar, con el dato delante: no «¿seguro?», sino qué desaparece.

    Va en un resolver, antes del cuerpo de la herramienta, para que el SDK la haga por el canal que
    toque: una petición del servidor hasta 2025-11-25, o un `input_required` que el cliente contesta
    reintentando en 2026-07-28 (FNV-310). Si el cliente **no sabe preguntar** (la elicitation es
    opcional), no se queda colgado ni se niega a trabajar: sigue adelante. El gate de verdad son los
    permisos del backend y la aprobación de la herramienta en el cliente; esto es la cortesía de
    avisar a quien va a perder algo (FNV-269). Cancelar, en cambio, sí detiene la operación.
    """
    if not _puede_preguntar(ctx):
        return ConfirmForm(confirmar=True)  # no hay a quién preguntar
    q = borrado.que_se_pierde
    return Elicit(f"{q} ¿{_pronombre(q).capitalize()} borro?", ConfirmForm)


def _exigir_si(confirmacion: ElicitationResult[ConfirmForm]) -> None:
    data = getattr(confirmacion, "data", None)
    if confirmacion.action != "accept" or data is None or not data.confirmar:
        raise FoundryError("Borrado cancelado: no se ha tocado nada.")


# Lo que se dice cuando el diálogo de login vuelve sin datos. No siempre lo canceló una persona: un
# cliente sin pantalla (p. ej. `codex exec`) lo cancela solo, y «cancelada por el usuario» mandaba a
# buscar a alguien que no existía (salió probando FNV-314).
_LOGIN_CANCELADO = (
    "No se ha iniciado sesión: el diálogo se canceló o se rechazó. Si el cliente no tiene pantalla "
    "(una ejecución automática, como `codex exec`), no puede mostrarlo: define FOUNDRY_TOKEN, o "
    "entra una vez desde un cliente interactivo marcando «recordar»."
)


async def _token_desde(form: LoginForm) -> str:
    """Del formulario de login a un token: el pegado tal cual, o el que da email + contraseña."""
    if form.token.strip():
        token = form.token.strip()
    elif form.email.strip() and form.password:
        token = await _get_client().obtain_token(form.email.strip(), form.password)
    else:
        raise FoundryError("Faltan datos: email + contraseña, o un token.")
    if form.remember:
        _keyring_set(token)
    return token


async def _reauth(ctx: Ctx) -> str:
    """Devuelve un token FRESCO: overrides de entorno → keyring (si otro proceso ya renovó) →
    si no queda otra, elicitation (el diálogo de login)."""
    cfg = load_config()
    client = _get_client()
    if cfg.env_token:
        return cfg.env_token
    if cfg.env_email and cfg.env_password:
        return await client.obtain_token(cfg.env_email, cfg.env_password)
    # El gestor de credenciales ANTES del diálogo (FNV-134): con varias sesiones abiertas, la
    # primera que renueva guarda el token nuevo y las demás lo reutilizan de aquí — sin volver a
    # pedir la contraseña N veces. Solo sirve si es distinto del que acaba de fallar y sigue vivo.
    stored = _keyring_get()
    if stored and stored != client.token and _token_alive(stored):
        return stored
    # En 2026-07-28 no se puede abrir el diálogo a mitad de una herramienta cualquiera: el que sabe
    # preguntar por el canal nuevo es `login`, que lo hace antes de su cuerpo (FNV-310).
    if _sin_canal_de_vuelta(ctx):
        raise FoundryError(
            "No hay sesión con Foundry, o ha caducado. Llama a la herramienta `login` (abre el "
            "diálogo para entrar) y repite lo que ibas a hacer."
        )
    # Interactivo: el cliente muestra el diálogo de login.
    try:
        result = await ctx.elicit(
            message=f"Inicia sesión en Foundry ({cfg.api_base}): email + contraseña, o pega un token.",
            schema=LoginForm,
        )
    except Exception as e:  # cliente MCP sin soporte de elicitation
        raise FoundryError(
            "Este cliente no puede pedir credenciales de forma interactiva. Define FOUNDRY_TOKEN "
            "o FOUNDRY_EMAIL/FOUNDRY_PASSWORD como variables de entorno."
        ) from e
    if result.action != "accept" or result.data is None:
        raise FoundryError(_LOGIN_CANCELADO)
    return await _token_desde(result.data)


def _prepare(ctx: Ctx, agent_model: str = "") -> FoundryClient:
    """Deja el cliente listo: fija `reauth` con el ctx actual y siembra el token del keyring."""
    client = _get_client()
    client.reauth = lambda: _reauth(ctx)
    if client.token is None:
        client.token = _keyring_get()  # siembra optimista; si expiró, el 401 dispara reauth
    set_agent_identity(_agent_source(ctx), _resolve_agent_model(agent_model, ctx=ctx))
    return client


# ── helpers de resolución (nombre → id) ──────────────────────────────────────
def _norm(s: str) -> str:
    """Normaliza para comparar: sin acentos, sin mayúsculas, sin espacios sobrantes."""
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.strip().lower()


def _resolve_agent_source(
    configured: str,
    client_name: str | None = None,
    client_title: str | None = None,
) -> str:
    """Identidad declarada del agente.

    Precedencia: override ``FOUNDRY_AGENT_SOURCE`` → ``clientInfo`` de la sesión MCP → unknown.
    La sesión la declara el cliente; sirve para trazabilidad operativa, no como prueba criptográfica.
    """

    override = _norm(configured).replace("-", "_").replace(" ", "_")
    if override and override != "auto":
        raw = override
    else:
        raw = _norm(" ".join(x for x in (client_name, client_title) if x))
    comparable = raw.replace("-", " ").replace("_", " ")
    if not comparable:
        return "unknown"
    if "claude" in comparable:
        return "claude_code"
    if "codex" in comparable:
        return "codex"
    if "opencode" in comparable or "open code" in comparable:
        return "opencode"
    if "gemini" in comparable:
        return "gemini"
    if comparable in {"unknown", "desconocido"}:
        return "unknown"
    if comparable in {"other", "otro"}:
        return "other"
    return "other"


def _agent_source(ctx: Ctx) -> str:
    """Resuelve la fuente para la llamada actual sin confiar en el texto del comentario."""

    # Con `initialize` (≤ 2025-11-25) sale del saludo; en 2026-07-28 no hay saludo y el SDK lo
    # sintetiza a partir del `_meta` de cada petición (`io.modelcontextprotocol/clientInfo`). En
    # los dos casos llega aquí, como `client_info` desde el SDK 2.x (`clientInfo` en la 1.x).
    params = getattr(ctx.session, "client_params", None)
    info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
    return _resolve_agent_source(
        load_config().agent_source,
        getattr(info, "name", None),
        getattr(info, "title", None),
    )


# El modelo que el agente ha declarado en esta sesión (FNV-311). Por stdio el cliente arranca un
# proceso por sesión, así que el módulo ES la sesión: lo declarado una vez vale para todo lo que
# venga después, también para las herramientas que no tienen el parámetro (notas, columnas,
# adjuntos…), porque la identidad viaja en las cabeceras de cada petición.
_declared_model = ""

# Dónde dice cada cliente, en el `_meta` de la petición, con qué modelo trabaja (FNV-319). Codex lo
# manda en cada `tools/call`: lo pone el cliente, no el modelo hablando de sí mismo (que se
# declaraba «gpt-6» corriendo «gpt-6-luna»), y sigue un cambio de `/model` a mitad de sesión.
# Claude Code y OpenCode no mandan nada parecido.
_CLIENT_MODEL_IN_META = (("x-codex-turn-metadata", "model"),)


def _client_model(ctx: Ctx | None) -> str | None:
    """El modelo que el cliente dice en la petición en curso, si lo dice."""
    try:
        meta = getattr(getattr(ctx, "request_context", None), "meta", None)
    except ValueError:  # el Context del SDK fuera de una petición
        return None
    if not isinstance(meta, Mapping):
        return None
    for block_key, model_key in _CLIENT_MODEL_IN_META:
        block = meta.get(block_key)
        model = block.get(model_key) if isinstance(block, Mapping) else None
        if isinstance(model, str) and model.strip():
            return model.strip()[:160]
    return None


def _resolve_agent_model(explicit: str = "", *, ctx: Ctx | None = None) -> str | None:
    """Lo que dice el cliente → lo declarado en esta llamada o antes en la sesión → configuración.

    MCP no tiene un campo para el modelo, pero algunos clientes lo mandan en el `_meta` de cada
    petición, y eso manda: es un dato del cliente. Si no, lo sabe el agente: basta con que lo diga
    una vez (lo normal: en ``whoami``); un valor nuevo sustituye al anterior, porque se puede
    cambiar de modelo sin cerrar la sesión.
    """

    global _declared_model
    value = explicit.strip()[:160]
    if value:
        _declared_model = value
    return _client_model(ctx) or _declared_model or load_config().agent_model.strip()[:160] or None


async def _add_agent_comment(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    content: str,
    *,
    entry_kind: str = "nota",
    meta: dict[str, Any] | None = None,
    agent_model: str = "",
) -> dict[str, Any]:
    """Único camino MCP para crear aportes: siempre adjunta la identidad de la sesión."""

    return await _prepare(ctx, agent_model).add_task_comment(
        initiative_id,
        task_id,
        content,
        entry_kind=entry_kind,
        meta=meta,
        agent_source=_agent_source(ctx),
        agent_model=_resolve_agent_model(agent_model, ctx=ctx),
    )


def _columns_of(detail: dict[str, Any]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = detail.get("board", {}).get("columns", [])
    return columns


def _resolve_column(columns: list[dict[str, Any]], column: str) -> str:
    """`column` por id exacto o por nombre (tolerante a acentos/mayúsculas). Devuelve column_id."""
    for c in columns:
        if str(c["id"]) == column:
            return str(c["id"])
    target = _norm(column)
    for c in columns:
        if _norm(c["name"]) == target:
            return str(c["id"])
    names = ", ".join(f'"{c["name"]}"' for c in columns) or "(el tablero no tiene columnas)"
    raise FoundryError(f'La columna "{column}" no existe en este tablero. Columnas: {names}.')


def _default_column_id(columns: list[dict[str, Any]]) -> str:
    """La columna de ENTRADA del tablero: donde nace una tarjeta a la que no se le pide columna.

    Se decide por **categoría y orden**: la primera de tipo `todo`. El nombre no pinta nada — desde
    FNV-256 las columnas se montan a mano, y un tablero puede tener una columna llamada "Backlog"
    que no sea su entrada (o que ni siquiera sea de entrada). Mirar el rótulo acertaba de casualidad
    en los presets y fallaba en silencio en cuanto alguien montaba las suyas: la tarjeta aparecía en
    otra columna y nadie se enteraba hasta buscarla.

    Sin categorías (datos anteriores al rediseño) se cae en la primera columna, que es lo único que
    queda por saber.
    """
    for c in columns:
        if c.get("category") == "todo":
            return str(c["id"])
    if columns:
        return str(columns[0]["id"])
    raise FoundryError("El tablero no tiene columnas.")


async def _resolve_assignee(space_id: str | None, assignee: str | None) -> str | None:
    """`assignee` (nombre/email de un miembro, o id) → user_id. En tableros personales solo id."""
    if not assignee:
        return None
    if space_id is None:
        return assignee  # tablero personal: no hay miembros; se acepta un id crudo
    members = await _get_client().list_members(space_id)
    for m in members:
        if str(m["user_id"]) == assignee:
            return assignee
    target = _norm(assignee)
    for m in members:
        if _norm(m.get("display_name", "")) == target or _norm(m.get("email", "")) == target:
            return str(m["user_id"])
    who = ", ".join(f'{m.get("display_name")} <{m.get("email")}>' for m in members) or "(sin miembros)"
    raise FoundryError(f'No encuentro a "{assignee}" entre los miembros del espacio. Miembros: {who}.')


def _validate(value: str, allowed: tuple[str, ...], label: str) -> str:
    v = _norm(value)
    if v not in allowed:
        raise FoundryError(f'{label} inválido: "{value}". Válidos: {", ".join(allowed)}.')
    return v


def _find_task(detail: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for col in _columns_of(detail):
        tasks: list[dict[str, Any]] = col.get("tasks", [])
        for t in tasks:
            if str(t["id"]) == task_id:
                return t
    return None


def _merge_date(new: str | None, current: Any) -> Any:
    """Semántica PATCH de una fecha: None = conservar la actual; "" = borrar; un valor = fijarlo."""
    if new is None:
        return current
    return new or None  # "" → None (borra la fecha)


def _is_textual(content_type: str) -> bool:
    """¿El adjunto se puede devolver como texto legible? (text/* o un MIME textual conocido)."""
    return content_type.startswith("text/") or content_type in _TEXTUAL_TYPES


def _tamano_legible(size: int) -> str:
    """El tamaño de un archivo en la unidad que le corresponde: bytes hasta 1 KB, luego KB y MB.

    Redondear todo a KB enseñaba «0 KB» para un archivo pequeño, y eso aparece justo delante de
    quien está decidiendo si borra algo que no se recupera (FNV-269).
    """
    if size < 1024:
        return f"{size} B"
    if size < 1_048_576:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1_048_576:.1f} MB"


def _enumerar(piezas: list[str]) -> str:
    """Una lista dicha como se dice en castellano: «a, b y c», no «a y b y c».

    Aparece en el aviso de lo que se pierde al borrar, que es texto para una persona: si se lee
    como una máquina enumerando, se lee por encima — y ahí es donde no conviene (FNV-269).
    """
    if len(piezas) < 2:
        return "".join(piezas)
    return f"{', '.join(piezas[:-1])} y {piezas[-1]}"


def _attachment_source(
    path: str, filename: str, text: str, base64_data: str
) -> tuple[bytes, str]:
    """De dónde salen los bytes de un adjunto que se sube: una ruta, un texto o base64 (FNV-266).

    Exactamente una de las tres, porque mezclarlas no significa nada y adivinar cuál gana sería
    peor que no subir. Devuelve (bytes, nombre del archivo).
    """
    dadas = [bool(path.strip()), bool(text), bool(base64_data.strip())]
    if sum(dadas) != 1:
        raise FoundryError(
            "Di de dónde sale el archivo con UNA de estas tres: `path` (una ruta del disco), "
            "`text` (el contenido) o `base64_data` (bytes en base64)."
        )
    if path.strip():
        origen = Path(path.strip()).expanduser()
        if not origen.is_file():
            raise FoundryError(f"No hay ningún archivo en «{origen}».")
        return origen.read_bytes(), filename.strip() or origen.name
    nombre = filename.strip()
    if not nombre:
        raise FoundryError(
            "Falta `filename`: sin nombre no hay extensión, y sin extensión el adjunto se guarda "
            "como binario genérico y se descarga sin saber qué es."
        )
    if text:
        return text.encode("utf-8"), nombre
    try:
        return base64.b64decode(base64_data.strip(), validate=True), nombre
    except (binascii.Error, ValueError) as e:
        raise FoundryError(f"`base64_data` no es base64 válido: {e}") from e


def _attachment_payload(data: bytes, content_type: str, filename: str) -> Image | str:
    """Convierte los bytes de un adjunto en algo que Claude pueda consumir: una **imagen** si lo es
    (para verla), su **texto** si es legible, o un aviso con metadatos si es un binario opaco."""
    if content_type.startswith("image/"):
        return Image(data=data, format=content_type.split("/", 1)[1] or "png")
    if _is_textual(content_type):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            return f'Adjunto "{filename}" ({content_type}, {len(data)} bytes):\n\n{text}'
    return (
        f'Adjunto "{filename}" ({content_type}, {len(data)} bytes): no es una imagen ni texto '
        "legible; el MCP no puede mostrarlo aquí. Descárgalo desde la app de Foundry."
    )


# ── tools: sesión ────────────────────────────────────────────────────────────
def _pedir_credenciales(ctx: Ctx) -> Elicit[LoginForm] | LoginForm:
    """El diálogo de `login`, como resolver: el SDK lo abre por el canal del protocolo de la
    conexión, también en 2026-07-28, donde a mitad de una herramienta ya no se puede (FNV-310).
    Las credenciales de entorno (automatización) entran sin preguntar, como siempre."""
    cfg = load_config()
    if cfg.env_token:
        return LoginForm(token=cfg.env_token)
    if cfg.env_email and cfg.env_password:
        return LoginForm(email=cfg.env_email, password=cfg.env_password)
    if not _puede_preguntar(ctx):
        raise FoundryError(
            "Este cliente no puede pedir credenciales de forma interactiva. Define FOUNDRY_TOKEN "
            "o FOUNDRY_EMAIL/FOUNDRY_PASSWORD como variables de entorno."
        )
    pregunta = f"Inicia sesión en Foundry ({cfg.api_base}): email + contraseña, o pega un token."
    return Elicit(pregunta, LoginForm)


@mcp.tool(annotations=ACTUALIZA)
async def login(
    ctx: Ctx, credenciales: Annotated[ElicitationResult[LoginForm], Resolve(_pedir_credenciales)]
) -> str:
    """Inicia sesión en Foundry (abre un diálogo para email + contraseña, o un token).

    Normalmente no hace falta llamarla: la primera acción sobre un tablero ya pide la sesión.
    Útil para iniciar sesión por adelantado, cambiar de cuenta, o cuando otra herramienta diga que
    no hay sesión.
    """
    data = getattr(credenciales, "data", None)
    if credenciales.action != "accept" or data is None:
        raise FoundryError(_LOGIN_CANCELADO)
    client = _prepare(ctx)
    client.token = await _token_desde(data)
    me = await client.me()
    return f"Sesión iniciada como {me.get('display_name')} <{me.get('email')}>."


@mcp.tool(annotations=ACTUALIZA)
async def logout() -> str:
    """Cierra la sesión: olvida el token (de la memoria y del gestor de credenciales del SO)."""
    _keyring_clear()
    _get_client().token = None
    return "Sesión cerrada: token olvidado."


# ── tools: lectura ───────────────────────────────────────────────────────────
@mcp.tool(annotations=SOLO_LEE)
async def whoami(ctx: Ctx, agent_model: str = "") -> dict[str, Any]:
    """Cuenta con la que actúa el MCP (id, email, nombre) y con qué identidad firma el agente.

    Llámala al empezar **declarando `agent_model`** (el modelo con el que trabajas, p. ej.
    `claude-opus-5-5` o `gpt-6-luna`): el servidor lo recuerda y lo pone en todo lo que hagas
    durante la sesión, aunque no lo repitas. `agente` devuelve lo que quedará registrado.
    """
    me = await _prepare(ctx, agent_model).me()
    agente = {"agent_source": _agent_source(ctx), "agent_model": _resolve_agent_model(ctx=ctx)}
    return {**me, "agente": agente}


@mcp.tool(annotations=SOLO_LEE)
async def list_spaces(ctx: Ctx) -> list[dict[str, Any]]:
    """Lista las Áreas de trabajo (espacios) visibles: id, name, owner_id, is_personal."""
    return await _prepare(ctx).list_spaces()


@mcp.tool(annotations=SOLO_LEE)
async def list_boards(ctx: Ctx, space_id: str | None = None) -> list[dict[str, Any]]:
    """Lista tableros/iniciativas (id, title, space_id, maturity).

    - sin `space_id`: los tableros **personales** de la cuenta (creados sin Área).
    - con `space_id`: las iniciativas/tableros de esa **Área**.
    """
    client = _prepare(ctx)
    items = await (client.list_space_initiatives(space_id) if space_id else client.list_personal_boards())
    return [
        {"id": i["id"], "title": i["title"], "space_id": i.get("space_id"), "maturity": i.get("maturity")}
        for i in items
    ]


@mcp.tool(annotations=SOLO_LEE)
async def list_all_boards(ctx: Ctx) -> dict[str, Any]:
    """Panorama completo: tableros personales + los de cada Área a la que pertenece la cuenta.

    Devuelve {"personal": [...], "spaces": [{"space": {...}, "boards": [...]}, ...]}.
    """
    client = _prepare(ctx)
    personal = await client.list_personal_boards()
    spaces = await client.list_spaces()
    out_spaces: list[dict[str, Any]] = []
    for s in spaces:
        boards = await client.list_space_initiatives(str(s["id"]))
        out_spaces.append({"space": s, "boards": boards})
    return {"personal": personal, "spaces": out_spaces}


# ── lectura del tablero sin desbordar el contexto (FNV-312) ──────────────────
# La API devuelve el tablero entero —en el FNV, 590 000 caracteres, el 80 % de la columna Hecho y la
# mayoría descripciones— y no tiene una ruta para una sola tarjeta. Se filtra aquí: la red sigue
# bajando todo, pero al contexto del agente solo llega lo que pidió, que es lo caro.
DETAILS = ("resumen", "completo")
# Lo que hace falta para decidir qué tocar: qué es, de quién cuelga, qué la frena y qué espera a una
# persona. Sin descripción ni procedencia: eso se lee con `get_task` cuando toca.
_CARD_SUMMARY_FIELDS = (
    "id", "title", "issue_type", "priority", "parent_id", "assignee_id", "labels", "status_agent",
    "flagged", "gate_pending", "tests_total", "tests_passed", "comment_count", "sprint_id",
    "story_points", "due_date",
)  # fmt: skip


def _card_key(board_key: str | None, task: dict[str, Any]) -> str | None:
    """«FNV-308»: como nombra la gente una tarjeta. La API solo trae el número (`seq`)."""
    seq = task.get("seq")
    return f"{board_key}-{seq}" if board_key and seq is not None else None


def _label_names(task: dict[str, Any]) -> list[str]:
    return [str(lbl.get("name")) for lbl in task.get("labels") or [] if isinstance(lbl, dict)]


def _card_summary(board_key: str | None, task: dict[str, Any]) -> dict[str, Any]:
    """La tarjeta en una línea: los campos vacíos, a cero o en falso no se mandan."""
    out: dict[str, Any] = {"key": _card_key(board_key, task)}
    for field in _CARD_SUMMARY_FIELDS:
        value = _label_names(task) if field == "labels" else task.get(field)
        if value not in (None, "", [], 0, False):
            out[field] = value
    return out


def _all_tasks(detail: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(columna, tarjeta) de todo el tablero, en el orden en que se ve."""
    return [(col, task) for col in _columns_of(detail) for task in col.get("tasks") or []]


def _locate_task(detail: dict[str, Any], task: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Una tarjeta por id, por clave («FNV-308», sin distinguir mayúsculas) o por su número («308»)."""
    wanted = task.strip()
    number = wanted.rsplit("-", 1)[-1].lstrip("#")
    prefix = wanted.rsplit("-", 1)[0].upper() if "-" in wanted else ""
    board_key = str(detail.get("key") or "").upper()
    for col, item in _all_tasks(detail):
        if str(item.get("id")) == wanted:
            return col, item
    if number.isdigit() and prefix in ("", board_key):
        for col, item in _all_tasks(detail):
            if str(item.get("seq")) == number:
                return col, item
    raise FoundryError(
        f"No hay ninguna tarjeta «{task}» en este tablero ({detail.get('title')}, clave "
        f"{detail.get('key')}). Búscala con `search_tasks`."
    )


@mcp.tool(annotations=SOLO_LEE)
async def get_board(
    ctx: Ctx,
    initiative_id: str,
    categories: list[str] | None = None,
    detail: str = "resumen",
    limit_per_column: int | None = None,
) -> dict[str, Any]:
    """Trae un tablero: la ficha de la iniciativa y sus columnas con sus tarjetas.

    Columna: id, name, order, `category` y `color`. **Mira la categoría**, no el nombre (que cambia):
    `todo` = por donde entra el trabajo · `in_progress` = en curso · `done` = cierra la tarjeta.

    Por defecto, para que quepa: tarjetas de `todo` e `in_progress` (las de `done` llegan plegadas,
    con `task_count`/`tasks_omitted`; pídelas con `categories`) y en resumen, sin descripción ni
    campos vacíos (`detail="completo"` trae todo). `limit_per_column` pone tope por columna.
    `key` es la clave de la tarjeta («FNV-308»): ábrela con `get_task`; busca con `search_tasks`.
    `status_agent="proposed"`: propuesta por IA o salida de una nota.
    """
    wanted = tuple(categories) if categories else ("todo", "in_progress")
    bad = [c for c in wanted if c not in COLUMN_CATEGORIES]
    if bad:
        raise FoundryError(f"categories no válidas: {bad}. Usa {', '.join(COLUMN_CATEGORIES)}.")
    if detail not in DETAILS:
        raise FoundryError(f"detail debe ser {' o '.join(DETAILS)}.")
    data = await _prepare(ctx).get_initiative(initiative_id)
    board_key = data.get("key")
    for col in _columns_of(data):
        tasks = col.get("tasks") or []
        shown = tasks if col.get("category") in wanted else []
        if limit_per_column is not None:
            shown = shown[: max(limit_per_column, 0)]
        col["task_count"] = len(tasks)
        if len(shown) < len(tasks):
            col["tasks_omitted"] = len(tasks) - len(shown)
        if detail == "resumen":
            col["tasks"] = [_card_summary(board_key, t) for t in shown]
        else:
            col["tasks"] = [{"key": _card_key(board_key, t), **t} for t in shown]
    return data


@mcp.tool(annotations=SOLO_LEE)
async def get_task(ctx: Ctx, initiative_id: str, task: str) -> dict[str, Any]:
    """Lee UNA tarjeta entera, con descripción, por id o por clave («FNV-308», «308»). Trae `key` y
    `column` (id, name, category). Su hilo: `get_task_comments`; criterios: `list_criteria`; casos de
    prueba: `list_test_cases`.
    """
    data = await _prepare(ctx).get_initiative(initiative_id)
    col, item = _locate_task(data, task)
    column = {"id": col.get("id"), "name": col.get("name"), "category": col.get("category")}
    return {"key": _card_key(data.get("key"), item), **item, "column": column}


@mcp.tool(annotations=SOLO_LEE)
async def search_tasks(
    ctx: Ctx,
    initiative_id: str,
    text: str = "",
    labels: list[str] | None = None,
    column: str = "",
    issue_type: str = "",
    include_done: bool = False,
    limit: int = 25,
) -> dict[str, Any]:
    """Busca tarjetas de un tablero; una línea por tarjeta (id, key, title, column, category,
    issue_type, priority, labels).

    `text`: en título, descripción o clave, sin distinguir acentos ni mayúsculas. `labels`: con TODAS
    estas. `issue_type`: epic|historia|tarea|bug|incidencia|subtarea. `column`: solo esa (nombre o
    id); sin ella se miran `todo` e `in_progress`, e `include_done=True` añade las de cierre.
    `limit`: 25 por omisión; `total` dice cuántas coincidían.
    """
    if issue_type and issue_type not in ISSUE_TYPES:
        raise FoundryError(f"issue_type no válido: {issue_type}. Usa {', '.join(ISSUE_TYPES)}.")
    data = await _prepare(ctx).get_initiative(initiative_id)
    board_key = data.get("key")
    only_column = _resolve_column(_columns_of(data), column) if column else None
    needle = _norm(text)
    wanted_labels = {_norm(lbl) for lbl in labels or []}
    found: list[dict[str, Any]] = []
    for col, item in _all_tasks(data):
        if only_column is not None:
            if str(col.get("id")) != only_column:
                continue
        elif col.get("category") == "done" and not include_done:
            continue
        key = _card_key(board_key, item) or ""
        haystack = _norm(f"{key} {item.get('title') or ''} {item.get('description') or ''}")
        if needle and needle not in haystack:
            continue
        if wanted_labels and not wanted_labels <= {_norm(n) for n in _label_names(item)}:
            continue
        if issue_type and item.get("issue_type") != issue_type:
            continue
        found.append(
            {
                "id": item.get("id"),
                "key": key or None,
                "title": item.get("title"),
                "column": col.get("name"),
                "category": col.get("category"),
                "issue_type": item.get("issue_type"),
                "priority": item.get("priority"),
                "labels": _label_names(item),
            }
        )
    return {"total": len(found), "tasks": found[: max(limit, 0)]}


# ── tools: tableros ──────────────────────────────────────────────────────────
@mcp.tool(annotations=SOLO_LEE)
async def list_board_presets(ctx: Ctx) -> list[dict[str, Any]]:
    """Las plantillas de columnas para crear un tablero: `key` (lo que se pasa a `create_board`),
    `label`, `template` (`kanban` o `scrum`, que enciende los sprints), `is_default` y sus `columns`
    con su categoría. Léelas antes de crear: las claves no se adivinan.
    """
    return await _prepare(ctx).list_board_presets()


@mcp.tool(annotations=CREA)
async def create_board(
    ctx: Ctx,
    title: str,
    space_id: str | None = None,
    preset: str = "",
    columns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Crea un tablero: personal sin `space_id`, o de un Área con `space_id`.

    Columnas con las que nace: sin nada, las de la plantilla por omisión; con `preset`, las de una
    plantilla de `list_board_presets`; con `columns`, a medida:
    `[{"name": "Ideas", "category": "todo"}, …]`, entre 2 y 10, `category` en todo/in_progress/done,
    `color` opcional (`#rrggbb`), al menos una de entrada y una de cierre y sin nombres repetidos.
    Con columnas a medida manda TAMBIÉN el `preset`: fija la plantilla (`kanban`/`scrum`) y con ella
    si hay sprints. Si algo no cuadra, el servidor lo rechaza sin crear nada a medias.
    """
    client = _prepare(ctx)
    clean = preset.strip() or None
    if space_id:
        return await client.create_space_board(space_id, title, clean, columns)
    return await client.create_personal_board(title, clean, columns)


@mcp.tool(annotations=ACTUALIZA)
async def move_board_to_space(ctx: Ctx, initiative_id: str, space_id: str) -> dict[str, Any]:
    """Mueve un tablero **personal** a un Área de trabajo para compartirlo con el equipo."""
    return await _prepare(ctx).move_board(initiative_id, space_id)


# ── tools: columnas del tablero (FNV-267) ─────────────────────────────────────
#
# Configurar el tablero es de quien lo monta, y por eso estas herramientas se estrenan tarde: el
# agente ya podía leer las columnas y trabajar con ellas, pero no cambiarlas. Son útiles para dejar
# un tablero listo desde una conversación ("móntame el flujo con una columna de bloqueadas") sin
# obligar a nadie a ir a los ajustes.
#
# Las REGLAS no se duplican aquí: el servidor aplica el tope de diez, los nombres sin repetir y el
# invariante de ≥1 columna de entrada y ≥1 de cierre. Lo que sí hace el MCP es **explicar antes de
# fallar** cuando la información ya la tiene delante (p. ej. cuántas tarjetas hay que reubicar).


async def _board_columns(client: FoundryClient, initiative_id: str) -> list[dict[str, Any]]:
    return _columns_of(await client.get_initiative(initiative_id))


async def _lo_que_se_pierde(
    client: FoundryClient, initiative_id: str, task: dict[str, Any]
) -> str:
    """Qué desaparece con una tarjeta, dicho en una frase (FNV-269).

    Los contadores ya vienen en la tarjeta —el tablero los trae para los distintivos—, así que
    decirlo no cuesta una ronda de llamadas. Es la diferencia entre «¿seguro?» y saber que ahí
    dentro hay doce comentarios que nadie ha leído desde marzo.
    """
    piezas: list[str] = []
    for cuantos, singular, plural in (
        (int(task.get("comment_count") or 0), "comentario", "comentarios"),
        (int(task.get("attachment_count") or 0), "adjunto", "adjuntos"),
        (int(task.get("tests_total") or 0), "caso de prueba", "casos de prueba"),
    ):
        if cuantos:
            piezas.append(f"{cuantos} {singular if cuantos == 1 else plural}")
    hijas = [
        t
        for c in _columns_of(await client.get_initiative(initiative_id))
        for t in (c.get("tasks") or [])
        if str(t.get("parent_id") or "") == str(task.get("id"))
    ]
    if hijas:
        piezas.append(f"{len(hijas)} {'tarjeta hija' if len(hijas) == 1 else 'tarjetas hijas'}")
    detalle = ", con " + _enumerar(piezas) if piezas else ", sin historial ni adjuntos"
    return f'La tarjeta «{task.get("title")}»{detalle}.'


@mcp.tool(annotations=CREA)
async def add_column(
    ctx: Ctx,
    initiative_id: str,
    name: str,
    category: str = "in_progress",
    color: str = "",
) -> list[dict[str, Any]]:
    """Añade una columna al final y devuelve todas. `category`: todo · in_progress · done.
    `color`: `#rrggbb` opcional (sin él, el de su categoría). Máximo diez y sin nombres repetidos.
    """
    client = _prepare(ctx)
    await client.add_column(
        initiative_id, name, _validate(category, COLUMN_CATEGORIES, "category"), color or None
    )
    return await _board_columns(client, initiative_id)


@mcp.tool(annotations=ACTUALIZA)
async def rename_column(
    ctx: Ctx, initiative_id: str, column: str, name: str
) -> list[dict[str, Any]]:
    """Renombra una columna (por nombre actual o por id) y devuelve cómo quedan todas.

    El id de la columna no cambia, así que **el historial de las tarjetas no se toca**: lo que
    quedó registrado sigue apuntando a esta columna aunque se llame de otra forma.
    """
    client = _prepare(ctx)
    columns = await _board_columns(client, initiative_id)
    await client.rename_column(initiative_id, _resolve_column(columns, column), name)
    return await _board_columns(client, initiative_id)


@mcp.tool(annotations=ACTUALIZA)
async def set_column_category(
    ctx: Ctx, initiative_id: str, column: str, category: str
) -> list[dict[str, Any]]:
    """Cambia el tipo de una columna (todo · in_progress · done): es lo que decide dónde entra
    una tarjeta y qué la cierra. El tablero debe conservar al menos una de entrada y una de cierre.
    """
    client = _prepare(ctx)
    columns = await _board_columns(client, initiative_id)
    await client.set_column_category(
        initiative_id,
        _resolve_column(columns, column),
        _validate(category, COLUMN_CATEGORIES, "category"),
    )
    return await _board_columns(client, initiative_id)


@mcp.tool(annotations=ACTUALIZA)
async def set_column_color(
    ctx: Ctx, initiative_id: str, column: str, color: str = ""
) -> list[dict[str, Any]]:
    """Pinta una columna de un color propio (`#rrggbb`), o se lo quita con `color` vacío.

    Sin color propio, la columna usa el de su categoría — que es lo que se ve por omisión y lo que
    vuelve al quitarlo. Es solo apariencia: ningún comportamiento depende del color.
    """
    client = _prepare(ctx)
    columns = await _board_columns(client, initiative_id)
    await client.set_column_color(initiative_id, _resolve_column(columns, column), color or None)
    return await _board_columns(client, initiative_id)


@mcp.tool(annotations=ACTUALIZA)
async def reorder_columns(
    ctx: Ctx, initiative_id: str, columns: list[str]
) -> list[dict[str, Any]]:
    """Reordena las columnas: pasa **todas**, por nombre o id, en el orden final. Si falta o
    sobra alguna, el servidor lo rechaza.
    """
    client = _prepare(ctx)
    actuales = await _board_columns(client, initiative_id)
    ids = [_resolve_column(actuales, c) for c in columns]
    if len(set(ids)) != len(actuales):
        faltan = [str(c["name"]) for c in actuales if str(c["id"]) not in set(ids)]
        raise FoundryError(
            "Hay que pasar TODAS las columnas del tablero en el orden que quedan. "
            f"Faltan: {', '.join(faltan) or '(ninguna, pero hay repetidas)'}."
        )
    await client.reorder_columns(initiative_id, ids)
    return await _board_columns(client, initiative_id)


async def _borrado_columna(initiative_id: str, column: str, reassign_to: str, ctx: Ctx) -> _Borrado:
    client = _prepare(ctx)
    columns = await _board_columns(client, initiative_id)
    column_id = _resolve_column(columns, column)
    objetivo = next(c for c in columns if str(c["id"]) == column_id)
    dentro = len(objetivo.get("tasks") or [])
    destino_id = _resolve_column(columns, reassign_to) if reassign_to else None
    if dentro and destino_id is None:
        otras = ", ".join(f'"{c["name"]}"' for c in columns if str(c["id"]) != column_id)
        raise FoundryError(
            f'La columna "{objetivo["name"]}" tiene {dentro} '
            f'{"tarjeta" if dentro == 1 else "tarjetas"} dentro: di a dónde se mudan con '
            f"`reassign_to`. Columnas destino: {otras}."
        )
    if destino_id == column_id:
        raise FoundryError("La columna destino no puede ser la que se borra.")
    if dentro:
        cuantas = "su tarjeta se muda" if dentro == 1 else f"sus {dentro} tarjetas se mudan"
        destino = next(c["name"] for c in columns if str(c["id"]) == destino_id)
        mudanza = f" y {cuantas} a «{destino}»"
    else:
        mudanza = ", que está vacía"
    destino_nombre = next((c["name"] for c in columns if str(c["id"]) == destino_id), None)
    return _Borrado(
        f'La columna «{objetivo["name"]}»{mudanza}.',
        {"column_id": column_id, "name": objetivo["name"], "dentro": dentro,
         "destino_id": destino_id, "destino": destino_nombre},
    )  # fmt: skip


async def _confirma_columna(
    borrado: Annotated[_Borrado, Resolve(_borrado_columna)], ctx: Ctx
) -> Elicit[ConfirmForm] | ConfirmForm:
    return _preguntar_borrado(borrado, ctx)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Borrar una columna del tablero", destructive_hint=True, idempotent_hint=False
    )
)
async def delete_column(
    ctx: Ctx,
    initiative_id: str,
    column: str,
    borrado: Annotated[_Borrado, Resolve(_borrado_columna)],
    confirmacion: Annotated[ElicitationResult[ConfirmForm], Resolve(_confirma_columna)],
    reassign_to: str = "",
) -> dict[str, Any]:
    """Borra una columna. Si tiene tarjetas, di a dónde van con `reassign_to` (otra columna, por
    nombre o id): se mudan con su estado y queda en su historial. El tablero no puede quedarse sin
    columna de entrada ni de cierre.
    """
    _exigir_si(confirmacion)
    client = _prepare(ctx)
    d = borrado.datos
    await client.delete_column(initiative_id, d["column_id"], d["destino_id"])
    return {
        "deleted": d["name"],
        "moved_tasks": d["dentro"],
        "moved_to": d["destino"],
        "columns": await _board_columns(client, initiative_id),
    }


# ── tools: tarjetas ──────────────────────────────────────────────────────────
# Nace oficial aunque la cree la IA (FNV-109): la procedencia es información, no fricción;
# el control lo tiene la persona que pide la tarjeta. Lo que sí debe revisar alguien va por
# los gates (`propose_criterion`, `record_decision`).
@mcp.tool(annotations=CREA)
async def create_task(
    ctx: Ctx,
    initiative_id: str,
    title: str,
    column: str = "",
    description: str = "",
    priority: str = "medium",
    kind: str = "tarea",
    assignee: str | None = None,
    issue_type: str = "tarea",
    parent_id: str | None = None,
    story_points: float | None = None,
    agent_model: str = "",
) -> dict[str, Any]:
    """Crea una tarjeta (work item) en un tablero. Nace al final de la columna.

    `column`: nombre o id; **déjalo vacío** y nacerá en la columna de entrada del tablero (la primera
    de categoría `todo`), porque los nombres cambian de un tablero a otro.
    `priority`: highest|high|medium|low|lowest (o baja|media|alta). `kind`: tarea|bug|incidencia.
    `issue_type`: epic|historia|tarea|bug|incidencia|subtarea; `parent_id`: la tarjeta padre. Una
    épica agrupa historias, tareas, bugs e incidencias; una historia, tareas, bugs, incidencias y
    subtareas; una tarea/bug/incidencia, subtareas. La subtarea exige padre; la épica no lo admite.
    `story_points`: estimación. `assignee`: nombre, email o id de un miembro.

    Nace **oficial**, no propuesta: lo que deba revisar una persona va con `propose_criterion` o
    `record_decision`.
    """
    client = _prepare(ctx, agent_model)
    detail = await client.get_initiative(initiative_id)
    columns = _columns_of(detail)
    column_id = _resolve_column(columns, column) if column else _default_column_id(columns)
    assignee_id = await _resolve_assignee(detail.get("space_id"), assignee)
    body: dict[str, Any] = {
        "column_id": column_id,
        "title": title,
        "description": description,
        "priority": _validate(priority, PRIORITIES, "priority"),
        "kind": _validate(kind, KINDS, "kind"),
        "assignee_id": assignee_id,
        "issue_type": _validate(issue_type, ISSUE_TYPES, "issue_type"),
        "as_agent": True,  # el MCP es la IA: la tarjeta declara su procedencia (FNV-109)
        "agent_source": _agent_source(ctx),
        "agent_model": _resolve_agent_model(agent_model, ctx=ctx),
    }
    if parent_id:
        body["parent_id"] = parent_id
    if story_points is not None:
        body["story_points"] = story_points
    return await client.create_task(initiative_id, body)


@mcp.tool(annotations=ACTUALIZA)
async def move_task(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    column: str,
    order: int = 0,
    agent_model: str = "",
) -> dict[str, Any]:
    """Mueve una tarjeta a otra columna y/o posición.

    `column`: nombre o id de la columna destino. `order`: índice destino dentro de la columna (0 = arriba).
    """
    client = _prepare(ctx, agent_model)
    detail = await client.get_initiative(initiative_id)
    column_id = _resolve_column(_columns_of(detail), column)
    return await client.move_task(initiative_id, task_id, column_id, max(0, order))


@mcp.tool(annotations=ACTUALIZA)
async def edit_task(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    kind: str | None = None,
    assignee: str | None = None,
    agent_model: str = "",
) -> dict[str, Any]:
    """Edita una tarjeta; solo cambian los campos que pases.

    `priority`: highest|high|medium|low|lowest (o baja|media|alta). `kind`: tarea|bug|incidencia.
    `assignee`: nombre, email o id; `""` lo quita. Tipo y padre: `set_task_hierarchy`; estimación y
    fechas: `set_task_details`.
    """
    client = _prepare(ctx, agent_model)
    detail = await client.get_initiative(initiative_id)
    task = _find_task(detail, task_id)
    if task is None:
        raise FoundryError(f"La tarea {task_id} no está en el tablero {initiative_id}.")
    body: dict[str, Any] = {
        "title": title if title is not None else task["title"],
        "description": description if description is not None else task.get("description", ""),
        "priority": _validate(priority, PRIORITIES, "priority") if priority is not None else task["priority"],
        "kind": _validate(kind, KINDS, "kind") if kind is not None else task["kind"],
        "as_agent": True,
        "agent_source": _agent_source(ctx),
        "agent_model": _resolve_agent_model(agent_model, ctx=ctx),
    }
    if assignee is None:
        body["assignee_id"] = task.get("assignee_id")
    elif assignee == "":
        body["assignee_id"] = None
    else:
        body["assignee_id"] = await _resolve_assignee(detail.get("space_id"), assignee)
    return await client.edit_task(initiative_id, task_id, body)


async def _borrado_tarjeta(initiative_id: str, task_id: str, ctx: Ctx) -> _Borrado:
    client = _prepare(ctx)
    detail = await client.get_initiative(initiative_id)
    task = _find_task(detail, task_id)
    if task is None:
        raise FoundryError(f"La tarjeta {task_id} no está en este tablero.")
    return _Borrado(await _lo_que_se_pierde(client, initiative_id, task), task)


async def _confirma_tarjeta(
    borrado: Annotated[_Borrado, Resolve(_borrado_tarjeta)], ctx: Ctx
) -> Elicit[ConfirmForm] | ConfirmForm:
    return _preguntar_borrado(borrado, ctx)


@mcp.tool(annotations=BORRA)
async def delete_task(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    borrado: Annotated[_Borrado, Resolve(_borrado_tarjeta)],
    confirmacion: Annotated[ElicitationResult[ConfirmForm], Resolve(_confirma_tarjeta)],
) -> dict[str, Any]:
    """Borra una tarjeta (irreversible). Antes pregunta, diciendo qué se va con ella (historial,
    adjuntos, criterios); si el cliente no sabe preguntar, sigue y lo dice en la respuesta.
    """
    _exigir_si(confirmacion)
    await _prepare(ctx).delete_task(initiative_id, task_id)
    return {
        "status": "deleted",
        "task_id": task_id,
        "era": borrado.datos.get("title"),
        "perdido": borrado.que_se_pierde,
    }


@mcp.tool(annotations=CREA)
async def create_tasks_from_seeds(
    ctx: Ctx, initiative_id: str, seed_ids: list[str], agent_model: str = ""
) -> list[dict[str, Any]]:
    """Manda semillas del Vivero como tarjetas **propuestas** a la columna de entrada del
    tablero; se aceptan o rechazan desde la app. `seed_ids`: semillas del mismo espacio.
    """
    return await _prepare(ctx, agent_model).create_tasks_from_seeds(initiative_id, seed_ids)


# ── tools: comentarios / historial ────────────────────────────────────────────
@mcp.tool(annotations=CREA)
async def add_task_comment(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    content: str,
    agent_model: str = "",
) -> dict[str, Any]:
    """Deja una **nota** libre en el hilo de una tarjeta. Para un avance, criterio, decisión,
    bloqueo, verificación o pregunta usa mejor `add_task_entry` o su tool propia: queda tipado.
    """
    return await _add_agent_comment(
        ctx, initiative_id, task_id, content, agent_model=agent_model
    )


@mcp.tool(annotations=CREA)
async def add_task_entry(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    content: str,
    kind: str = "avance",
    meta: dict[str, Any] | None = None,
    agent_model: str = "",
) -> dict[str, Any]:
    """Deja un **aporte tipado** en el hilo de una tarjeta, en vez de un comentario plano.

    `kind`: avance · criterio · decision · bloqueo (levanta la bandera de impedimento) ·
    verificacion · pregunta · nota. `meta`: JSON libre para máquinas (p. ej. `{"commit": "abc123"}`).
    Para avance, bloqueo y verificación usa mejor `report_progress`, `flag_blocker` y
    `report_verification`, que estructuran los datos.
    """
    return await _add_agent_comment(
        ctx,
        initiative_id,
        task_id,
        content,
        entry_kind=kind,
        meta=meta,
        agent_model=agent_model,
    )


async def _ensure_repo_link(
    client: FoundryClient,
    initiative_id: str,
    task_id: str,
    repo_url: str,
    kind: str = "commit",
    title: str = "",
) -> list[dict[str, Any]]:
    """Deja la tarjeta enlazada a esa URL y devuelve sus vínculos. Si ya lo estaba, no hace nada.

    El backend contesta 400 a un enlace repetido, y aquí es normal repetirlo: una decisión y el
    avance que la cuenta citan el mismo commit. Ese 400 se llevaba por delante el avance entero, que
    era lo que importaba guardar (salió cerrando FNV-317).
    """
    links = await client.list_repo_links(initiative_id, task_id)
    if any(lk.get("url") == repo_url for lk in links):
        return links
    return await client.link_repo(initiative_id, task_id, repo_url, kind, title)


async def _repo_meta(
    client: FoundryClient, initiative_id: str, task_id: str, repo_url: str, kind: str = "commit"
) -> dict[str, Any]:
    """Ata la tarjeta al código (ADR-0016) y devuelve el trozo de `meta` que hace visible ese enlace
    **dentro** de la propia entrada del hilo, no solo en la lista de repos de la tarjeta."""
    if not repo_url:
        return {}
    await _ensure_repo_link(client, initiative_id, task_id, repo_url, kind)
    return {"repo_url": repo_url}


@mcp.tool(annotations=CREA)
async def report_progress(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    content: str,
    percent: int | None = None,
    checklist: list[str] | None = None,
    repo_url: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Deja un **avance** en una tarjeta: qué se hizo, cuánto queda y en qué código se ve.

    `percent`: 0–100. `checklist`: pasos `"[x] hecho"` / `"[ ] pendiente"`. `repo_url`: el commit o
    PR; queda vinculado a la tarjeta y enlazado en la entrada.
    """
    if percent is not None and not 0 <= percent <= 100:
        raise FoundryError(f"percent debe estar entre 0 y 100 (recibido: {percent}).")
    client = _prepare(ctx, agent_model)
    meta = await _repo_meta(client, initiative_id, task_id, repo_url)
    if percent is not None:
        meta["percent"] = percent
    if checklist:
        meta["checklist"] = checklist
    return await client.add_task_comment(
        initiative_id,
        task_id,
        content,
        entry_kind="avance",
        meta=meta or None,
        agent_source=_agent_source(ctx),
        agent_model=_resolve_agent_model(agent_model, ctx=ctx),
    )


@mcp.tool(annotations=CREA)
async def flag_blocker(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    reason: str,
    repo_url: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Marca una tarjeta como **bloqueada** y por qué: deja el motivo en el hilo y levanta la
    bandera de impedimento del tablero. Úsalo en cuanto algo te frene (una credencial, una decisión
    humana, un test que no pasa), no solo en el chat. `repo_url`: el commit/PR donde se ve.
    """
    client = _prepare(ctx, agent_model)
    meta = await _repo_meta(client, initiative_id, task_id, repo_url)
    return await client.add_task_comment(
        initiative_id,
        task_id,
        reason,
        entry_kind="bloqueo",
        meta=meta or None,
        agent_source=_agent_source(ctx),
        agent_model=_resolve_agent_model(agent_model, ctx=ctx),
    )


@mcp.tool(annotations=CREA)
async def report_verification(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    status: str,
    detail: str = "",
    url: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Deja la **verificación** de una tarjeta: la evidencia de que funciona, o de que no.

    `status`: ok|fallo|parcial. `detail`: qué probaste y con qué resultado (tests, build, prueba a
    mano), con números. `url`: la evidencia (CI, PR, captura).
    """
    st = _validate(status, VERIFICATION_STATUSES, "status")
    meta: dict[str, Any] = {"status": st}
    if url:
        meta["url"] = url
    return await _add_agent_comment(
        ctx,
        initiative_id,
        task_id,
        detail or f"Verificación: {st}",
        entry_kind="verificacion",
        meta=meta,
        agent_model=agent_model,
    )


@mcp.tool(annotations=CREA)
async def propose_criterion(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    text: str,
    agent_model: str = "",
) -> dict[str, Any]:
    """Propone un **criterio de aceptación** («para darla por hecha debe cumplirse Z»). Nace
    propuesto y cuenta cuando lo acepta una persona. Úsalo al encontrar una condición verificable.
    """
    return await _prepare(ctx, agent_model).add_task_criterion(initiative_id, task_id, text)


@mcp.tool(annotations=CREA)
async def record_decision(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    title: str,
    rationale: str = "",
    repo_url: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Registra una **decisión de diseño** de una tarjeta («elegí A por B»). Nace propuesta: la
    acepta o descarta una persona. Con `repo_url` (commit, PR o repo) queda atada a ese código.
    """
    client = _prepare(ctx, agent_model)
    repo_link_id: str | None = None
    if repo_url:
        # Reusa el vínculo de repo (ADR-0016): lo crea si no estaba y toma su id.
        links = await _ensure_repo_link(client, initiative_id, task_id, repo_url)
        repo_link_id = next(
            (lk["id"] for lk in links if lk.get("url") == repo_url),
            links[-1]["id"] if links else None,
        )
    return await client.add_task_decision(
        initiative_id, task_id, title, rationale, repo_link_id=repo_link_id
    )


@mcp.tool(annotations=SOLO_LEE)
async def list_criteria(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lee los criterios de aceptación de una tarjeta: `status` = proposed (espera a una persona) ·
    accepted · rejected, más `source` (humano|ia|colaborativo) y `checked`. Sirve para ver qué se
    decidió sobre lo que propusiste, también si se decidió desde la app.
    """
    return await _prepare(ctx).list_task_criteria(initiative_id, task_id)


@mcp.tool(annotations=ACTUALIZA)
async def review_criterion(
    ctx: Ctx, initiative_id: str, task_id: str, criterion_id: str, accept: bool
) -> dict[str, Any]:
    """**Gate humano** de un criterio: `accept=True` lo vuelve oficial y `accept=False` lo
    descarta (`criterion_id` de `list_criteria`). Es la decisión de una persona: úsala solo si te lo
    piden y **nunca** sobre lo que propusiste tú.
    """
    return await _prepare(ctx).review_task_criterion(
        initiative_id, task_id, criterion_id, accept=accept
    )


@mcp.tool(annotations=SOLO_LEE)
async def list_decisions(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lee las decisiones de diseño de una tarjeta: `status` (proposed · accepted · rejected),
    `source` (humano|ia|colaborativo) y el `repo_link_id` al que atan, si lo hay.
    """
    return await _prepare(ctx).list_task_decisions(initiative_id, task_id)


@mcp.tool(annotations=ACTUALIZA)
async def review_decision(
    ctx: Ctx, initiative_id: str, task_id: str, decision_id: str, accept: bool
) -> dict[str, Any]:
    """**Gate humano** de una decisión: `accept=True` la vuelve oficial y `accept=False` la
    descarta (`decision_id` de `list_decisions`). Es la decisión de una persona: úsala solo si te lo
    piden y **nunca** sobre lo que registraste tú.
    """
    return await _prepare(ctx).review_task_decision(
        initiative_id, task_id, decision_id, accept=accept
    )


@mcp.tool(annotations=ACTUALIZA)
async def review_task_proposal(
    ctx: Ctx, initiative_id: str, task_id: str, accept: bool
) -> dict[str, Any]:
    """**Gate humano** de una tarjeta propuesta (`status_agent="proposed"`): `accept=True` la
    vuelve oficial; `accept=False` la marca `rejected` (se oculta, no se borra). Es la decisión de una
    persona: úsala solo si te lo piden y **nunca** sobre lo que propusiste tú.
    """
    return await _prepare(ctx).review_task_proposal(initiative_id, task_id, accept=accept)


@mcp.tool(annotations=CREA)
async def add_test_case(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    title: str,
    steps: str = "",
    expected: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Deja un **caso de prueba** en un entregable: una historia (lo normal) o una
    tarea/bug/incidencia suelta; épicas y subtareas, no. Apunta a 1–4 por historia.

    `title`: el escenario; `steps`: cómo ejecutarlo; `expected`: qué debe pasar. Nace `pending` y sin
    gate: se valida corriéndolo con `mark_test_case` (el criterio de aceptación, con gate, es
    `propose_criterion`).
    """
    return await _prepare(ctx, agent_model).add_test_case(
        initiative_id, task_id, title, steps, expected
    )


@mcp.tool(annotations=SOLO_LEE)
async def list_test_cases(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lee los casos de prueba de una tarjeta: estado (pending · passed · failed), pasos,
    resultado esperado y procedencia. Con todos en `passed` está lista para cerrar, pero la cierra
    una persona.
    """
    return await _prepare(ctx).list_test_cases(initiative_id, task_id)


@mcp.tool(annotations=ACTUALIZA)
async def mark_test_case(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    test_case_id: str,
    status: str,
    agent_model: str = "",
) -> dict[str, Any]:
    """Registra el resultado de correr un caso de prueba: `status` = passed · failed · pending.

    El `test_case_id` sale de `list_test_cases`. Cada marca deja una corrida en la bitácora
    (`list_test_executions`), aunque repita el veredicto anterior.
    """
    return await _prepare(ctx, agent_model).mark_test_case(
        initiative_id, task_id, test_case_id, status
    )


@mcp.tool(annotations=SOLO_LEE)
async def list_test_executions(
    ctx: Ctx, initiative_id: str, task_id: str, test_case_id: str = ""
) -> list[dict[str, Any]]:
    """Lee la bitácora de corridas de los casos de prueba: resultado, quién (humano o agente y
    modelo), en qué columna y cuándo; se guarda aunque el veredicto repita. Sin `test_case_id`, toda
    la tarjeta. El estado actual está en `list_test_cases`.
    """
    return await _prepare(ctx).list_test_executions(
        initiative_id, task_id, test_case_id or None
    )


@mcp.tool(annotations=SOLO_LEE)
async def get_task_comments(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lista el historial/comentarios de una tarjeta (autor, tipo human|agent, texto y fecha)."""
    return await _prepare(ctx).list_task_comments(initiative_id, task_id)


@mcp.tool(annotations=SOLO_LEE)
async def get_task_history(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Historial estructurado de una tarjeta: qué campo cambió (`column_id`, `priority`,
    `assignee_id`, `sprint_id`, `label`, `link`…), de qué valor a cuál, quién y cuándo. Valores en
    crudo (ids, enums). Los comentarios van en `get_task_comments`.
    """
    return await _prepare(ctx).get_task_history(initiative_id, task_id)


# ── tools: workflow (transición de estado) ─────────────────────────────────────
@mcp.tool(annotations=ACTUALIZA)
async def transition_task(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    status: str,
    order: int = 0,
    agent_model: str = "",
) -> dict[str, Any]:
    """Mueve una tarjeta a otro estado, que es su columna: `status` = nombre o id de la columna
    destino **de ese tablero** (no hay estados universales: míralos con `get_board`). Queda en el
    historial. `order`: 0 = arriba.
    """
    client = _prepare(ctx, agent_model)
    detail = await client.get_initiative(initiative_id)
    column_id = _resolve_column(_columns_of(detail), status)
    return await client.move_task(initiative_id, task_id, column_id, max(0, order))


# ── tools: jerarquía / campos ricos ────────────────────────────────────────────
@mcp.tool(annotations=ACTUALIZA)
async def set_task_hierarchy(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    issue_type: str,
    parent_id: str | None = None,
    agent_model: str = "",
) -> dict[str, Any]:
    """Fija el tipo y el padre de una tarjeta (es un `set` explícito).

    `issue_type`: epic|historia|tarea|bug|incidencia|subtarea. Una épica agrupa historias, tareas,
    bugs e incidencias; una historia, tareas, bugs, incidencias y subtareas; una tarea/bug/incidencia,
    subtareas. La subtarea exige padre, la épica no lo admite y no puede haber ciclos.
    """
    body: dict[str, Any] = {"issue_type": _validate(issue_type, ISSUE_TYPES, "issue_type")}
    if parent_id:
        body["parent_id"] = parent_id
    return await _prepare(ctx, agent_model).set_task_hierarchy(initiative_id, task_id, body)


@mcp.tool(annotations=ACTUALIZA)
async def set_task_details(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    story_points: float | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    flagged: bool | None = None,
    agent_model: str = "",
) -> dict[str, Any]:
    """Cambia estimación, fechas o impedimento de una tarjeta; lo que no pases se conserva.

    `story_points`: número ≥ 0. `start_date`/`due_date`: ISO (AAAA-MM-DD); `""` la borra. `flagged`:
    pone o quita el impedimento.
    """
    client = _prepare(ctx, agent_model)
    detail = await client.get_initiative(initiative_id)
    task = _find_task(detail, task_id)
    if task is None:
        raise FoundryError(f"La tarea {task_id} no está en el tablero {initiative_id}.")
    body: dict[str, Any] = {
        "story_points": task.get("story_points") if story_points is None else story_points,
        "flagged": bool(task.get("flagged", False)) if flagged is None else flagged,
        "start_date": _merge_date(start_date, task.get("start_date")),
        "due_date": _merge_date(due_date, task.get("due_date")),
    }
    return await client.set_task_details(initiative_id, task_id, body)


@mcp.tool(annotations=ACTUALIZA)
async def set_task_labels(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    labels: list[str],
    agent_model: str = "",
) -> list[dict[str, Any]]:
    """Reemplaza el conjunto de etiquetas de una tarjeta por `labels` (nombres). Crea en el catálogo
    del tablero las que no existan (sin distinguir mayúsculas). Pasa `[]` para quitar todas.
    """
    client = _prepare(ctx, agent_model)
    catalog = await client.list_labels(initiative_id)
    by_name = {_norm(lb["name"]): str(lb["id"]) for lb in catalog}
    label_ids: list[str] = []
    for name in labels:
        key = _norm(name)
        if key not in by_name:
            created = await client.create_label(initiative_id, name.strip())
            by_name[key] = str(created["id"])
        label_ids.append(by_name[key])
    return await client.set_task_labels(initiative_id, task_id, label_ids)


# ── tools: épicas ──────────────────────────────────────────────────────────────
@mcp.tool(annotations=CREA)
async def create_epic(
    ctx: Ctx,
    initiative_id: str,
    title: str,
    column: str = "",
    description: str = "",
    priority: str = "medium",
    agent_model: str = "",
) -> dict[str, Any]:
    """Crea una **épica** (work item raíz que agrupa historias, tareas, bugs e incidencias). Atajo de
    `create_task` con `issue_type=epic`. Cuelga hijas de ella con `parent_id` al crearlas; a su vez,
    una historia puede agrupar tareas, bugs, incidencias y subtareas.
    """
    # `create_task` pasó por el decorador de FastMCP, que la devuelve sin tipo: se lo pone esta línea.
    epic: dict[str, Any] = await create_task(
        ctx,
        initiative_id,
        title,
        column=column,
        description=description,
        priority=priority,
        issue_type="epic",
        agent_model=agent_model,
    )
    return epic


@mcp.tool(annotations=SOLO_LEE)
async def list_epics(ctx: Ctx, initiative_id: str) -> list[dict[str, Any]]:
    """Lista las épicas del tablero (issue_type=epic): id, title, column_id, priority."""
    detail = await _prepare(ctx).get_initiative(initiative_id)
    epics: list[dict[str, Any]] = []
    for col in _columns_of(detail):
        for t in col.get("tasks", []):
            if t.get("issue_type") == "epic":
                epics.append(
                    {
                        "id": t["id"],
                        "title": t["title"],
                        "column_id": t["column_id"],
                        "priority": t.get("priority"),
                    }
                )
    return epics


# ── tools: vínculos ────────────────────────────────────────────────────────────
@mcp.tool(annotations=CREA)
async def link_tasks(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    target_id: str,
    link_type: str = "relates_to",
    agent_model: str = "",
) -> list[dict[str, Any]]:
    """Vincula dos tarjetas del mismo tablero. `link_type`: blocks|is_blocked_by|relates_to (desde
    la óptica de `task_id`). Devuelve los vínculos de `task_id`.
    """
    lt = _validate(link_type, LINK_TYPES, "link_type")
    return await _prepare(ctx, agent_model).link_tasks(initiative_id, task_id, target_id, lt)


@mcp.tool(annotations=SOLO_LEE)
async def get_task_links(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lista los vínculos de una tarjeta desde su óptica: cada uno con `id` (del vínculo),
    `link_type` (blocks|is_blocked_by|relates_to), `other_id` y `other_title` (la otra tarjeta).
    Útil para revisarlos o antes de `unlink_tasks`.
    """
    return await _prepare(ctx).list_task_links(initiative_id, task_id)


@mcp.tool(annotations=BORRA)
async def unlink_tasks(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    target_id: str,
    link_type: str | None = None,
    agent_model: str = "",
) -> list[dict[str, Any]]:
    """Quita vínculos entre `task_id` y `target_id`: sin `link_type`, todos; con él (blocks ·
    is_blocked_by · relates_to, desde `task_id`), solo ese. Devuelve los que quedan.
    """
    client = _prepare(ctx, agent_model)
    want_type = _validate(link_type, LINK_TYPES, "link_type") if link_type else None
    links = await client.list_task_links(initiative_id, task_id)
    matches = [
        lk
        for lk in links
        if str(lk["other_id"]) == target_id
        and (want_type is None or lk["link_type"] == want_type)
    ]
    if not matches:
        suffix = f" de tipo {want_type}" if want_type else ""
        raise FoundryError(
            f"No hay ningún vínculo entre {task_id} y {target_id}{suffix}."
        )
    remaining = links
    for lk in matches:
        remaining = await client.unlink_tasks(initiative_id, task_id, str(lk["id"]))
    return remaining


# ── tools: vínculos de repositorio (ADR-0016, Opción A) ──────────────────────────
# Idempotente (FNV-226): enlazar lo que ya estaba enlazado no es un error, es lo normal.
@mcp.tool(annotations=ACTUALIZA)
async def link_repo(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    url: str,
    kind: str = "repository",
    title: str = "",
    agent_model: str = "",
) -> list[dict[str, Any]]:
    """Vincula una tarjeta a un recurso de repositorio por URL; si ya lo estaba, no hace nada.
    `kind`: repository|branch|pull_request|commit. `title`: etiqueta legible opcional («PR #12»).
    Devuelve los vínculos de la tarjeta.
    """
    k = _validate(kind, REPO_KINDS, "kind")
    return await _ensure_repo_link(_prepare(ctx, agent_model), initiative_id, task_id, url, k, title)


@mcp.tool(annotations=SOLO_LEE)
async def get_repo_links(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lista los recursos de repositorio de una tarjeta: cada uno con `id` (del vínculo), `url`,
    `kind` (repository|branch|pull_request|commit) y `title`. Útil antes de `unlink_repo`.
    """
    return await _prepare(ctx).list_repo_links(initiative_id, task_id)


@mcp.tool(annotations=BORRA)
async def unlink_repo(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    url: str,
    agent_model: str = "",
) -> list[dict[str, Any]]:
    """Quita el vínculo de repositorio de `task_id` cuya URL coincide con `url` (resuelve el id del
    vínculo solo). Devuelve los vínculos restantes.
    """
    client = _prepare(ctx, agent_model)
    links = await client.list_repo_links(initiative_id, task_id)
    matches = [lk for lk in links if str(lk["url"]) == url]
    if not matches:
        raise FoundryError(f"La tarjeta {task_id} no tiene ningún repo vinculado con esa URL.")
    remaining = links
    for lk in matches:
        remaining = await client.unlink_repo(initiative_id, task_id, str(lk["id"]))
    return remaining


# ── tools: sprints (Scrum) ─────────────────────────────────────────────────────
@mcp.tool(annotations=SOLO_LEE)
async def list_sprints(ctx: Ctx, initiative_id: str) -> list[dict[str, Any]]:
    """Lista los sprints del tablero (id, name, state future|active|closed, goal, fechas)."""
    return await _prepare(ctx).list_sprints(initiative_id)


@mcp.tool(annotations=CREA)
async def create_sprint(
    ctx: Ctx,
    initiative_id: str,
    name: str,
    goal: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    agent_model: str = "",
) -> dict[str, Any]:
    """Crea un sprint **planificado** (`future`) en un tablero Scrum. Fechas ISO (YYYY-MM-DD)."""
    body: dict[str, Any] = {"name": name, "goal": goal}
    if start_date:
        body["start_date"] = start_date
    if end_date:
        body["end_date"] = end_date
    return await _prepare(ctx, agent_model).create_sprint(initiative_id, body)


@mcp.tool(annotations=ACTUALIZA)
async def start_sprint(
    ctx: Ctx, initiative_id: str, sprint_id: str, agent_model: str = ""
) -> dict[str, Any]:
    """Arranca un sprint (future → active). Falla si ya hay otro sprint activo en el tablero."""
    return await _prepare(ctx, agent_model).start_sprint(initiative_id, sprint_id)


@mcp.tool(annotations=ACTUALIZA)
async def complete_sprint(
    ctx: Ctx, initiative_id: str, sprint_id: str, agent_model: str = ""
) -> dict[str, Any]:
    """Completa un sprint (active → closed); las tarjetas sin terminar vuelven al backlog."""
    return await _prepare(ctx, agent_model).complete_sprint(initiative_id, sprint_id)


@mcp.tool(annotations=ACTUALIZA)
async def assign_task_to_sprint(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    sprint_id: str | None = None,
    agent_model: str = "",
) -> dict[str, Any]:
    """Mueve una tarjeta a un sprint, o al **backlog** si `sprint_id` se omite/es None. El sprint
    debe ser del mismo tablero y no estar cerrado. Deja rastro en el historial (campo sprint_id).
    """
    return await _prepare(ctx, agent_model).assign_task_sprint(
        initiative_id, task_id, sprint_id or None
    )


# ── tools: adjuntos ────────────────────────────────────────────────────────────
@mcp.tool(annotations=SOLO_LEE)
async def list_task_attachments(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lista los adjuntos (metadatos) de una tarjeta: id, filename, content_type, size, quién lo
    subió y cuándo. Para VER o leer el contenido de uno, usa `read_task_attachment` con su id.
    """
    return await _prepare(ctx).list_task_attachments(initiative_id, task_id)


@mcp.tool(annotations=SOLO_LEE, structured_output=False)  # imagen o texto
async def read_task_attachment(
    ctx: Ctx, initiative_id: str, task_id: str, attachment_id: str
) -> Image | str:
    """Lee un adjunto por su id (de `list_task_attachments`). Una imagen vuelve como imagen,
    para verla; el texto (text/*, json, csv, yaml, sql…), como texto; otro binario (pdf, zip…), solo
    sus metadatos: se descarga desde la app.
    """
    data, content_type, filename = await _prepare(ctx).read_task_attachment(
        initiative_id, task_id, attachment_id
    )
    return _attachment_payload(data, content_type, filename)


@mcp.tool(annotations=CREA)
async def upload_task_attachment(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    path: str = "",
    filename: str = "",
    text: str = "",
    base64_data: str = "",
) -> dict[str, Any]:
    """Sube un archivo a una tarjeta (un informe, un diagrama, una captura). Tope: 10 MB.

    Usa exactamente UNA forma: `path` (ruta en el equipo del agente; el nombre sale de ella salvo
    que pases `filename`), `text` (contenido tal cual; **exige `filename`**) o `base64_data` (bytes
    en base64; **exige `filename`**). El tipo se deduce de la extensión: **ponla**, o se guardará
    como binario genérico.
    """
    data, name = _attachment_source(path, filename, text, base64_data)
    if not data:
        raise FoundryError("El archivo está vacío: no hay nada que subir.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise FoundryError(
            f"«{name}» ocupa {_tamano_legible(len(data))} y el tope son 10 MB. "
            "Sube una versión más ligera o enlaza el archivo desde la tarjeta."
        )
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return await _prepare(ctx).add_task_attachment(
        initiative_id, task_id, name, content_type, data
    )


async def _borrado_adjunto(initiative_id: str, task_id: str, attachment_id: str, ctx: Ctx) -> _Borrado:
    antes = await _prepare(ctx).list_task_attachments(initiative_id, task_id)
    adjunto = next((a for a in antes if str(a.get("id")) == attachment_id), None)
    if adjunto is None:
        raise FoundryError(
            f"El adjunto {attachment_id} no está en esta tarjeta. "
            f"Tiene {len(antes)}: míralos con `list_task_attachments`."
        )
    peso = _tamano_legible(int(adjunto.get("size") or 0))
    return _Borrado(
        f'El adjunto «{adjunto.get("filename")}» ({peso}) de esta tarjeta.',
        {**adjunto, "remaining": len(antes) - 1},
    )


async def _confirma_adjunto(
    borrado: Annotated[_Borrado, Resolve(_borrado_adjunto)], ctx: Ctx
) -> Elicit[ConfirmForm] | ConfirmForm:
    return _preguntar_borrado(borrado, ctx)


@mcp.tool(annotations=BORRA)
async def delete_task_attachment(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    attachment_id: str,
    borrado: Annotated[_Borrado, Resolve(_borrado_adjunto)],
    confirmacion: Annotated[ElicitationResult[ConfirmForm], Resolve(_confirma_adjunto)],
) -> dict[str, Any]:
    """Borra un adjunto de una tarjeta (irreversible; el id sale de
    `list_task_attachments`). Dice qué se borró y queda en el historial.
    """
    _exigir_si(confirmacion)
    await _prepare(ctx).delete_task_attachment(initiative_id, task_id, attachment_id)
    return {
        "deleted": True,
        "id": attachment_id,
        "filename": borrado.datos.get("filename"),
        "size": borrado.datos.get("size"),
        "remaining": borrado.datos["remaining"],
    }


# ── notas: la base de conocimiento (FNV-131 escritura · FNV-132 lectura) ─────
#
# Las notas viven en un CRDT (el editor manda); el servidor escribe por la puerta de siembra
# (FNV-130): el contenido queda materializado al instante (búsqueda/grafo lo ven) y el PRIMER
# editor que abra la nota lo vuelca al CRDT — quien la tenga abierta lo ve al recargar.
# PROCEDENCIA: lo que se escribe por aquí es contenido de IA; dilo en la nota cuando aporte
# (p. ej. una línea final "— redactado por Claude") y NO reescribas notas humanas sin pedirlo.

_MD_WRITE_NOTICE = (
    "La nota queda materializada (la búsqueda ya la ve) y pendiente de sembrar: el editor la "
    "vuelca al CRDT en la PRÓXIMA apertura; si alguien la tiene abierta, la verá al recargar."
)


def _inline_md(nodes: Any) -> str:
    """Runs inline de BlockNote → markdown (negrita/cursiva/tachado/código/enlaces)."""
    if not isinstance(nodes, list):
        return ""
    parts: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("type") == "link":
            parts.append(f"[{_inline_md(n.get('content'))}]({n.get('href', '')})")
            continue
        if n.get("type") == "mention":
            parts.append(str((n.get("props") or {}).get("text", "")))
            continue
        text = str(n.get("text", ""))
        styles = n.get("styles") or {}
        if styles.get("code"):
            text = f"`{text}`"
        if styles.get("bold"):
            text = f"**{text}**"
        if styles.get("italic"):
            text = f"*{text}*"
        if styles.get("strike"):
            text = f"~~{text}~~"
        parts.append(text)
    return "".join(parts)


def _block_to_md(block: dict[str, Any], depth: int) -> str:
    """Un bloque materializado → su línea (o líneas) markdown."""
    c = block.get("content") or {}
    bn_type = c.get("type") or block.get("type", "paragraph")
    props = c.get("props") or {}
    inline = _inline_md(c.get("content")) or str(block.get("text", ""))
    pad = "  " * depth
    if bn_type == "heading":
        return f"{'#' * int(props.get('level', 1) or 1)} {inline}"
    if bn_type == "bulletListItem":
        return f"{pad}- {inline}"
    if bn_type == "numberedListItem":
        return f"{pad}1. {inline}"
    if bn_type == "checkListItem":
        mark = "x" if props.get("checked") else " "
        return f"{pad}- [{mark}] {inline}"
    if bn_type == "quote":
        return f"> {inline}"
    if bn_type == "codeBlock":
        lang = str(props.get("language", "") or "")
        code = "".join(
            str(n.get("text", "")) for n in (c.get("content") or []) if isinstance(n, dict)
        )
        return f"```{lang}\n{code}\n```"
    if bn_type == "table":
        rows = ((c.get("content") or {}).get("rows")) or []
        lines: list[str] = []
        for i, row in enumerate(rows):
            cells = [
                _inline_md(cell.get("content")) for cell in row.get("cells", []) if isinstance(cell, dict)
            ]
            lines.append("| " + " | ".join(cells) + " |")
            if i == 0:
                lines.append("|" + "---|" * max(1, len(cells)))
        return "\n".join(lines)
    if bn_type == "chart":
        return f"*(gráfico: {block.get('text', '')})*"
    return f"{pad}{inline}" if depth else inline


def _blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Los bloques materializados de una nota → markdown legible (jerarquía por parent_block_id)."""
    depth_of: dict[str, int] = {}
    lines: list[str] = []
    for b in blocks:
        parent = b.get("parent_block_id")
        depth = (depth_of.get(str(parent), -1) + 1) if parent else 0
        depth_of[str(b.get("id"))] = depth
        lines.append(_block_to_md(b, depth))
    return "\n".join(lines).strip()


class NoteSpec(BaseModel):
    """Una nota a crear dentro de una colección: su título y su contenido markdown."""

    title: str = Field(description="Título de la nota.")
    markdown: str = Field(default="", description="Contenido markdown (puede ir vacío).")


@mcp.tool(annotations=CREA)
async def create_note(
    ctx: Ctx,
    space_id: str,
    title: str,
    markdown: str = "",
    parent_id: str | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    """Crea una nota en el Área, opcionalmente dentro de otra y con contenido. `markdown` admite
    encabezados, listas (anidadas y checklists), citas, código, tablas, negrita y enlaces; los
    [[wikilinks]] crean backlinks. No pises trabajo humano sin pedirlo.
    """
    client = _prepare(ctx)
    doc = await client.create_document(space_id, title, parent_id)
    if icon:
        doc = await client.update_document(str(doc["id"]), {"icon": icon})
    written = 0
    if markdown.strip():
        result = await client.write_document(str(doc["id"]), markdown, "replace")
        written = int(result.get("blocks_written", 0))
    return {"document": doc, "blocks_written": written, "nota": _MD_WRITE_NOTICE}


@mcp.tool(annotations=CREA)
async def write_note(
    ctx: Ctx, document_id: str, markdown: str, mode: str = "replace"
) -> dict[str, Any]:
    """Escribe markdown en una nota que YA existe. `mode`: replace (la pisa; deja versión en el
    historial y gana sobre lo no guardado del editor) o append (añade al final). En una nota escrita
    por personas, usa `append` o pregunta antes de reemplazar.
    """
    if mode not in ("replace", "append"):
        raise FoundryError(f'Modo inválido: "{mode}". Usa replace o append.')
    result = await _prepare(ctx).write_document(document_id, markdown, mode)
    return {**result, "nota": _MD_WRITE_NOTICE}


@mcp.tool(annotations=CREA)
async def create_note_collection(
    ctx: Ctx,
    space_id: str,
    folder_title: str,
    notes: list[NoteSpec],
    icon: str = "📁",
) -> dict[str, Any]:
    """Crea de un tiro una carpeta con su árbol de notas dentro (una «colección de markdown»).

    Crea la nota-carpeta (con icono) y cada nota de `notes` como hija, escribiendo su markdown.
    Devuelve la carpeta y las notas creadas con sus ids.
    """
    client = _prepare(ctx)
    folder = await client.create_document(space_id, folder_title, None)
    folder = await client.update_document(str(folder["id"]), {"icon": icon})
    # Dos pasadas a propósito: primero existen TODAS, luego se escriben. Los [[wikilinks]] se
    # resuelven al escribir, así que una colección que se enlaza entre sí necesita a sus
    # destinos ya creados — si no, los backlinks internos nacen muertos.
    created: list[dict[str, Any]] = []
    for spec in notes:
        created.append(await client.create_document(space_id, spec.title, str(folder["id"])))
    for spec, doc in zip(notes, created, strict=True):  # una nota creada por spec, o hay un bug
        if spec.markdown.strip():
            await client.write_document(str(doc["id"]), spec.markdown, "replace")
    return {"folder": folder, "notes": created, "nota": _MD_WRITE_NOTICE}


@mcp.tool(annotations=ACTUALIZA)
async def move_note(ctx: Ctx, document_id: str, parent_id: str | None = None) -> dict[str, Any]:
    """Cuelga la nota de otra carpeta/nota, o la saca a la raíz (sin `parent_id`)."""
    return await _prepare(ctx).update_document(document_id, {"parent_id": parent_id})


@mcp.tool(annotations=ACTUALIZA)
async def rename_note(
    ctx: Ctx, document_id: str, title: str, icon: str | None = None
) -> dict[str, Any]:
    """Renombra una nota (y opcionalmente cambia su icono)."""
    body: dict[str, Any] = {"title": title}
    if icon is not None:
        body["icon"] = icon
    return await _prepare(ctx).update_document(document_id, body)


async def _borrado_nota(document_id: str, ctx: Ctx) -> _Borrado:
    client = _prepare(ctx)
    try:
        # El detalle viene envuelto (`{document, blocks}`): leer el título del envoltorio daba
        # siempre None y se preguntaba por un uuid — que no le dice a nadie qué está borrando.
        nota = (await client.get_document(document_id)).get("document") or {}
        titulo = nota.get("title") or document_id
    except FoundryError:
        titulo = document_id
    try:
        enlaces = len(await client.document_backlinks(document_id))
    except Exception:
        enlaces = 0
    cuantos = (
        f", enlazada desde {enlaces} {'nota' if enlaces == 1 else 'notas'}" if enlaces else ""
    )
    return _Borrado(f"La nota «{titulo}»{cuantos}.", {"title": titulo})


async def _confirma_nota(
    borrado: Annotated[_Borrado, Resolve(_borrado_nota)], ctx: Ctx
) -> Elicit[ConfirmForm] | ConfirmForm:
    return _preguntar_borrado(borrado, ctx)


@mcp.tool(annotations=BORRA)
async def delete_note(
    ctx: Ctx,
    document_id: str,
    borrado: Annotated[_Borrado, Resolve(_borrado_nota)],
    confirmacion: Annotated[ElicitationResult[ConfirmForm], Resolve(_confirma_nota)],
) -> str:
    """Borra una nota (sus hijas quedan huérfanas: muévelas antes si importan). Antes pregunta,
    diciendo cuál es y cuántas notas la enlazan; si el cliente no sabe preguntar, sigue.
    """
    _exigir_si(confirmacion)
    await _prepare(ctx).delete_document(document_id)
    return f"Nota «{borrado.datos['title']}» borrada."


@mcp.tool(annotations=CREA)
async def link_notes(ctx: Ctx, from_document_id: str, to_document_id: str) -> dict[str, Any]:
    """Enlaza dos notas: añade al final de la de origen un [[wikilink]] visible a la de destino,
    que es lo que alimenta los backlinks y el mapa del Área.
    """
    client = _prepare(ctx)
    target = await client.get_document(to_document_id)
    title = str(target.get("document", {}).get("title") or "").strip()
    if not title:
        raise FoundryError("La nota destino no tiene título: ponle uno antes de enlazarla.")
    result = await client.write_document(from_document_id, f"Ver [[{title}]]", "append")
    return {**result, "linked_to": title}


@mcp.tool(annotations=CREA)
async def link_note_to_task(
    ctx: Ctx, document_id: str, target_id: str, lead: str = ""
) -> dict[str, Any]:
    """Ata una nota a una tarjeta o semilla que YA existe (el chip `#tarjeta`, sin abrir el editor).

    Si la tarjeta debe NACER de la nota, usa `extract_tasks_from_note`. El chip se añade al final de
    la nota, visible; escribir `#PROY-12` con `write_note` NO sirve: queda texto, no vínculo. Luego
    la tarjeta la enseña en `get_task_notes`. `target_id`: id de la tarjeta o semilla, del Área de la
    nota. `lead`: texto antes del chip (por defecto «Menciona a » / «Nace de »).
    """
    result = await _prepare(ctx).link_document_mention(document_id, target_id, lead)
    return {**result, "hint": "Queda escrito en la nota; se ve en el editor al recargarla."}


@mcp.tool(annotations=SOLO_LEE)
async def list_notes(ctx: Ctx, space_id: str) -> list[dict[str, Any]]:
    """El árbol de notas del Área: id, título, icono y las hijas anidadas en `children`."""
    docs = await _prepare(ctx).list_documents(space_id)
    by_id: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for d in docs:
        by_id[str(d["id"])] = {
            "id": str(d["id"]),
            "title": d.get("title", ""),
            "icon": d.get("icon"),
            "children": [],
        }
    for d in docs:
        node = by_id[str(d["id"])]
        parent = by_id.get(str(d.get("parent_id") or ""))
        (parent["children"] if parent else roots).append(node)
    return roots


@mcp.tool(annotations=SOLO_LEE)
async def read_note(ctx: Ctx, document_id: str) -> dict[str, Any]:
    """Lee una nota completa EN MARKDOWN (encabezados, listas, tablas, código, enlaces).

    Léela antes de opinar o de escribir encima: es la documentación del proyecto, no contexto
    inventado. Devuelve también título, icono y de qué nota cuelga.
    """
    detail = await _prepare(ctx).get_document(document_id)
    doc = detail.get("document", {})
    return {
        "id": str(doc.get("id", document_id)),
        "title": doc.get("title", ""),
        "icon": doc.get("icon"),
        "parent_id": doc.get("parent_id"),
        "markdown": _blocks_to_markdown(detail.get("blocks", [])),
    }


@mcp.tool(annotations=SOLO_LEE)
async def search_notes(ctx: Ctx, space_id: str, query: str) -> list[dict[str, Any]]:
    """Busca por CONTENIDO en las notas del Área; devuelve la nota y la frase donde aparece."""
    return await _prepare(ctx).search_documents(space_id, query)


@mcp.tool(annotations=SOLO_LEE)
async def get_note_backlinks(ctx: Ctx, document_id: str) -> list[dict[str, Any]]:
    """Qué apunta a esta nota: los [[wikilinks]] y menciones que entran desde otras notas."""
    return await _prepare(ctx).document_backlinks(document_id)


@mcp.tool(annotations=CREA)
async def extract_tasks_from_note(
    ctx: Ctx, document_id: str, initiative_id: str
) -> list[dict[str, Any]]:
    """Saca tarjetas de una nota con la IA del backend: nacen PROPUESTAS (gate humano) y
    enlazadas al párrafo del que salieron.
    """
    return await _prepare(ctx).extract_tasks_from_document(document_id, initiative_id)


@mcp.tool(annotations=SOLO_LEE)
async def get_task_notes(ctx: Ctx, task_id: str) -> dict[str, Any]:
    """Qué documentación respalda una tarjeta: su nota de ORIGEN (si nació de una nota, con el
    párrafo exacto) y las notas que la MENCIONAN (chips #tarjeta)."""
    client = _prepare(ctx)
    return {
        "origin": await client.task_origin(task_id),
        "mentioned_in": await client.task_mentions(task_id),
    }


# ── prompts: los ritos del tablero, invocables desde el cliente (FNV-270) ─────
#
# Cómo se trabaja una tarjeta estaba escrito en las skills del repo: solo lo veía un cliente, y solo
# dentro de ese repositorio. Como prompts viajan con la conexión, así que cualquiera que se conecte
# a Foundry los tiene — y el usuario los invoca cuando quiere, en vez de esperar que el agente se
# los sepa.
#
# Son PLANTILLAS, no órdenes: describen el rito y dejan el trabajo al agente que las reciba. No
# repiten lo que las herramientas ya explican en su documentación.


@mcp.prompt(
    title="Trabajar una tarjeta del tablero",
    description="El ciclo completo de una tarjeta: plan visible, avance con evidencia y entrega.",
)
def trabajar_tarjeta(initiative_id: str, task_id: str) -> str:
    """El rito de principio a fin sobre una tarjeta concreta."""
    return f"""Trabaja la tarjeta {task_id} del tablero {initiative_id} siguiendo este ciclo:

0. **Di con qué modelo trabajas.** Llama a `whoami` con `agent_model` (tu modelo exacto): queda
   en todo lo que hagas en la sesión, y así cada aporte dice qué IA lo hizo.
1. **Entérate antes de tocar nada.** Lee la tarjeta con `get_task`, su historial con
   `get_task_comments`, sus criterios con `list_criteria` y sus casos de prueba con
   `list_test_cases`. Lo que ya se decidió no se vuelve a decidir.
2. **Haz visible el plan.** Mueve la tarjeta a la columna de trabajo en curso con
   `transition_task` cuando empieces de verdad. Una tarjeta a la vez.
3. **Construye**, y si por el camino aparece algo que no es de esta tarjeta —un fallo, una deuda—,
   **fíchalo aparte** con `create_task` en vez de arreglarlo en silencio o perderlo.
4. **Deja el avance con `report_progress`**: qué se construyó respondiendo al alcance de la
   tarjeta, qué quedó fuera y por qué, y el commit en `repo_url`. Lo que decidiste por el camino va
   en `record_decision`; lo que hay que cumplir para darla por hecha, en `propose_criterion`.
5. **Deja la evidencia con `report_verification`**: qué probaste y con qué resultado, con números.
   Si falta la prueba de fuego humana, dilo tal cual y usa `parcial` — no `ok`.
6. **Entrega**: mueve la tarjeta a la columna de revisión. **Hecho lo decide una persona**: los
   criterios y las decisiones que propongas esperan su visto bueno, y no te los apruebes tú.

Todo lo que escribas por aquí queda marcado como aporte de IA: no hace falta que lo firmes."""


@mcp.prompt(
    title="Dejar el avance de una tarjeta",
    description="Un avance que se entiende dentro de un mes, con su evidencia y su commit.",
)
def dejar_avance(initiative_id: str, task_id: str) -> str:
    """Cómo se reporta un avance para que sirva de algo."""
    return f"""Deja el avance de la tarjeta {task_id} del tablero {initiative_id}.

Antes de escribir, lee lo que ya hay en su hilo (`get_task_comments`): un avance que repite lo
anterior es ruido.

En `report_progress`:
- **Qué se construyó**, respondiendo al alcance que pedía la tarjeta — no una lista de archivos.
- **Qué quedó fuera y por qué**. Lo que se decidió no hacer vale tanto como lo hecho.
- **El commit** en `repo_url`, y `percent` con lo que de verdad está terminado.
- `checklist` con lo cumplido y lo que falta, para que se lea de un vistazo.

Y después, en `report_verification`, la evidencia **con números**: cuántas pruebas pasaron, qué
comprobaciones se corrieron, qué se probó a mano. Si algo no se ha visto funcionar, el estado es
`parcial` y se dice qué falta por mirar. Un `ok` sin evidencia es peor que no informar."""


@mcp.prompt(
    title="Poner al día un tablero",
    description="Qué está atascado, qué espera decisión humana y qué se puede cerrar.",
)
def repasar_tablero(initiative_id: str) -> str:
    """Una lectura honesta del estado de un tablero."""
    return f"""Repasa el tablero {initiative_id} y cuenta cómo está de verdad.

Léelo con `get_board` (fíjate en la `category` de cada columna: por dónde entra el trabajo y qué lo
da por cerrado) y mira, tarjeta a tarjeta, lo que el tablero ya sabe:

- **Lo que espera a una persona**: tarjetas con criterios o decisiones propuestos sin revisar
  (`gate_pending`), que son las que están bloqueadas sin que nadie lo haya dicho.
- **Lo que dice estar hecho pero no lo ha demostrado**: tarjetas en revisión con casos de prueba
  sin correr (`tests_total` frente a `tests_passed`).
- **Lo que lleva parado**: tarjetas en curso cuyo último aporte es viejo (`get_task_comments`).
- **Lo que está marcado como impedido** (`flagged`) y si alguien lo ha tocado desde entonces.

Devuelve un resumen corto y ordenado por lo que más duele, con la clave de cada tarjeta. No muevas
ninguna ni cierres nada: esto es una lectura, y qué hacer con ella lo decide el equipo."""


# ── qué se expone y cómo se llama (FNV-313) ──────────────────────────────────
#
# Las 76 herramientas pesaban ~75 000 caracteres en `tools/list`, y Codex y los clientes que usan
# `mcpServers` las cargan enteras en cada sesión. Tres palancas, aplicadas en un solo sitio
# (`_finish_tools`) para que la migración al SDK 2.x (FNV-310) solo tenga que tocar aquí:
#  - grupos activables con FOUNDRY_TOOLSETS (como el MCP de GitHub); sin la variable, todos;
#  - sin los `title` que pydantic pone a cada argumento («Initiative Id»): repiten el nombre;
#  - sin `outputSchema`: los nuestros son genéricos (`additionalProperties: true`), no le dicen
#    nada al cliente y obligan a mandar cada resultado dos veces (texto + structuredContent).
# Y `title` legible en todas, que es lo que enseñan los clientes en vez de `set_task_hierarchy`.

# Siempre activas, se elija el grupo que se elija: sin ellas no hay sesión.
ALWAYS_ON = ("login", "logout", "whoami")
TOOLSETS: dict[str, tuple[str, ...]] = {
    # Trabajar tarjetas: leer el tablero, crearlas y moverlas, el hilo, los gates y los vínculos.
    "tableros": (
        "list_spaces", "list_boards", "list_all_boards", "get_board", "get_task", "search_tasks",
        "list_board_presets", "create_board", "move_board_to_space", "create_task", "create_epic",
        "list_epics", "edit_task", "move_task", "transition_task", "delete_task",
        "set_task_hierarchy", "set_task_details", "set_task_labels", "link_tasks", "unlink_tasks",
        "get_task_links", "get_task_history", "review_task_proposal", "add_task_comment",
        "add_task_entry", "get_task_comments", "report_progress", "report_verification",
        "flag_blocker", "propose_criterion", "list_criteria", "review_criterion",
        "record_decision", "list_decisions", "review_decision", "link_repo", "unlink_repo",
        "get_repo_links",
    ),
    "qa": ("add_test_case", "list_test_cases", "mark_test_case", "list_test_executions"),
    "sprints": ("create_sprint", "start_sprint", "complete_sprint", "assign_task_to_sprint", "list_sprints"),
    "columnas": (
        "add_column", "rename_column", "set_column_category", "set_column_color",
        "reorder_columns", "delete_column",
    ),
    "adjuntos": (
        "upload_task_attachment", "list_task_attachments", "read_task_attachment",
        "delete_task_attachment",
    ),
    "notas": (
        "list_notes", "read_note", "search_notes", "create_note", "write_note",
        "create_note_collection", "move_note", "rename_note", "delete_note", "link_notes",
        "get_note_backlinks", "link_note_to_task", "get_task_notes", "extract_tasks_from_note",
        "create_tasks_from_seeds",
    ),
}  # fmt: skip
TITLES: dict[str, str] = {
    "login": "Iniciar sesión", "logout": "Cerrar sesión", "whoami": "Quién soy (y con qué modelo)",
    "list_spaces": "Listar Áreas", "list_boards": "Listar tableros", "list_all_boards": "Todos los tableros",
    "get_board": "Ver tablero", "get_task": "Ver tarjeta", "search_tasks": "Buscar tarjetas",
    "list_board_presets": "Plantillas de columnas", "create_board": "Crear tablero",
    "move_board_to_space": "Mover tablero a un Área", "create_task": "Crear tarjeta",
    "create_epic": "Crear épica", "list_epics": "Listar épicas", "edit_task": "Editar tarjeta",
    "move_task": "Mover tarjeta", "transition_task": "Cambiar estado de tarjeta",
    "delete_task": "Borrar tarjeta", "set_task_hierarchy": "Cambiar tipo y padre",
    "set_task_details": "Estimación y fechas", "set_task_labels": "Poner etiquetas",
    "link_tasks": "Vincular tarjetas", "unlink_tasks": "Desvincular tarjetas",
    "get_task_links": "Ver vínculos", "get_task_history": "Ver historial de cambios",
    "review_task_proposal": "Aceptar o descartar tarjeta propuesta",
    "add_task_comment": "Comentar tarjeta", "add_task_entry": "Aporte tipado en el hilo",
    "get_task_comments": "Ver hilo de la tarjeta", "report_progress": "Dejar avance",
    "report_verification": "Dejar verificación", "flag_blocker": "Marcar bloqueo",
    "propose_criterion": "Proponer criterio", "list_criteria": "Ver criterios",
    "review_criterion": "Aceptar o descartar criterio", "record_decision": "Registrar decisión",
    "list_decisions": "Ver decisiones", "review_decision": "Aceptar o descartar decisión",
    "link_repo": "Enlazar commit o PR", "unlink_repo": "Quitar enlace de repo",
    "get_repo_links": "Ver enlaces de repo", "add_test_case": "Añadir caso de prueba",
    "list_test_cases": "Ver casos de prueba", "mark_test_case": "Marcar caso de prueba",
    "list_test_executions": "Bitácora de pruebas", "create_sprint": "Crear sprint",
    "start_sprint": "Arrancar sprint", "complete_sprint": "Cerrar sprint",
    "assign_task_to_sprint": "Meter tarjeta en sprint", "list_sprints": "Listar sprints",
    "add_column": "Añadir columna", "rename_column": "Renombrar columna",
    "set_column_category": "Cambiar tipo de columna", "set_column_color": "Color de columna",
    "reorder_columns": "Reordenar columnas", "delete_column": "Borrar columna",
    "upload_task_attachment": "Adjuntar archivo", "list_task_attachments": "Ver adjuntos",
    "read_task_attachment": "Leer adjunto", "delete_task_attachment": "Borrar adjunto",
    "list_notes": "Listar notas", "read_note": "Leer nota", "search_notes": "Buscar notas",
    "create_note": "Crear nota", "write_note": "Reescribir nota",
    "create_note_collection": "Crear colección de notas", "move_note": "Mover nota",
    "rename_note": "Renombrar nota", "delete_note": "Borrar nota", "link_notes": "Enlazar notas",
    "get_note_backlinks": "Quién enlaza esta nota", "link_note_to_task": "Atar nota a tarjeta",
    "get_task_notes": "Notas que mencionan la tarjeta",
    "extract_tasks_from_note": "Sacar tarjetas de una nota",
    "create_tasks_from_seeds": "Convertir semillas en tarjetas",
}  # fmt: skip


def _selected_tools(toolsets: tuple[str, ...]) -> set[str]:
    """Las herramientas de los grupos pedidos (más las de sesión). Sin grupos, o con `todos`, todas.

    Un nombre de grupo desconocido se avisa por stderr (stdout es el canal del protocolo) y se
    ignora: equivocarse en un nombre no debería dejar al agente sin herramientas.
    """
    if not toolsets or "todos" in toolsets or "all" in toolsets:
        return set(ALWAYS_ON).union(*TOOLSETS.values())
    unknown = [t for t in toolsets if t not in TOOLSETS]
    if unknown:
        print(
            f"foundry-boards: FOUNDRY_TOOLSETS trae grupos que no existen ({', '.join(unknown)}); "
            f"los que hay son {', '.join(TOOLSETS)}.",
            file=sys.stderr,
        )
    return set(ALWAYS_ON).union(*(TOOLSETS[t] for t in toolsets if t in TOOLSETS))


def _strip_schema_titles(node: Any) -> None:
    """Quita los `title` que pydantic genera para cada argumento y modelo: repiten el nombre."""
    if isinstance(node, dict):
        node.pop("title", None)
        for value in node.get("properties", {}).values():
            _strip_schema_titles(value)
        for key in ("items", "anyOf", "oneOf", "allOf", "$defs"):
            child = node.get(key)
            for sub in child.values() if isinstance(child, dict) else child or []:
                _strip_schema_titles(sub)


def _finish_tools(server: MCPServer, toolsets: tuple[str, ...]) -> None:
    keep = _selected_tools(toolsets)
    for tool in list(server._tool_manager.list_tools()):
        if tool.name not in keep:
            server.remove_tool(tool.name)
            continue
        tool.title = TITLES.get(tool.name, tool.title)
        _strip_schema_titles(tool.parameters)
        tool.fn_metadata.output_schema = None  # resultado sin structuredContent duplicado


_finish_tools(mcp, load_config().toolsets)


def main() -> None:
    """Punto de entrada: arranca el servidor MCP por stdio (transporte que usa Claude Code)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
