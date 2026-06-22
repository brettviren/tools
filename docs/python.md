A Python tool uses the Click module to provide CLI handling.

If the tool is simple, implement it as a single `src/tools/<tool>.py` source file that contains both Click command functions and any library functions.  If it becomes complex, implement the Click command functions as `src/tools/<tool>/__main__.py` and the library functions in one or more `src/tools/<tool>/<module>.py` files. 

A Python tool implementation may depend on any module under the `tools.` module tree.  Factoring common library functions to a common module is encouraged.  Very general purpose functions may be placed into `src/tools/util/*.py`.  More specific but commonly used functions may be placed under `src/tools/<topic>/*.py`.

Ask questions if unsure about proper factoring for a given implementation.  Suggest opportunities to factor code which is common across tools.

If asked to provide a TUI, see use Textual.

If asked to provide a GUI, use `qtpy` wrapper with the default backend of `pyqt6`.

If aked to provide a web UI, use `flask`.
