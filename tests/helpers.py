"""Shared test helpers. One definition of `registry()` for every test
module that needs to hand model.derive() (or cli.g_signals) a
SignalRegistry, so the empty-source boilerplate isn't copy-pasted into
every module that touches the fixture corpus."""


def registry(rows=(), name="pinmap"):
    """A SignalRegistry holding one source, for model.derive()."""
    from bbnet import signals
    reg = signals.SignalRegistry()
    reg.register(name, rows)
    return reg
