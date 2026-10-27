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
import unicodedata
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.session import ServerSession
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from foundry_mcp.config import load_config
from foundry_mcp.foundry import FoundryClient, FoundryError, set_agent_identity

# El contexto de cada llamada, con los parámetros que pide mypy estricto: la sesión del servidor y
# un lifespan que no usamos. FastMCP lo inyecta reconociendo esta anotación, así que cambiarla puede
# convertir `ctx` en un argumento de todas las herramientas: lo vigila tests/test_context_injection.py.
Ctx = Context[ServerSession, None]

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
SOLO_LEE = ToolAnnotations(readOnlyHint=True)
CREA = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
# Deja un valor concreto: repetirla con los mismos datos no cambia nada más (se puede reintentar).
ACTUALIZA = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
BORRA = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
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

mcp = FastMCP(
    "foundry-boards",
    instructions=(
        "Administra los tableros de Foundry (vivero colaborativo de ideas → iniciativas) con un "
        "modelo de gestión de trabajo ágil. Úsalo para ver/crear/reorganizar tableros y sus "
        "tarjetas (work items), asignar responsables, transicionar por estados, gestionar "
        "jerarquía (épica → historia/tarea/bug/incidencia → subtarea; una historia agrupa "
        "tareas y bugs), etiquetas, vínculos, sprints (Scrum) y leer el "
        "historial estructurado. Ubica ids con `list_spaces`/`list_boards`; `get_board` trae "
        "columnas y tarjetas. La autenticación es interactiva: si no hay sesión, la primera acción "
        "abrirá un diálogo de inicio de sesión (o usa `login`). "
        f"Cada tablero tiene SUS columnas ({COLUMN_SETS_HINT}): míralas con `get_board` en vez de "
        "suponerlas, y fíjate en la `category` de cada una (`todo`/`in_progress`/`done`), que es lo "
        "único estable — el nombre lo cambia quien quiera. Al crear una tarjeta sin `column`, nace "
        "en la columna de entrada del tablero. Al registrar un comentario, avance, bloqueo o "
        "verificación, declara "
        "`agent_model` si conoces el modelo actual; si no, el servidor usa su configuración. "
        "El backend valida permisos por rol."
    ),
)


class LoginForm(BaseModel):
    """Formulario que Claude Code muestra al usuario para iniciar sesión en Foundry."""

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


class ConfirmForm(BaseModel):
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


async def _confirmar_borrado(ctx: Ctx, que_se_pierde: str) -> None:
    """Pregunta antes de borrar, con el dato delante — no «¿seguro?», sino qué desaparece.

    Si el cliente MCP **no sabe preguntar** (la elicitation es opcional en el protocolo), no se
    queda colgado ni se niega a trabajar: sigue adelante. El gate de verdad son los permisos del
    backend y la aprobación de la propia herramienta en el cliente; esto es la cortesía de avisar
    a quien va a perder algo. Cancelar, en cambio, sí detiene la operación.
    """
    try:
        pregunta = f"{que_se_pierde} ¿{_pronombre(que_se_pierde).capitalize()} borro?"
        result = await ctx.elicit(message=pregunta, schema=ConfirmForm)
    except Exception:
        return  # cliente sin elicitation: no hay a quién preguntar
    if result.action != "accept" or result.data is None or not result.data.confirmar:
        raise FoundryError("Borrado cancelado: no se ha tocado nada.")


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
    # Interactivo: Claude Code muestra el diálogo de login.
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
        raise FoundryError("Autenticación cancelada por el usuario.")
    data = result.data
    if data.token.strip():
        token = data.token.strip()
    elif data.email.strip() and data.password:
        token = await client.obtain_token(data.email.strip(), data.password)
    else:
        raise FoundryError("Faltan datos: email + contraseña, o un token.")
    if data.remember:
        _keyring_set(token)
    return token


