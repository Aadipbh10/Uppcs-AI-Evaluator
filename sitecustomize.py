"""PRANA PCS — sitecustomize (intentionally inert).

Historically this file monkeypatched main.py at interpreter startup to work
around latency and SQLAlchemy session-lifecycle bugs. Those fixes now live
directly in main.py (expire_on_commit=False, session-safe evaluation_access,
unified admin resolver, fast Gemini/raster config, self-contained initData
validation, chat-id-scoped Telegram worker).

It is kept as a no-op only because Python auto-imports `sitecustomize` when it
is on sys.path. Removing the patching removes a fragile, hard-to-see layer where
runtime behaviour silently depended on an auto-imported file.
"""
# No runtime patching. All fixes are folded into main.py.
