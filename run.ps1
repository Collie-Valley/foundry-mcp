# Prepara el entorno de desarrollo del MCP y comprueba que carga.
# No arranca el servidor: en uso normal lo lanza tu cliente MCP por stdio (ver README, «Desarrollo»).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

uv sync   # crea .venv e instala las dependencias de pyproject.toml/uv.lock (Windows, Linux y Mac)

# Chequeo de humo: importa el servidor y lista las tools (no necesita .env ni backend).
uv run python -c "import asyncio; from foundry_mcp.server import mcp; print('tools:', ', '.join(sorted(t.name for t in asyncio.run(mcp.list_tools()))))"

Write-Host "`nListo. Para usar esta copia desde tu cliente MCP, mira la sección «Desarrollo» del README." -ForegroundColor Green