def _prepare(ctx: Ctx, agent_model: str = "") -> FoundryClient:
    """Deja el cliente listo: fija `reauth` con el ctx actual y siembra el token del keyring."""
    client = _get_client()
    client.reauth = lambda: _reauth(ctx)
    if client.token is None:
        client.token = _keyring_get()  # siembra optimista; si expiró, el 401 dispara reauth
    set_agent_identity(_agent_source(ctx), _resolve_agent_model(agent_model))
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

    params = getattr(ctx.session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    return _resolve_agent_source(
        load_config().agent_source,
        getattr(info, "name", None),
        getattr(info, "title", None),
    )


def _resolve_agent_model(explicit: str = "") -> str | None:
    """Modelo declarado por llamada → configuración del proceso → desconocido.

    MCP no incluye el modelo en ``clientInfo``. El override por llamada permite que un mismo
    cliente cambie de modelo sin reiniciar el servidor.
    """

    value = explicit.strip() or load_config().agent_model.strip()
    return value[:160] or None


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
        agent_model=_resolve_agent_model(agent_model),
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
@mcp.tool(annotations=ACTUALIZA)
async def login(ctx: Ctx) -> str:
    """Inicia sesión en Foundry (abre un diálogo para email + contraseña, o un token).

    Normalmente no hace falta llamarla: la primera acción sobre un tablero ya pide la sesión.
    Útil para iniciar sesión por adelantado o cambiar de cuenta.
    """
    client = _prepare(ctx)
    client.token = None  # fuerza el diálogo de login
    client.token = await _reauth(ctx)  # lo mismo que el `reauth` que acaba de fijar `_prepare`
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
async def whoami(ctx: Ctx) -> dict[str, Any]:
    """Cuenta con la que actúa el MCP (id, email, nombre). Verifica la sesión/credenciales."""
    return await _prepare(ctx).me()


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


@mcp.tool(annotations=SOLO_LEE)
async def get_board(ctx: Ctx, initiative_id: str) -> dict[str, Any]:
    """Trae un tablero completo: la ficha de la iniciativa + columnas, cada una con sus tarjetas.

    Cada columna: id, name, order, `category` y `color`. **La categoría es lo que hay que mirar**
    (`todo` = por donde entra el trabajo · `in_progress` = en curso · `done` = cierra la tarjeta):
    es lo único estable de una columna, porque el nombre lo pone el equipo y lo cambia cuando
    quiere. El `color` es apariencia; puede ser el propio de la columna o venir vacío, y entonces
    la pantalla usa el de su categoría. Ningún tablero comparte nomenclatura con otro.

    Cada tarjeta: id, title, description, priority, kind, origin, status_agent, assignee_id, order,
    comment_count (nº de notas de historial). `status_agent="proposed"` marca tarjetas propuestas
    por IA o proyectadas de una nota. Usa `get_task_comments` para leer el historial de una tarjeta.
    """
    return await _prepare(ctx).get_initiative(initiative_id)


# ── tools: tableros ──────────────────────────────────────────────────────────
@mcp.tool(annotations=SOLO_LEE)
async def list_board_presets(ctx: Ctx) -> list[dict[str, Any]]:
    """Las **plantillas de columnas** entre las que se elige al crear un tablero (FNV-257).

    Cada una trae `key` (lo que se pasa a `create_board`), `label`, `template` (`kanban` o `scrum`;
    la de Scrum enciende los sprints), `is_default` —la que se siembra si no se elige ninguna— y sus
    `columns` con la categoría de cada una. **Léelas antes de crear un tablero**: las claves no se
    adivinan, y elegir por lo que trae cada plantilla evita tener que rehacer las columnas después.
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
    """Crea un tablero Kanban nuevo, sin forjar la 'Gran Idea'.

    - sin `space_id`: tablero **personal** (solo la cuenta lo ve).
    - con `space_id`: tablero dentro de un **Área** (lo ven sus miembros; requiere colaborador/dueño).

    **Con qué columnas nace** (FNV-256):
    - sin nada: las de la plantilla por omisión del backend (el flujo en inglés);
    - `preset`: la `key` de una plantilla de `list_board_presets` (p. ej. la del flujo en español);
    - `columns`: las columnas **a medida**, `[{"name": "Ideas", "category": "todo"}, …]` — entre 2 y
      10, con `category` en `todo`/`in_progress`/`done`, y opcionalmente `color` (`#rrggbb`). Hace
      falta al menos una de entrada (`todo`) y una de cierre (`done`), sin nombres repetidos.

    Con columnas a medida conviene mandar TAMBIÉN el `preset` del que se parte: es quien fija la
    plantilla del tablero (`kanban`/`scrum`) y, con ella, si hay sprints. Las reglas las hace
    cumplir el servidor: si algo no cuadra, contesta con el motivo y no crea nada a medias.
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
    """Añade una columna al final del tablero y devuelve cómo quedan todas.

    `category`: `todo` (por donde entra el trabajo) · `in_progress` · `done` (cierra la tarjeta).
    `color`: `#rrggbb` opcional; sin él, la columna hereda el color de su categoría, que es lo
    normal. El servidor aplica el tope de diez columnas y rechaza un nombre que ya exista.
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
    """Cambia el TIPO de una columna: `todo` · `in_progress` · `done`.

    Es lo que decide de verdad el comportamiento —dónde entra una tarjeta nueva y qué la da por
    cerrada—, así que no es un adorno. El tablero debe conservar al menos una de entrada y una de
    cierre: si el cambio lo dejaría sin una de las dos, el servidor lo rechaza.
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
    """Reordena las columnas del tablero: pasa **todas**, por nombre o por id, en el orden que quedan.

    Es la lista completa, no un movimiento suelto: así el resultado es el que se ve, sin depender de
    dónde estaba cada una. Si falta o sobra alguna, el servidor lo rechaza en vez de adivinar.
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


@mcp.tool(
    annotations=ToolAnnotations(
        title="Borrar una columna del tablero", destructiveHint=True, idempotentHint=False
    )
)
async def delete_column(
    ctx: Ctx, initiative_id: str, column: str, reassign_to: str = ""
) -> dict[str, Any]:
    """Borra una columna del tablero. **Si tiene tarjetas, hay que decir a dónde van** con
    `reassign_to` (otra columna, por nombre o id): se mudan con el estado de la columna destino y
    el traslado queda en el historial de cada tarjeta.

    Se comprueba antes de llamar cuántas tarjetas hay dentro, para poder decirlo en vez de fallar
    con un error escueto. El tablero no puede quedarse sin columna de entrada o de cierre; eso lo
    impide el servidor.
    """
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
    await _confirmar_borrado(ctx, f'La columna «{objetivo["name"]}»{mudanza}.')
    await client.delete_column(initiative_id, column_id, destino_id)
    return {
        "deleted": objetivo["name"],
        "moved_tasks": dentro,
        "moved_to": next(
            (c["name"] for c in columns if str(c["id"]) == destino_id), None
        ),
        "columns": await _board_columns(client, initiative_id),
    }


# ── tools: tarjetas ──────────────────────────────────────────────────────────
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
    """Crea una tarjeta (work item) en un tablero.

    `column`: nombre (tolerante a acentos/mayúsculas) o id de columna; la tarjeta toma el estado del
    workflow de esa columna. Los nombres NO son iguales en todos los tableros, así que **déjalo
    vacío** y nacerá en la columna de entrada — la primera de categoría `todo`, se llame "Backlog",
    "To Do", "Ideas" o como el equipo la haya llamado—; pásalo solo para elegir otra, y mira
    `get_board` si dudas.
    `priority`: highest|high|medium|low|lowest (se aceptan también baja|media|alta). `kind`:
    tarea|bug|incidencia.
    `issue_type`: epic|historia|tarea|bug|incidencia|subtarea (jerarquía ágil). `parent_id`: tarjeta
    padre. Quién cuelga de quién: una **épica** agrupa historias, tareas, bugs e incidencias; una
    **historia** agrupa tareas, bugs, incidencias y subtareas; una tarea/bug/incidencia agrupa
    subtareas; una subtarea es hoja. Una subtarea **exige** padre; una épica no puede tenerlo.
    `story_points`: estimación.
    `agent_model`: override opcional del modelo para esta creación; vacío usa la configuración.
    `assignee` (opcional): nombre/email de un miembro (tableros de Área) o id de usuario.
    Nace al final de la columna. Requiere rol colaborador/dueño.

    Queda marcada como creada por la IA (procedencia `ia`, `origin=agent`), pero **oficial** —no
    pasa por el gate de propuesta (FNV-109): la procedencia es información, no fricción; el control
    lo tiene la persona que te pide la tarjeta—. Para trabajo que SÍ deba revisar una persona antes
    de darlo por bueno, usa `propose_criterion`/`record_decision`, que sí nacen a la espera.
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
        "agent_model": _resolve_agent_model(agent_model),
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
    """Edita una tarjeta (**parcial**: solo cambian los campos que pases).

    `priority`: highest|high|medium|low|lowest (o baja|media|alta). `kind`: tarea|bug|incidencia.
    `assignee`: nombre/email/id de un miembro; pasa `assignee=""` (cadena vacía) para **quitar** el
    responsable. Para tipo/padre usa `set_task_hierarchy`; para estimación/fechas, `set_task_details`.
    `agent_model`: override opcional del modelo para esta edición; vacío usa la configuración.
    Requiere colaborador/dueño.
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
        "agent_model": _resolve_agent_model(agent_model),
    }
    if assignee is None:
        body["assignee_id"] = task.get("assignee_id")
    elif assignee == "":
        body["assignee_id"] = None
    else:
        body["assignee_id"] = await _resolve_assignee(detail.get("space_id"), assignee)
    return await client.edit_task(initiative_id, task_id, body)


@mcp.tool(annotations=BORRA)
async def delete_task(ctx: Ctx, initiative_id: str, task_id: str) -> dict[str, Any]:
    """Elimina una tarjeta del tablero (irreversible). Requiere rol colaborador/dueño.

    Antes de borrar mira **qué se va con ella** —su historial, sus adjuntos y sus criterios— y lo
    pregunta, porque una tarjeta vieja puede llevar dentro el porqué de media decisión. Si el
    cliente no sabe preguntar, sigue adelante y lo dice en la respuesta.
    """
    client = _prepare(ctx)
    detail = await client.get_initiative(initiative_id)
    task = _find_task(detail, task_id)
    if task is None:
        raise FoundryError(f"La tarjeta {task_id} no está en este tablero.")
    perdido = await _lo_que_se_pierde(client, initiative_id, task)
    await _confirmar_borrado(ctx, perdido)
    await client.delete_task(initiative_id, task_id)
    return {"status": "deleted", "task_id": task_id, "era": task.get("title"), "perdido": perdido}


@mcp.tool(annotations=CREA)
async def create_tasks_from_seeds(
    ctx: Ctx, initiative_id: str, seed_ids: list[str], agent_model: str = ""
) -> list[dict[str, Any]]:
    """Vivero→Tablero: manda semillas (ideas) del Vivero como tarjetas **propuestas** a la columna
    de entrada del tablero, se llame como se llame.

    Nacen como propuesta (`status_agent="proposed"`, `origin="agent"`); se aceptan/rechazan desde
    la app. `seed_ids` son ids de semillas del mismo espacio del tablero. Requiere colaborador/dueño.
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
    """Agrega una **nota** libre al historial de una tarjeta (cómo se implementó o avanzó).

    Equivale a `add_task_entry` con `kind="nota"`. Para registrar un aporte estructurado (un avance,
    un criterio de aceptación, una decisión, un bloqueo, una verificación o una pregunta) usa mejor
    `add_task_entry`: queda tipado y distinguible en el hilo, no aplanado en texto. Queda marcado
    como nota del **agente** (procedencia `ia`). `agent_model` permite declarar el modelo concreto;
    si se omite usa `FOUNDRY_AGENT_MODEL`. Append-only; requiere rol colaborador/dueño.
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
    """Registra un **aporte tipado** del agente en el hilo de una tarjeta (ADR-0017), en lugar de un
    comentario plano, para que su output quede estructurado y trazable.

    `kind` (tipo de aporte): `avance` (progreso), `criterio` (de aceptación), `decision`,
    `bloqueo` (marca la tarjeta con la bandera de impedimento), `verificacion` (evidencia/prueba),
    `pregunta` o `nota` (libre). `meta` es JSON libre para adjuntar contexto legible por máquina,
    p. ej. `{"commit": "abc123", "ci": "green"}`.

    Los tres casos frecuentes tienen tool propia, más cómoda y con los datos ya estructurados:
    **`report_progress`** (avance), **`flag_blocker`** (bloqueo) y **`report_verification`**
    (verificación). Usa este tool general para los demás tipos.

    Queda marcado como aporte del **agente** (procedencia `ia`; la API guarda con qué cuenta se
    escribió). `agent_model` sobreescribe la configuración para esta entrada. Append-only;
    requiere rol colaborador/dueño.
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


async def _repo_meta(
    client: FoundryClient, initiative_id: str, task_id: str, repo_url: str, kind: str = "commit"
) -> dict[str, Any]:
    """Ata la tarjeta al código (ADR-0016) y devuelve el trozo de `meta` que hace visible ese enlace
    **dentro** de la propia entrada del hilo, no solo en la lista de repos de la tarjeta."""
    if not repo_url:
        return {}
    await client.link_repo(initiative_id, task_id, repo_url, kind, "")
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
    """Reporta un **avance** en una tarjeta: qué hiciste, cuánto queda y en qué código se ve.

    Úsalo cada vez que completes una parte del trabajo, para que el progreso quede tipado en el
    hilo (`avance`) en vez de aplanado en un comentario suelto. Equivale a
    `add_task_entry(kind="avance")` pero con los datos del avance estructurados:

    - `percent`: 0–100, cuánto de la tarjeta está hecho.
    - `checklist`: pasos en markdown — `"[x] hecho"` / `"[ ] pendiente"`.
    - `repo_url`: el commit/PR donde se materializó. Queda **vinculado a la tarjeta** (ADR-0016) y
      se muestra como enlace dentro de la entrada: es la traza IA→código.

    Queda con procedencia `ia`. Append-only; requiere rol colaborador/dueño.
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
        agent_model=_resolve_agent_model(agent_model),
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
    """Marca que una tarjeta está **bloqueada** y por qué (aporte `bloqueo`).

    Además de dejar el motivo tipado en el hilo, **levanta la bandera de impedimento** de la
    tarjeta, que se ve en el tablero. Úsalo en cuanto algo te impida seguir —una credencial que
    falta, una decisión humana pendiente, un test que no pasa— en lugar de decirlo solo en el chat,
    donde la persona no lo verá si la sesión se cierra.

    `repo_url` (opcional): el commit/PR donde se ve el problema. Procedencia `ia`; colaborador/dueño.
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
        agent_model=_resolve_agent_model(agent_model),
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
    """Registra la **verificación** de una tarjeta: la evidencia de que lo hecho funciona (o no).

    Aporte `verificacion`. `status`: ok|fallo|parcial. `detail`: qué probaste y con qué resultado
    (tests, build, prueba manual). `url`: la evidencia (CI, PR, captura); se muestra como enlace
    dentro de la entrada.

    Úsalo después de correr los tests o de probar el cambio, para que "está verificado" sea un dato
    del tablero —con su evidencia— y no una afirmación en el chat. Procedencia `ia`.
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
    """Propone un **criterio de aceptación** para una tarjeta (ADR-0017 F2): "para darla por hecha
    debe cumplirse Z".

    Nace como **propuesta** (`proposed`, procedencia `ia`): una persona lo acepta o descarta en el
    tablero (gate humano) antes de que cuente como criterio oficial. Úsalo cuando, al trabajar una
    tarjeta, identifiques una condición verificable de "hecho". Requiere rol colaborador/dueño.
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
    """Registra una **decisión de diseño** de una tarjeta (ADR-0017 F2), estilo ADR-lite: "elegí A
    por B".

    Nace como **propuesta** (`proposed`, procedencia `ia`); una persona la acepta/descarta (gate
    humano). Si pasas `repo_url` (un commit/PR/repositorio, ADR-0016), la decisión queda **atada a
    ese código** para la trazabilidad IA→commit — *"Claude decidió X, materializado en `abc123`"*.
    Requiere rol colaborador/dueño.
    """
    client = _prepare(ctx, agent_model)
    repo_link_id: str | None = None
    if repo_url:
        # Reusa el vínculo de repo (ADR-0016): lo crea (idempotente por URL) y toma su id.
        links = await client.link_repo(initiative_id, task_id, repo_url, "commit", "")
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
    """Lee los **criterios de aceptación** de una tarjeta con el estado de su gate (ADR-0017 F2):
    `status` = `proposed` (propuesto por la IA, espera decisión humana) · `accepted` (oficial) ·
    `rejected` (descartado). Trae también `source` (humano|ia|colaborativo) y `checked`.

    Úsalo para **cerrar el círculo**: tras proponer con `propose_criterion`, una persona decide en
    el tablero **o** con `review_criterion`; al releer aquí ves qué se aceptó/descartó (por si tu
    sesión se cerró y la decisión se tomó desde la app). No muta nada.
    """
    return await _prepare(ctx).list_task_criteria(initiative_id, task_id)


@mcp.tool(annotations=ACTUALIZA)
async def review_criterion(
    ctx: Ctx, initiative_id: str, task_id: str, criterion_id: str, accept: bool
) -> dict[str, Any]:
    """**Gate humano** de un criterio de aceptación (ADR-0017 F2): `accept=True` lo vuelve oficial;
    `accept=False` lo descarta. El `criterion_id` sale de `list_criteria`.

    Es la **decisión de una persona**, la misma que el botón del tablero: úsalo solo cuando alguien
    te lo pida explícitamente. **No auto-apruebes** criterios que tú mismo propusiste. Requiere rol
    colaborador/dueño.
    """
    return await _prepare(ctx).review_task_criterion(
        initiative_id, task_id, criterion_id, accept=accept
    )


@mcp.tool(annotations=SOLO_LEE)
async def list_decisions(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lee las **decisiones de diseño** de una tarjeta con el estado de su gate (ADR-0017 F2):
    `status` = `proposed` · `accepted` · `rejected`; más `source` (humano|ia|colaborativo) y el
    `repo_link_id` al que atan (ADR-0016), si lo hay.

    Complemento de lectura de `record_decision`: al releer aquí ves qué decisiones se oficializaron
    o descartaron (incluso si se decidió desde la app mientras no estabas). No muta nada.
    """
    return await _prepare(ctx).list_task_decisions(initiative_id, task_id)


@mcp.tool(annotations=ACTUALIZA)
async def review_decision(
    ctx: Ctx, initiative_id: str, task_id: str, decision_id: str, accept: bool
) -> dict[str, Any]:
    """**Gate humano** de una decisión de diseño (ADR-0017 F2): `accept=True` la vuelve oficial;
    `accept=False` la descarta. El `decision_id` sale de `list_decisions`.

    Es la **decisión de una persona** (equivale al botón del tablero): úsalo solo a pedido explícito
    y **no auto-apruebes** decisiones que tú mismo registraste. Requiere rol colaborador/dueño.
    """
    return await _prepare(ctx).review_task_decision(
        initiative_id, task_id, decision_id, accept=accept
    )


@mcp.tool(annotations=ACTUALIZA)
async def review_task_proposal(
    ctx: Ctx, initiative_id: str, task_id: str, accept: bool
) -> dict[str, Any]:
    """**Gate humano** de una **tarjeta** propuesta por la IA (`status_agent="proposed"`, ADR-0017
    F2): `accept=True` la vuelve una tarjeta oficial del tablero; `accept=False` la marca `rejected`
    (el front la oculta, pero no se borra: conserva la traza).

    Su estado se lee en `get_board` (campo `status_agent` de cada tarjeta). Es la **decisión de una
    persona**, la misma que el botón del tablero: úsalo solo a pedido explícito y **no auto-apruebes**
    tarjetas que tú mismo propusiste. Requiere rol colaborador/dueño.
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
    """Deja un **caso de prueba** en una historia (FNV-127): un escenario de QA a validar en la fase
    de revisión antes de dar por terminada la historia. Distinto de un criterio de aceptación (ese es
    la definición de hecho, con gate): aquí NO hay gate — el caso nace `pending` y se valida
    corriéndolo (`mark_test_case`).

    Solo cuelga de un **entregable**: una historia (lo normal), o una tarea/bug/incidencia suelta. El
    backend **rechaza** una épica (demasiado gruesa) o una subtarea. Apunta a 1–4 casos por historia.
    `title` es el escenario; `steps`, cómo ejecutarlo; `expected`, qué debería pasar. Queda marcado
    de la IA (procedencia `ia`). Requiere rol colaborador/dueño.
    """
    return await _prepare(ctx, agent_model).add_test_case(
        initiative_id, task_id, title, steps, expected
    )


@mcp.tool(annotations=SOLO_LEE)
async def list_test_cases(
    ctx: Ctx, initiative_id: str, task_id: str
) -> list[dict[str, Any]]:
    """Lee los **casos de prueba** de una tarjeta (FNV-127) con su estado (`pending` sin validar ·
    `passed` · `failed`), sus pasos, el resultado esperado y la procedencia. No muta nada.

    Cuando todos están `passed`, la historia está «lista para cerrar» (una persona la mueve a Hecho:
    no se cierra sola). Úsalo para saber qué falta validar antes de dar por terminada una historia.
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
    """Registra el **resultado de correr** un caso de prueba (FNV-127): `status` = `passed` (pasó) ·
    `failed` (falló) · `pending` (aún sin validar). El `test_case_id` sale de `list_test_cases`.

    Es la validación de QA. Si acabas de ejecutar el escenario (p. ej. corriste la prueba), registra
    aquí el veredicto real; si el equipo valida a mano en el tablero, no hace falta. Cada marca deja
    además una corrida en la **bitácora** (FNV-159), aunque el veredicto repita el anterior: mírala
    con `list_test_executions`. Queda en el historial. Requiere rol colaborador/dueño.
    """
    return await _prepare(ctx, agent_model).mark_test_case(
        initiative_id, task_id, test_case_id, status
    )


@mcp.tool(annotations=SOLO_LEE)
async def list_test_executions(
    ctx: Ctx, initiative_id: str, task_id: str, test_case_id: str = ""
) -> list[dict[str, Any]]:
    """Lee la **bitácora de ejecuciones** de los casos de prueba (FNV-159): cada marca deja una
    corrida —resultado (`passed`/`failed`/`pending`), quién (humano o agente + modelo), en qué etapa
    (columna) y cuándo—, y se conserva **aunque el veredicto repita** el anterior. Sin `test_case_id`
    trae toda la tarjeta; con él, las de un solo caso. Es el registro que sobrevive a las rondas de
    QA y a los cambios de etapa, complementario al estado actual de `list_test_cases`. No muta nada.
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
    """Historial **estructurado** de una tarjeta (ADR-0014, E4): qué campo cambió, de qué valor a
    qué valor, quién y cuándo (orden cronológico).

    Distinto de `get_task_comments` (notas de texto): aquí cada entrada es un cambio de campo
    (`created`, `column_id`, `status_id`, `priority`, `assignee_id`, `sprint_id`, `label`, `link`,
    …). Los valores van en crudo (ids/enums). Útil para leer "quién cambió qué y cuándo".
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
    """Transiciona una tarjeta a otro **estado** del workflow moviéndola a su columna (ADR-0014).

    Cada columna del tablero corresponde a un estado, así que `status` es el nombre (o el id) de la
    columna destino **de ESE tablero**: no hay una lista universal de estados — míralos con
    `get_board`, que además dice la categoría de cada columna (por dónde entra el trabajo y cuál lo
    cierra). Equivale a `move_task` pero pensado como cambio de estado; deja rastro en el historial
    (status_id/column_id). `order`: 0=arriba.
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
    """Fija el tipo y el padre de una tarjeta (jerarquía ágil, ADR-0014 + FNV-24).

    `issue_type`: epic|historia|tarea|bug|incidencia|subtarea. `parent_id`: tarjeta padre. Quién
    cuelga de quién: una **épica** agrupa historias, tareas, bugs e incidencias; una **historia**
    agrupa tareas, bugs, incidencias y subtareas; una tarea/bug/incidencia agrupa subtareas; una
    subtarea es hoja. Una subtarea exige padre; una épica es raíz y no puede tenerlo; sin ciclos.
    Es un `set` explícito. Requiere colaborador/dueño.
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
    """Actualiza (PATCH) los campos ricos de una tarjeta: estimación, fechas e impedimento.

    **Solo cambia los campos que pases; los omitidos se conservan** (lee el estado actual y hace
    merge). `story_points`: estimación (número ≥0). `start_date`/`due_date`: fechas ISO
    (YYYY-MM-DD); pasa `""` para **borrar** una fecha. `flagged`: marca/quita el impedimento.
    Requiere colaborador/dueño.
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
    la óptica de `task_id`). Devuelve los vínculos de `task_id`. Requiere colaborador/dueño.
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
    """Quita el/los vínculo(s) entre `task_id` y `target_id` (resuelve el id del vínculo solo).

    Sin `link_type` borra todos los vínculos hacia `target_id`; con él (blocks|is_blocked_by|
    relates_to, desde la óptica de `task_id`) borra solo ese. Devuelve los vínculos restantes.
    Requiere colaborador/dueño.
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
@mcp.tool(annotations=CREA)
async def link_repo(
    ctx: Ctx,
    initiative_id: str,
    task_id: str,
    url: str,
    kind: str = "repository",
    title: str = "",
    agent_model: str = "",
) -> list[dict[str, Any]]:
    """Vincula una tarjeta a un recurso de repositorio por URL. `kind`: repository|branch|
    pull_request|commit. `title` es una etiqueta legible opcional (p. ej. "PR #12"). Pensado para
    que Claude Code asocie el repo/branch/PR donde trabaja a la tarjeta. Devuelve los vínculos de
    la tarjeta. Requiere colaborador/dueño.
    """
    k = _validate(kind, REPO_KINDS, "kind")
    return await _prepare(ctx, agent_model).link_repo(initiative_id, task_id, url, k, title)


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
    vínculo solo). Devuelve los vínculos restantes. Requiere colaborador/dueño.
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
    """Lee un adjunto de una tarjeta por su id (el id sale de `list_task_attachments`).

    - **Imagen** (png/jpg/webp/…): se devuelve como imagen para que Claude la **vea** — útil para
      mockups, capturas o diagramas adjuntos a la tarjeta.
    - **Texto** (text/*, json, csv, yaml, sql…): se devuelve el contenido como texto.
    - **Otro binario** (pdf, zip, …): se devuelven metadatos + un aviso (el MCP no puede
      renderizarlo aquí; descárgalo desde la app). Límite del backend: 10 MB.
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
    """Sube un archivo a una tarjeta: el informe, el diagrama o la captura que acabas de producir.

    Tres formas de decir QUÉ se sube, y hay que usar exactamente una:
    - `path`: la ruta del archivo en el equipo donde corre el agente. El nombre sale de la ruta
      salvo que pases `filename`.
    - `text`: el contenido tal cual (un markdown, un csv, un log). **Exige `filename`**.
    - `base64_data`: bytes en base64, para lo binario que no está en disco (una imagen generada).
      **Exige `filename`**.

    El tipo de contenido se deduce de la extensión del nombre; lo desconocido va como binario
    genérico, así que **pon extensión** o el adjunto se descargará sin saber qué es. Tope del
    servidor: 10 MB — se comprueba antes de subir para no fallar a mitad. Queda registrado en el
    historial de la tarjeta, con quién lo subió. Requiere rol colaborador/dueño.
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


@mcp.tool(annotations=BORRA)
async def delete_task_attachment(
    ctx: Ctx, initiative_id: str, task_id: str, attachment_id: str
) -> dict[str, Any]:
    """Borra un adjunto de una tarjeta. **Es irreversible**: el archivo no se recupera.

    El id sale de `list_task_attachments`. Se lee antes de borrar para poder decir qué desapareció
    —un «hecho» a secas no vale para algo que no se puede deshacer— y el backend lo registra en el
    historial de la tarjeta. Requiere rol colaborador/dueño.
    """
    client = _prepare(ctx)
    antes = await client.list_task_attachments(initiative_id, task_id)
    borrado = next((a for a in antes if str(a.get("id")) == attachment_id), None)
    if borrado is None:
        raise FoundryError(
            f"El adjunto {attachment_id} no está en esta tarjeta. "
            f"Tiene {len(antes)}: míralos con `list_task_attachments`."
        )
    peso = _tamano_legible(int(borrado.get("size") or 0))
    await _confirmar_borrado(
        ctx, f'El adjunto «{borrado.get("filename")}» ({peso}) de esta tarjeta.'
    )
    await client.delete_task_attachment(initiative_id, task_id, attachment_id)
    return {
        "deleted": True,
        "id": attachment_id,
        "filename": borrado.get("filename"),
        "size": borrado.get("size"),
        "remaining": len(antes) - 1,
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
    """Crea una nota en el Área (opcionalmente dentro de otra nota/carpeta y con contenido).

    `markdown` acepta encabezados, listas (también anidadas y checklists), citas, código, tablas
    e inline con negrita/enlaces; los [[wikilinks]] crean backlinks. Lo que escribas aquí nace
    como contenido de IA: identifícalo si aporta y no pises trabajo humano sin pedirlo.
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
    """Escribe markdown en una nota YA existente. `mode`: replace (pisa, deja versión en el
    historial) o append (añade al final, sin conflicto).

    OJO: `replace` gana sobre lo no guardado del editor. Sobre una nota escrita por humanos,
    prefiere `append` o pide confirmación antes de reemplazar. Contenido con procedencia de IA.
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


@mcp.tool(annotations=BORRA)
async def delete_note(ctx: Ctx, document_id: str) -> str:
    """Borra una nota (y con ella deja huérfanas a sus hijas: muévelas antes si importan).

    Borrar una nota es decisión humana, así que se **pregunta** antes, diciendo cuál es y cuántas
    notas la enlazan — perder una nota enlazada rompe los backlinks de las demás. Si el cliente no
    sabe preguntar, sigue adelante.
    """
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
    await _confirmar_borrado(ctx, f"La nota «{titulo}»{cuantos}.")
    await client.delete_document(document_id)
    return f"Nota «{titulo}» borrada."


@mcp.tool(annotations=CREA)
async def link_notes(ctx: Ctx, from_document_id: str, to_document_id: str) -> dict[str, Any]:
    """Enlaza dos notas añadiendo al final de la origen un [[wikilink]] a la destino.

    El enlace queda VISIBLE en la nota (así funciona el producto: el wikilink del contenido es lo
    que alimenta los backlinks y el mapa del Área); no hay enlaces invisibles de base de datos.
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
    """Ata una nota a una tarjeta (o semilla) que YA existe: el `#tarjeta` sin abrir el editor.

    Para cuando las dos existen ya y ninguna salió de la otra («esta nota habla de esta tarjeta»).
    Si la tarjeta debe NACER de la nota, usa `extract_tasks_from_note`, que además deja la
    procedencia del párrafo.

    El chip se añade al final de la nota y queda VISIBLE, como si lo hubiera tecleado una persona:
    escribir `#PROY-12` con `write_note` NO sirve —dejaría una almohadilla de texto, no un
    vínculo—, porque el chip lleva el id dentro y de ahí sale el backlink. Después la tarjeta
    enseña la nota en «MENCIONADA EN» (`get_task_notes`) y el mapa del Área dibuja la arista.

    `target_id` es el id de la tarjeta o de la semilla, y tiene que ser del Área de la nota.
    `lead` es el texto que precede al chip; por defecto «Menciona a » / «Nace de ».
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
    """Saca tarjetas del Tablero desde una nota, con la referencia a su párrafo de origen.

    Es el puente Notas → Tablero con trazabilidad (ADR-0011): cada tarjeta nace PROPUESTA
    (gate humano) y enlazada al bloque del que salió — desde la tarjeta se vuelve al párrafo.
    Usa la IA del backend (flujo configurable por usuario).
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

1. **Entérate antes de tocar nada.** Lee la tarjeta con `get_board`, su historial con
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


def main() -> None:
    """Punto de entrada: arranca el servidor MCP por stdio (transporte que usa Claude Code)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
