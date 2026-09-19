"""Phase 0 AC: 'no MetaTrader5 import at module load' (SPEC C7).

Importing the app and every mt5 module must never pull in the Windows-only
MetaTrader5 package — that's what lets pytest run green on non-Windows.
"""

import sys


def test_no_metatrader5_import_at_module_load() -> None:
    import app.main  # noqa: F401 — imports app.mt5.{base,mock_source,connection}
    import app.mt5.connection  # noqa: F401 — factory imports mt5_source lazily
    import app.mt5.mt5_source  # noqa: F401 — the lazy-import wrapper itself

    assert "MetaTrader5" not in sys.modules


def test_mt5_source_lazy_import_only_on_use() -> None:
    import sys

    from app.mt5.mt5_source import MT5DataSource

    source = MT5DataSource()  # constructing must NOT import MetaTrader5
    assert "MetaTrader5" not in sys.modules
    assert source._mt5 is None  # noqa: SLF001 — internal lazy handle stays unset
