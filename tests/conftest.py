"""Load the plugin as a package, the way Hermes' own loader does.

``hermes_cli/plugins.py`` loads a plugin with
``spec_from_file_location(name, __init__.py, submodule_search_locations=[dir])``,
so the plugin is a real package and its relative imports work. The unit tests
need the same shape, otherwise ``from . import config`` inside the plugin fails.

The loaded submodules are aliased into ``sys.modules`` under flat names so tests
can ``import novita_env`` etc. without depending on pytest's import mode.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
_NS = "novita_test_ns"
_SLUG = "novita_sandbox_plugin"
_PKG_NAME = f"{_NS}.{_SLUG}"


def _ensure_namespace() -> None:
    if _NS not in sys.modules:
        namespace = types.ModuleType(_NS)
        namespace.__path__ = []  # type: ignore[attr-defined]
        namespace.__package__ = _NS
        sys.modules[_NS] = namespace


def _load_package() -> types.ModuleType:
    if _PKG_NAME in sys.modules:
        return sys.modules[_PKG_NAME]
    _ensure_namespace()
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PKG_NAME
    module.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


package = _load_package()

config = importlib.import_module(f"{_PKG_NAME}.config")
environment = importlib.import_module(f"{_PKG_NAME}.environment")
inject = importlib.import_module(f"{_PKG_NAME}.inject")
setup_patch = importlib.import_module(f"{_PKG_NAME}.setup_patch")

for _alias, _module in (
    ("novita_plugin", package),
    ("novita_config", config),
    ("novita_env", environment),
    ("novita_inject", inject),
    ("novita_setup_patch", setup_patch),
):
    sys.modules[_alias] = _module
