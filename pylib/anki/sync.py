# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from typing import Any

from anki import sync_pb2

# public exports
SyncAuth = sync_pb2.SyncAuth
SyncOutput = sync_pb2.SyncCollectionResponse
SyncStatus = sync_pb2.SyncStatusResponse


# Legacy attributes some add-ons may be using


def __getattr__(name: str) -> Any:
    # requests is slow to import, so it is only imported when used
    if name in ("HttpClient", "AnkiRequestsClient"):
        from .httpclient import HttpClient

        return HttpClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class Syncer:
    def sync(self) -> str:
        pass
