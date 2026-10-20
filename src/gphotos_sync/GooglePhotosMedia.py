#!/usr/bin/env python3
# coding: utf8

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from gphotos_sync.Checks import get_check

from . import Utils
from .BaseMedia import BaseMedia

DuplicateSuffix = re.compile(r"(.*)[ ]\(\d+\)(\..*)")

JSONValue = Union[str, int, float, bool, None, Dict[str, Any], List[Any]]
JSONType = Union[Dict[str, JSONValue], List[JSONValue]]


class GooglePhotosMedia(BaseMedia):
    """A media item as returned by the Google Photos API.

    Supports both the Picker API PickedMediaItem format (which holds most
    details in 'mediaFile') and the legacy Library API MediaItem format.
    """

    def __init__(self, media_json, to_lower=False):
        self.__media_json: Dict[str, Any] = media_json
        self.__uid: Optional[str] = None
        self.__lower = to_lower
        self.__is_picker = "mediaFile" in media_json
        # the dict that holds filename, mimeType, baseUrl
        self.__file_json: Dict[str, Any] = media_json.get("mediaFile", media_json)
        super(GooglePhotosMedia, self).__init__()
        if self.__is_picker:
            self.__media_meta = self.__file_json.get("mediaFileMetadata", {})
        elif self.is_video:
            self.__media_meta = media_json.get("mediaMetadata", {}).get("video")
        else:
            self.__media_meta = media_json.get("mediaMetadata", {}).get("photo")

    @property
    def uid(self) -> Optional[str]:
        return self.__uid

    # ----- override Properties below -----
    @property
    def size(self) -> int:
        return 0

    @property
    def id(self) -> str:
        return self.__media_json["id"]

    @property
    def description(self) -> str:
        try:
            return get_check().valid_file_name(self.__media_json["description"])
        except KeyError:
            return ""

    @property
    def orig_name(self) -> str:
        try:
            name = self.__file_json["filename"]
            matches = DuplicateSuffix.match(name)
            if matches:
                # append the prefix and the suffix, ditching the ' (n)'
                name = "{}{}".format(*matches.groups())
        except KeyError:
            name = ""
        if self.__lower:
            name = name.lower()
        return str(Path(get_check().valid_file_name(name)))

    @property
    def create_date(self) -> datetime:
        try:
            if self.__is_picker:
                create_date = self.__media_json["createTime"]
            else:
                create_date = self.__media_json["mediaMetadata"].get("creationTime")
            photo_date = Utils.string_to_date(create_date)
        except (KeyError, ValueError):
            photo_date = None
        if photo_date is None:
            photo_date = Utils.MINIMUM_DATE

        # TODO: why does mypy not like this?
        return photo_date  # type: ignore

    @property
    def modify_date(self) -> datetime:
        date = Utils.MINIMUM_DATE
        return date

    @property
    def mime_type(self) -> Optional[str]:
        return self.__file_json.get("mimeType")

    @property
    def is_video(self) -> bool:
        if self.__media_json.get("type") == "VIDEO":
            return True
        return super().is_video

    @property
    def url(self) -> Optional[str]:
        # the Picker API does not supply a productUrl
        return self.__media_json.get("productUrl")

    @property
    def base_url(self) -> Optional[str]:
        return self.__file_json.get("baseUrl")

    @property
    def camera_model(self):
        camera_model = None
        try:
            camera_model = self.__media_meta.get("cameraModel")
        except (KeyError, AttributeError):
            pass
        return camera_model
