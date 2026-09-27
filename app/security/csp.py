"""Per-request CSP nonce.

script-src allows only same-origin files and inline <script> blocks carrying this
request's nonce, so an injected script can't run even if escaping ever misses one.
Templates write it with {{ csp_nonce() }}; htmx gets it through the htmx-config
meta tag (inlineScriptNonce) so scripts inside swapped fragments still execute.
Inline event handlers (onclick=...) are not allowed at all; app/static/js/actions.js
handles those through data- attributes.
"""
import contextvars
import secrets

_nonce: contextvars.ContextVar[str] = contextvars.ContextVar("csp_nonce", default="")


def new_nonce() -> str:
    n = secrets.token_urlsafe(16)
    _nonce.set(n)
    return n


def csp_nonce() -> str:
    return _nonce.get()
