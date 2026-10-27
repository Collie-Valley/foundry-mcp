"""Lanzador del MCP desde una copia del código, sin instalar el paquete.

Se ejecuta como script suelto: añade **este** directorio al `sys.path` para poder importar el
paquete `foundry_mcp` sin instalarlo y sin depender del CWD con que el cliente MCP arranque el
proceso. Equivale a `python -m foundry_mcp`. Ver la sección «Desarrollo» del README.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foundry_mcp.server import main  # import tras ajustar sys.path

if __name__ == "__main__":
    main()
