# Load the additive Prana extension immediately after main.py finishes importing.
import builtins
_original_import=builtins.__import__
_loaded=False

def _prana_import(name,globals=None,locals=None,fromlist=(),level=0):
    global _loaded
    module=_original_import(name,globals,locals,fromlist,level)
    if name=='main' and not _loaded:
        try:
            import prana_extensions
            prana_extensions.install(vars(module))
            _loaded=True
        except Exception as exc:
            print('PRANA EXTENSION LOAD ERROR:',repr(exc))
    return module
builtins.__import__=_prana_import
