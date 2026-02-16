# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html


from anki._backend import RustBackend


def run_api_server(backend: RustBackend) -> None:
    try:
        backend.run_api_server()
    except Exception as exc:
        print("API server failed:", exc)
