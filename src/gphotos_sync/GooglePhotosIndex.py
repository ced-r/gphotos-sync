import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable

from gphotos_sync import Utils
from gphotos_sync.GooglePhotosMedia import GooglePhotosMedia
from gphotos_sync.GooglePhotosRow import GooglePhotosRow
from gphotos_sync.LocalData import LocalData
from gphotos_sync.LocalFilesMedia import LocalFilesMedia
from gphotos_sync.Settings import Settings

log = logging.getLogger(__name__)


class GooglePhotosIndex(object):
    PAGE_SIZE = 100

    def __init__(self, root_folder: Path, db: LocalData, settings: Settings):
        self._root_folder: Path = root_folder
        self._db: LocalData = db

        self.files_indexed: int = 0
        self.files_index_skipped: int = 0

        if db:
            self.latest_download = self._db.get_scan_date() or Utils.MINIMUM_DATE

        self.settings = settings
        self.start_date: datetime = settings.start_date
        self.end_date: datetime = settings.end_date
        self.include_video: bool = settings.include_video
        self.rescan: bool = settings.rescan
        self.favourites = settings.favourites_only
        self.case_insensitive_fs: bool = settings.case_insensitive_fs
        self.archived: bool = settings.archived
        self._use_flat_path: bool = settings.use_flat_path
        self._media_folder: Path = settings.photos_path

    def check_for_removed_in_folder(self, folder: Path):
        for pth in folder.iterdir():
            if pth.is_dir():
                self.check_for_removed_in_folder(pth)
            else:
                local_path = pth.relative_to(self._root_folder).parent
                if pth.match(".*") or pth.match("gphotos*"):
                    continue
                file_row = self._db.get_file_by_path(
                    GooglePhotosRow, local_path, pth.name
                )
                if not file_row:
                    pth.unlink()
                    log.warning("%s deleted", pth)

    def check_for_removed(self):
        """Removes local files that are no longer represented in the Photos
        Library - presumably because they were deleted.

        note for partial scans using date filters this is still OK because
        for a file to exist it must have been indexed in a previous scan
        """
        log.warning("Finding and removing deleted media ...")
        self.check_for_removed_in_folder(self._root_folder / self._media_folder)

    def write_media_index(self, media: GooglePhotosMedia, update: bool = True):
        self._db.put_row(GooglePhotosRow.from_media(media), update)
        if media.create_date > self.latest_download:
            self.latest_download = media.create_date

    def _wanted(self, media_item: GooglePhotosMedia) -> bool:
        """The Picker API has no server side filters so apply the date and
        media type filters here"""
        if media_item.is_video and not self.include_video:
            return False
        if self.start_date and media_item.create_date < self.start_date:
            return False
        if self.end_date and media_item.create_date > self.end_date:
            return False
        return True

    def index_picked_media(self, items: Iterable[dict]) -> int:
        """add the media items picked in a Picker API session to the index

        Args:
            items: PickedMediaItem json as returned by mediaItems.list

        Returns:
            the number of new items added to the index
        """
        log.warning("Indexing picked Google Photos Files ...")
        total_listed = 0
        indexed_before = self.files_indexed
        items = list(items)
        picked_ids = {item["id"] for item in items}

        for media_item_json in items:
            total_listed += 1
            media_item = GooglePhotosMedia(
                media_item_json, to_lower=self.case_insensitive_fs
            )
            if not self._wanted(media_item):
                self.files_index_skipped += 1
                log.debug("Skipped Index (filtered) %s", media_item.orig_name)
                continue
            media_item.set_path_by_date(self._media_folder, self._use_flat_path)

            num, row = self._db.file_duplicate_no(
                str(media_item.filename),
                str(media_item.relative_folder),
                media_item.id,
            )
            if not row:
                # the same file may have been indexed under a different id
                # by an earlier Picker session or by the old Library API
                row = self._db.find_same_file(
                    str(media_item.orig_name),
                    str(media_item.relative_folder),
                    media_item.create_date,
                )
                # but never steal the entry of another item picked right now
                # (e.g. two burst shots with the same name and timestamp)
                if row and row.id in picked_ids:
                    row = None
                if row:
                    log.debug(
                        "Re-identified %s as %s", row.relative_path, media_item.id
                    )
                    self._db.put_remote_id(row.id, media_item.id)
                    num = row.duplicate_number

            # we just learned if there were any duplicates in the db
            media_item.duplicate_number = num

            if self.settings.progress and total_listed % 10 == 0:
                log.warning(f"Listed {total_listed} items ...\033[F")
            if not row:
                self.files_indexed += 1
                log.info("Indexed %d %s", self.files_indexed, media_item.relative_path)
                self.write_media_index(media_item, False)
                if self.files_indexed % 2000 == 0:
                    self._db.store()
            else:
                self.files_index_skipped += 1
                log.debug(
                    "Skipped Index (already indexed) %d %s",
                    self.files_index_skipped,
                    media_item.relative_path,
                )
                self.latest_download = max(self.latest_download, media_item.create_date)

        self._db.set_scan_date(last_date=self.latest_download)
        self._db.store()

        new_items = self.files_indexed - indexed_before
        log.warning(f"indexed {new_items} new items of {total_listed} picked")
        return new_items

    def get_extra_meta(self):
        count = 0
        log.warning(
            "updating index with extra metadata for comparison "
            "(may take some time) ..."
        )
        media_items = self._db.get_rows_by_search(GooglePhotosRow, uid="ISNULL")
        for item in media_items:
            file_path = self._root_folder / item.relative_path
            # if this item has a uid it has been scanned before
            if file_path.exists():
                local_file = LocalFilesMedia(file_path)
                count += 1
                log.info("updating metadata %d on %s", count, file_path)
                item.update_extra_meta(
                    local_file.uid, local_file.create_date, local_file.size
                )
                # erm lets try some duck typing then !
                # todo is the DbRow class model rubbish or brilliant Python?
                # noinspection PyTypeChecker
                self._db.put_row(GooglePhotosRow.from_media(item), update=True)
                if count % 2000 == 0:
                    self._db.store()
            else:
                log.debug("skipping metadata (already scanned) on %s", file_path)
        log.warning("updating index with extra metadata complete")
