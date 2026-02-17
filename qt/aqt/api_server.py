# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html


from concurrent.futures import Future

from aqt.main import AnkiQt


def run_api_server_in_background(mw: AnkiQt) -> None:
    def on_done(future: Future) -> None:
        try:
            future.result()
        except Exception as exc:
            print("API server failed:", exc)

    mw.taskman.run_in_background(
        lambda: mw.backend.run_api_server(),
        on_done,
        uses_collection=False,
    )
