'''Podarcis engine package.

Everything under ``.podarcis/`` is imported as ``podarcis.*`` and nothing else.
The package used to be reachable under two names at once — as ``podarcis.x``
via the editable install and as bare ``x`` via ``sys.path`` inserts scattered
across seven modules — which gave every module two independent instances, each
with its own copy of module-level state.

``ROOT_DIR`` is the single authority on where this checkout lives; modules
derive their paths from it instead of recounting ``parent`` hops.
'''

from pathlib import Path

PODARCIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = PODARCIS_DIR.parent
