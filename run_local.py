"""Local dev server — starts uvicorn on the Modal-free ASGI app.

Importing ``app.asgi`` loads ``.env`` and runs ``init_db()`` as a side effect, so
this stays a thin wrapper. For reload/workers, run uvicorn directly instead:

    uvicorn app.asgi:app --reload --port 8000
"""
import uvicorn

if __name__ == "__main__":
    print("Starting recruiting engine on http://localhost:8000")
    uvicorn.run("app.asgi:app", host="0.0.0.0", port=8000)
