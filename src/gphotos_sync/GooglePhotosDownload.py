# type: ignore
# lots of typing issues in this file, partly due to use of asyncIO Future
# and concurrent Future - TODO: for reviewimport concurrent.futures as futures
import concurrent.futures as futures
import errno
import logging
import os
import shutil
import tempfile
from asyncio import Future
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError, RequestException, Timeout
from urllib3.util.retry import Retry

from gphotos_sync import Utils
from gphotos_sync.BadIds import BadIds
from gphotos_sync.DatabaseMedia import DatabaseMedia
from gphotos_sync.GooglePhotosPicker import PickerSession
from gphotos_sync.GooglePhotosRow import GooglePhotosRow
from gphotos_sync.LocalData import LocalData

from .Settings import Settings

try:
    import win32con  # type: ignore
    import win32file  # type: ignore

    _use_win_32 = True
except ImportError:
    win32file, win32con = None, None
    _use_win_32 = False

log = logging.getLogger(__name__)


class GooglePhotosDownload(object):
    """A Class for managing the indexing and download of Google Photos"""

    PAGE_SIZE: int = 100
    BATCH_SIZE: int = 40

    def __init__(
        self,
        access_token: Callable[[], str],
        root_folder: Path,
        db: LocalData,
        settings: Settings,
    ):
        """
        Parameters:
            access_token: returns a valid OAuth bearer token, Picker API
              base URLs require an Authorization header
            root_folder: path to the root of local file synchronization
            db: local database for indexing
            settings: further arguments
        """
        self._db: LocalData = db
        self._root_folder: Path = root_folder
        self._access_token = access_token
        self._picker: Optional[PickerSession] = None
        self.files_not_ready: int = 0

        self.files_downloaded: int = 0
        self.files_download_started: int = 0
        self.files_download_skipped: int = 0
        self.files_download_failed: int = 0

        self.settings = settings
        self.max_threads = settings.max_threads
        self.start_date: datetime = settings.start_date
        self.end_date: datetime = settings.end_date
        self.retry_download: bool = settings.retry_download
        self.case_insensitive_fs: bool = settings.case_insensitive_fs
        self.video_timeout: int = settings.video_timeout
        self.image_timeout: int = settings.image_timeout

        # attributes related to multi-threaded download
        self.download_pool = futures.ThreadPoolExecutor(max_workers=self.max_threads)
        self.pool_future_to_media: Dict[Future, DatabaseMedia] = {}
        self.bad_ids = BadIds(self._root_folder)

        self.current_umask = os.umask(7)
        os.umask(self.current_umask)

        self._session = requests.Session()
        # define the retry behaviour for each connection. Note that
        # respect_retry_after_header=True means that status codes [413, 429, 503]
        # will backoff for the recommended period defined in the retry after header
        retries = Retry(
            total=settings.max_retries,
            backoff_factor=5,
            status_forcelist=[500, 502, 503, 504, 509, 429],
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        self._session.mount(
            "https://", HTTPAdapter(max_retries=retries, pool_maxsize=self.max_threads)
        )

    def close(self):
        self._session.close()

    def download_photo_media(self, picker: PickerSession):
        """
        Download all indexed media that was picked in the given Picker
        session and has not yet been downloaded.

        Base URLs are only available for items picked in a live Picker
        session so items indexed in earlier runs but not downloaded must be
        picked again.
        """
        self._picker = picker

        def grouper(
            iterable: Iterable[DatabaseMedia],
        ) -> Iterable[Iterable[DatabaseMedia]]:
            """Collect data into chunks size BATCH_SIZE"""
            return zip_longest(*[iter(iterable)] * self.BATCH_SIZE, fillvalue=None)

        if not self.retry_download:
            self.files_download_skipped = self._db.downloaded_count()

        log.warning("Downloading Photos ...")
        try:
            for media_items_block in grouper(
                self._db.get_rows_by_search(
                    GooglePhotosRow,
                    start_date=self.start_date,
                    end_date=self.end_date,
                    skip_downloaded=not self.retry_download,
                )
            ):
                batch = {}

                items = (mi for mi in media_items_block if mi)
                for media_item in items:
                    if media_item.id not in picker:
                        # no base URL available - not picked this session
                        continue
                    if self.case_insensitive_fs:
                        relative_folder = str(media_item.relative_folder).lower()
                        filename = str(media_item.filename).lower()
                    else:
                        relative_folder = media_item.relative_folder
                        filename = media_item.filename
                    local_folder = self._root_folder / relative_folder
                    local_full_path = local_folder / filename

                    try:
                        if local_full_path.exists():
                            self.files_download_skipped += 1
                            log.debug(
                                "SKIPPED download (file exists) %d %s",
                                self.files_download_skipped,
                                media_item.relative_path,
                            )
                            self._db.put_downloaded(media_item.id)

                        elif not picker.is_ready(media_item.id):
                            self.files_not_ready += 1
                            log.warning(
                                "SKIPPED %s - video is still being processed by "
                                "Google, pick it again later",
                                media_item.relative_path,
                            )

                        elif self.bad_ids.check_id_ok(media_item.id):
                            batch[media_item.id] = media_item
                            if not local_folder.is_dir():
                                local_folder.mkdir(parents=True)

                    except Exception as err:
                        # skip files with filenames too long for this OS.
                        # probably thrown by local_full_path.exists().
                        errname = type(err).__name__
                        if errname == "OSError" and (
                            err.errno == errno.ENAMETOOLONG  # type: ignore
                        ):
                            log.warning(
                                "SKIPPED file because name is too long for this OS %s",
                                local_full_path,
                            )
                            self.files_download_failed += 1
                        else:
                            # re-raise other errors
                            raise

                if len(batch) > 0:
                    self.download_batch(batch)
        finally:
            # allow any remaining background downloads to complete
            futures_left = list(self.pool_future_to_media.keys())
            self.do_download_complete(futures_left)
            log.warning(
                "Downloaded %d Items, Failed %d, Already Downloaded %d, "
                "Not Ready %d",
                self.files_downloaded,
                self.files_download_failed,
                self.files_download_skipped,
                self.files_not_ready,
            )
            self.bad_ids.store_ids()
            self.bad_ids.report()
        return self.files_downloaded

    def download_batch(self, batch: Mapping[str, DatabaseMedia]):
        """Downloads a batch of media items collected in download_photo_media.

        A fresh 'base_url' is required since they have a limited lifespan.
        The picker session re-lists its items to refresh them when needed.
        """
        try:
            for media_id, media_item in batch.items():
                base_url = self._picker.base_url(media_id)
                if not base_url:
                    log.warning("No base URL for %s", media_item.relative_path)
                    continue
                self.download_file(media_item, base_url)

        except KeyboardInterrupt:
            log.warning("Cancelling download threads ...")
            for f in self.pool_future_to_media:
                f.cancel()
            futures.wait(self.pool_future_to_media)
            log.warning("Cancelled download threads")
            raise

    def download_file(self, media_item: DatabaseMedia, base_url: str):
        """farms a single media download off to the thread pool.

        Uses a dictionary of Futures -> mediaItem to track downloads that are
        currently scheduled/running. When a Future is done it calls
        do_download_complete to remove the Future from the dictionary and
        complete processing of the media item.
        """
        # we dont want a massive queue so wait until at least one thread is free
        while len(self.pool_future_to_media) >= self.max_threads:
            # check which futures are done, complete the main thread work
            # and remove them from the dictionary
            done_list = []
            for future in self.pool_future_to_media.keys():
                if future.done():
                    done_list.append(future)

            if done_list:
                self.do_download_complete(done_list)
            else:
                futures.wait(
                    list(self.pool_future_to_media.keys()),
                    return_when=futures.FIRST_COMPLETED,
                )

        # fetch the token in the main thread so that any refresh is not
        # performed concurrently by worker threads
        headers = {"Authorization": "Bearer {}".format(self._access_token())}

        # start a new background download
        self.files_download_started += 1
        log.info(
            "downloading %d %s", self.files_download_started, media_item.relative_path
        )
        future = self.download_pool.submit(
            self.do_download_file, base_url, media_item, headers
        )
        self.pool_future_to_media[future] = media_item

    def do_download_file(
        self, base_url: str, media_item: DatabaseMedia, headers: Dict[str, str]
    ):
        """Runs in a process pool and does a download of a single media item."""
        if self.case_insensitive_fs:
            relative_folder = str(media_item.relative_folder).lower()
            filename = str(media_item.filename).lower()
        else:
            relative_folder = media_item.relative_folder
            filename = media_item.filename
        local_folder = self._root_folder / relative_folder
        local_full_path = local_folder / filename

        if media_item.is_video:
            download_url = "{}=dv".format(base_url)
            timeout = self.video_timeout
        else:
            download_url = "{}=d".format(base_url)
            timeout = self.image_timeout
        temp_file = tempfile.NamedTemporaryFile(dir=local_folder, delete=False)
        t_path = Path(temp_file.name)

        try:
            response = self._session.get(
                download_url, stream=True, timeout=timeout, headers=headers
            )
            response.raise_for_status()
            shutil.copyfileobj(response.raw, temp_file)
            temp_file.close()
            temp_file = None
            response.close()
            t_path.rename(local_full_path)
            create_date = Utils.safe_timestamp(media_item.create_date)
            try:
                os.utime(
                    str(local_full_path),
                    (
                        Utils.safe_timestamp(media_item.modify_date).timestamp(),
                        create_date.timestamp(),
                    ),
                )
            except (PermissionError,):
                log.debug("Could not set times for downloaded file")
            if _use_win_32:
                file_handle = win32file.CreateFile(
                    str(local_full_path),
                    win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32con.OPEN_EXISTING,
                    0,
                    None,
                )
                win32file.SetFileTime(file_handle, *(create_date,) * 3)
                file_handle.close()
            try:
                os.chmod(str(local_full_path), 0o666 & ~self.current_umask)
            except (PermissionError,):
                log.debug("Could not set file access rights for downloaded file")
        except KeyboardInterrupt:
            log.debug("User cancelled download thread")
            raise
        finally:
            if temp_file:
                temp_file.close()
            if t_path.exists():
                t_path.unlink()

    def do_download_complete(
        self,
        futures_list: Union[
            Mapping[futures.Future, DatabaseMedia], List[futures.Future]
        ],
    ):
        """runs in the main thread and completes processing of a media
        item once (multi threaded) do_download has completed
        """
        for future in futures_list:
            media_item = self.pool_future_to_media.get(future)
            timeout = self.video_timeout if media_item.is_video else self.image_timeout
            e = future.exception(timeout=timeout)
            if e:
                self.files_download_failed += 1
                log.error(
                    "FAILURE %d downloading %s - %s",
                    self.files_download_failed,
                    media_item.relative_path,
                    e,
                )
                # treat API errors as possibly transient. Report them above in
                # log.error but do not raise them. Other exceptions will raise
                # up to the root handler and abort. Note that all retry logic is
                # already handled in urllib3

                # Items that cause API errors go in a BadIds file which must
                # be deleted to retry these items. Also do this for timeouts
                # which have been reported as happening on files missing on
                # the server. See #480 and #488
                if isinstance(e, HTTPError) and getattr(
                    e.response, "status_code", None
                ) in (401, 403):
                    # the base URL expired or access was revoked, this is
                    # not a problem with the media item so allow a retry
                    log.warning(
                        "access denied for %s, pick it again to retry",
                        media_item.relative_path,
                    )
                elif isinstance(e, RequestException) or isinstance(e, Timeout):
                    self.bad_ids.add_id(
                        media_item.relative_path, media_item.id, media_item.url, e
                    )
                else:
                    # don't leave the thread hanging if we are going to raise
                    del self.pool_future_to_media[future]
                    raise e
            else:
                self._db.put_downloaded(media_item.id)
                self.files_downloaded += 1
                log.debug(
                    "COMPLETED %d downloading %s",
                    self.files_downloaded,
                    media_item.relative_path,
                )
                if self.settings.progress and self.files_downloaded % 10 == 0:
                    log.warning(f"Downloaded {self.files_downloaded} items ...\033[F")
            del self.pool_future_to_media[future]
