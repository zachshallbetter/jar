"""Shift the wall clock by TIME_SHIFT_DAYS (float), by TIME_SHIFT_DAYS=@<epoch>, or to an absolute instant with
TIME_SHIFT_TO=<ISO time, e.g. 2026-11-01T08:30:00Z> (no zone means UTC) for clock-bomb hunting. The clock runs on from
that instant. Monotonic/perf clocks untouched."""
import os
_d = os.environ.get("TIME_SHIFT_DAYS")
_to = os.environ.get("TIME_SHIFT_TO")
if _to:   # an absolute target: same as @<epoch>
    import datetime as _d0, time as _t1
    _at = _d0.datetime.fromisoformat(_to.strip().replace("Z", "+00:00"))
    if _at.tzinfo is None:
        _at = _at.replace(tzinfo=_d0.timezone.utc)
    _d = "@%s" % _at.timestamp()
if _d and _d.startswith("@"):   # "@<epoch>": shift so the clock starts at that instant
    import time as _t0
    _d = str((float(_d[1:]) - _t0.time()) / 86400.0)
if _d:
    import time as _time, datetime as _dtmod
    _OFF = float(_d) * 86400.0
    _rt, _rtn = _time.time, _time.time_ns
    _time.time = lambda: _rt() + _OFF
    _time.time_ns = lambda: _rtn() + int(_OFF * 1e9)
    _lt, _gt, _sf, _ct, _at = _time.localtime, _time.gmtime, _time.strftime, _time.ctime, _time.asctime
    _time.localtime = lambda s=None: _lt(_time.time() if s is None else s)
    _time.gmtime = lambda s=None: _gt(_time.time() if s is None else s)
    _time.ctime = lambda s=None: _ct(_time.time() if s is None else s)
    def _strftime(fmt, t=None):
        return _sf(fmt, _lt(_time.time()) if t is None else t)
    _time.strftime = _strftime
    # Libraries that subclass date/datetime with their own metaclass (pydantic) bind to the real classes first.
    for _m in ("pydantic", "pydantic.v1", "pydantic.v1.types", "fastapi", "starlette.responses", "httpx", "email.utils", "sqlite3"):
        try:
            __import__(_m)
        except Exception:
            pass
    _RD, _RDT = _dtmod.date, _dtmod.datetime
    class _Meta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, cls.__bases__[0])
        def __subclasscheck__(cls, sub):
            return issubclass(sub, cls.__bases__[0])
    class FakeDate(_RD, metaclass=_Meta):
        @classmethod
        def today(cls):
            t = _RDT.fromtimestamp(_time.time())
            return _RD(t.year, t.month, t.day)
    class FakeDateTime(_RDT, metaclass=_Meta):
        @classmethod
        def now(cls, tz=None):
            return _RDT.fromtimestamp(_time.time(), tz)
        @classmethod
        def utcnow(cls):
            return _RDT.utcfromtimestamp(_time.time())
        @classmethod
        def today(cls):
            return _RDT.fromtimestamp(_time.time())
    _dtmod.date = FakeDate
    _dtmod.datetime = FakeDateTime
    if os.environ.get("TIME_SHIFT_VERBOSE"):
        import sys; print(f"[shim] now={_RDT.fromtimestamp(_time.time(), _dtmod.timezone.utc).isoformat()}", file=sys.stderr)
