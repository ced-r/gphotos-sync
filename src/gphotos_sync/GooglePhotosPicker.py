import logging
import re
import sys
import time
import webbrowser
from typing import Any, Callable, Dict, List, Optional

from gphotos_sync.restclient import RestClient

log = logging.getLogger(__name__)

"""
Support for the Google Photos Picker API.

On 31 March 2025 Google removed the photoslibrary.readonly and
photoslibrary.sharing scopes from the Library API. The Library API can now only
see media created by the calling app, so it can no longer be used to back up a
user's library. The replacement is the Picker API: the user selects media in
the Google Photos web UI (up to 2000 items per session) and the app may then
list and download the selected items.

See https://developers.google.com/photos/support/updates
and https://developers.google.com/photos/picker/guides/get-started-picker
"""

PICKER_SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
PICKER_API_URL = "https://photospicker.googleapis.com/$discovery/rest?version=v1"

DURATION = re.compile(r"^([\d.]+)s$")


def parse_duration(duration: Optional[str], default: float) -> float:
    """parse a google-duration string such as '5s' or '1799.5s'"""
    if duration:
        matches = DURATION.match(duration)
        if matches:
            return float(matches.group(1))
    return default


class PickerSession:
    """A single Picker API session: create it, let the user pick media in
    their browser, then list the picked items and provide their base URLs
    for download.
    """

    MAX_ITEMS: int = 2000
    PAGE_SIZE: int = 100
    # base URLs are valid for 60 minutes, refresh them a little before that
    BASE_URL_LIFETIME: float = 50 * 60
    DEFAULT_POLL_INTERVAL: float = 5
    DEFAULT_TIMEOUT: float = 30 * 60

    def __init__(
        self,
        api: RestClient,
        max_items: int = MAX_ITEMS,
        timeout: Optional[float] = None,
        open_browser: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._api = api
        self.max_items = max(0, min(int(max_items), self.MAX_ITEMS))
        self.timeout = timeout
        self.open_browser = open_browser
        self._sleep = sleep
        self._clock = clock

        self.session: Dict[str, Any] = {}
        self.items: Dict[str, dict] = {}
        self._items_fetched_at: float = 0

    @property
    def id(self) -> Optional[str]:
        return self.session.get("id")

    def create(self) -> Dict[str, Any]:
        body = {"pickingConfig": {"maxItemCount": str(self.max_items)}}
        self.session = self._api.sessions.create.execute(body).json()  # type: ignore
        log.debug("created picker session %s", self.id)
        return self.session

    def prompt_user(self):
        uri = self.session["pickerUri"]
        message = (
            "\nOpen this URL in a browser logged into your Google account and "
            "select the photos and videos to back up (max {} per session). "
            "Click 'Done' when finished:\n\n    {}\n".format(self.max_items, uri)
        )
        # use stderr so that the URL is visible whatever the log level is
        print(message, file=sys.stderr, flush=True)
        if self.open_browser:
            try:
                webbrowser.open(uri)
            except webbrowser.Error:
                log.debug("could not open a browser", exc_info=True)

    def wait_for_selection(self) -> bool:
        """poll the session until the user has finished picking.

        Returns:
            True if media items were picked

        Raises:
            TimeoutError if the user does not pick within the timeout
        """
        polling = self.session.get("pollingConfig", {})
        interval = parse_duration(
            polling.get("pollInterval"), self.DEFAULT_POLL_INTERVAL
        )
        timeout = self.timeout or parse_duration(
            polling.get("timeoutIn"), self.DEFAULT_TIMEOUT
        )
        # never hammer the API even if the server suggests a tiny interval
        interval = max(interval, 1.0)

        log.warning("Waiting for media to be picked in the browser ...")
        start = self._clock()
        while True:
            self.session = self._api.sessions.get.execute(  # type: ignore
                sessionId=self.id
            ).json()
            if self.session.get("mediaItemsSet"):
                return True
            if self._clock() - start > timeout:
                raise TimeoutError(
                    "No media were picked within {:.0f} seconds".format(timeout)
                )
            self._sleep(interval)

    def list_items(self) -> List[dict]:
        """list all of the picked media items, this also (re)fetches fresh
        base URLs for every item"""
        items: Dict[str, dict] = {}
        page_token = None
        while True:
            response = self._api.mediaItems.list.execute(  # type: ignore
                sessionId=self.id, pageSize=self.PAGE_SIZE, pageToken=page_token
            ).json()
            for item in response.get("mediaItems", []):
                items[item["id"]] = item
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        self.items = items
        self._items_fetched_at = self._clock()
        log.warning("%d media items were picked", len(items))
        return list(items.values())

    def _refresh_if_stale(self):
        if self._clock() - self._items_fetched_at > self.BASE_URL_LIFETIME:
            log.info("refreshing picked media base URLs")
            self.list_items()

    def base_url(self, media_id: str) -> Optional[str]:
        """return a currently valid base URL for a picked media item or
        None if this item was not picked in this session"""
        self._refresh_if_stale()
        item = self.items.get(media_id)
        if not item:
            return None
        return item.get("mediaFile", {}).get("baseUrl")

    def is_ready(self, media_id: str) -> bool:
        """videos may still be processing on the server and cannot be
        downloaded until their processingStatus is READY"""
        item = self.items.get(media_id, {})
        if item.get("type") != "VIDEO":
            return True
        meta = item.get("mediaFile", {}).get("mediaFileMetadata", {})
        status = meta.get("videoMetadata", {}).get("processingStatus", "READY")
        return status == "READY"

    def __contains__(self, media_id: str) -> bool:
        return media_id in self.items

    def delete(self):
        """sessions expire anyway but Google recommend deleting them when
        done"""
        if self.id:
            try:
                self._api.sessions.delete.execute(sessionId=self.id)  # type: ignore
                log.debug("deleted picker session %s", self.id)
            except Exception:
                log.debug("failed to delete picker session", exc_info=True)
            self.session = {}
