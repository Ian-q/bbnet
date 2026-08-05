"""bbnet — a netlist, DRC, and layout engine for hand-built breadboards.

Islands of parts on 0.1" grid boards; derived nets; design-rule checks;
a two-layer autorouter; a printable build sheet. The engine knows nothing
about any particular project — see `signals.py` for the one hook a host
application uses to supply its own pin-allocation table.
"""

__version__ = "0.1.0"
