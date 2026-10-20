"""
Offline tests of the Google Photos Picker API support.

A fake Google backend is mounted as a requests transport adapter so that the
real RestClient, PickerSession, indexing, database and download code all run
end to end without network access or a Google account.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.models import PreparedRequest, Response

import gphotos_sync.GooglePhotosDownload as GooglePhotosDownload
from gphotos_sync.__main__ import GooglePhotosSyncMain
from gphotos_sync.authorize import Authorize
from gphotos_sync.GooglePhotosMedia import GooglePhotosMedia
from gphotos_sync.GooglePhotosPicker import PICKER_SCOPE, PickerSession, parse_duration
from gphotos_sync.restclient import RestClient

TESTS = Path(__file__).absolute().parent.parent
DISCOVERY = json.loads((TESTS / "test-data" / "picker_discovery.json").read_text())
SECRET = TESTS / "test_credentials" / "client_secret.json"
JPEG = (TESTS / "test-data" / "20180126_185832.jpg").read_bytes()
TOKEN = "test-access-token"
CONTENT_HOST = "https://lh3.googleusercontent.com/fake/"


def picked(media_id, filename, create_time, video=False, status="READY"):
    meta: dict = {"width": 10, "height": 10, "cameraModel": "Pixel"}
    if video:
        meta["videoMetadata"] = {"fps": 30, "processingStatus": status}
    else:
        meta["photoMetadata"] = {"isoEquivalent": 100}
    return {
        "id": media_id,
        "createTime": create_time,
        "type": "VIDEO" if video else "PHOTO",
        "mediaFile": {
            "baseUrl": CONTENT_HOST + media_id,
            "mimeType": "video/mp4" if video else "image/jpeg",
            "filename": filename,
            "mediaFileMetadata": meta,
        },
    }


class FakeGoogle(BaseAdapter):
    """Fake implementation of the Picker API and the media content server"""

    def __init__(self, items: List[dict], polls_before_set: int = 2):
        super().__init__()
        self.items = items
        self.polls_before_set = polls_before_set
        self.requests: List[PreparedRequest] = []
        self.sessions: Dict[str, dict] = {}
        self.deleted: List[str] = []
        self.forbidden: set = set()
        self.list_calls = 0
        self._next = 0

    def _response(self, request, status=200, body=b"", json_body=None):
        resp = Response()
        resp.status_code = status
        resp.request = request
        resp.url = request.url
        if json_body is not None:
            body = json.dumps(json_body).encode()
            resp.headers["Content-Type"] = "application/json"
        import io

        resp.raw = io.BytesIO(body)
        return resp

    def send(self, request, **kwargs):
        self.requests.append(request)
        url = urlparse(request.url)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}

        if request.url.startswith(CONTENT_HOST):
            if request.headers.get("Authorization") != "Bearer " + TOKEN:
                return self._response(request, 401)
            media_id, _, param = url.path.split("/")[-1].partition("=")
            if media_id in self.forbidden:
                return self._response(request, 403)
            assert param in ("d", "dv"), param
            return self._response(request, body=JPEG)

        assert url.netloc == "photospicker.googleapis.com", request.url
        if url.path == "/$discovery/rest":
            return self._response(request, json_body=DISCOVERY)

        if request.method == "POST" and url.path == "/v1/sessions":
            assert request.headers["Content-Type"] == "application/json"
            body = json.loads(request.body)
            self._next += 1
            sid = "session-{}".format(self._next)
            self.sessions[sid] = {"polls": 0, "config": body["pickingConfig"]}
            return self._response(request, json_body=self._session(sid))

        if url.path.startswith("/v1/sessions/"):
            sid = url.path.split("/")[-1]
            if request.method == "DELETE":
                self.deleted.append(sid)
                return self._response(request, json_body={})
            self.sessions[sid]["polls"] += 1
            return self._response(request, json_body=self._session(sid))

        if url.path == "/v1/mediaItems":
            sid = query["sessionId"]
            assert self._session(sid)["mediaItemsSet"]
            self.list_calls += 1
            size = int(query["pageSize"])
            start = int(query.get("pageToken", 0))
            page: dict = {"mediaItems": self.items[start : start + size]}
            if start + size < len(self.items):
                page["nextPageToken"] = str(start + size)
            return self._response(request, json_body=page)

        raise AssertionError("unexpected request " + request.url)

    def _session(self, sid):
        s = self.sessions[sid]
        result = {
            "id": sid,
            "pickerUri": "https://photos.google.com/picker/" + sid,
            "mediaItemsSet": s["polls"] >= self.polls_before_set,
            "expireTime": "2099-01-01T00:00:00Z",
        }
        if not result["mediaItemsSet"]:
            result["pollingConfig"] = {"pollInterval": "1s", "timeoutIn": "600s"}
        return result

    def close(self):
        pass


def fake_session(fake: FakeGoogle) -> requests.Session:
    session = requests.Session()
    session.mount("https://", fake)
    return session


@pytest.fixture
def run_sync(tmp_path, monkeypatch):
    """run the real gphotos-sync main against a FakeGoogle backend"""
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def run(fake: FakeGoogle, *extra_args):
        def authorize(self):
            self.session = fake_session(fake)

        args = [
            str(tmp_path / "backup"),
            "--secret",
            str(SECRET),
            "--picker-once",
            "--no-browser",
            "--max-threads",
            "4",
            "--log-level",
            "error",
            *extra_args,
        ]
        with mock.patch.object(Authorize, "authorize", authorize), mock.patch.object(
            Authorize, "access_token", lambda self: TOKEN
        ), mock.patch.object(
            GooglePhotosDownload, "HTTPAdapter", lambda **kwargs: fake
        ):
            GooglePhotosSyncMain().main(args)
        return tmp_path / "backup"

    return run


def db_rows(root: Path):
    con = sqlite3.connect(str(root / "gphotos.sqlite"))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM SyncFiles ORDER BY FileName").fetchall()
    con.close()
    return rows


ITEMS = [
    picked("id-photo1", "IMG_0001.jpg", "2023-05-04T10:11:12Z"),
    picked("id-photo2", "IMG_0002.jpg", "2024-12-31T23:59:59.500Z"),
    picked("id-video1", "VID_0001.mp4", "2024-01-02T03:04:05Z", video=True),
    picked(
        "id-video2",
        "VID_0002.mp4",
        "2024-01-02T03:04:06Z",
        video=True,
        status="PROCESSING",
    ),
]


def test_full_picker_sync(run_sync):
    fake = FakeGoogle(ITEMS)
    root = run_sync(fake)

    photos = root / "photos"
    assert (photos / "2023" / "05" / "IMG_0001.jpg").read_bytes() == JPEG
    assert (photos / "2024" / "12" / "IMG_0002.jpg").exists()
    assert (photos / "2024" / "01" / "VID_0001.mp4").exists()
    # still processing on the server so must not be downloaded yet
    assert not (photos / "2024" / "01" / "VID_0002.mp4").exists()

    rows = {r["FileName"].lower(): r for r in db_rows(root)}
    assert set(rows) == {"img_0001.jpg", "img_0002.jpg", "vid_0001.mp4", "vid_0002.mp4"}
    assert rows["vid_0002.mp4"]["Downloaded"] == 0
    assert rows["img_0001.jpg"]["Downloaded"] == 1

    # the session was polled until picked, then cleaned up
    assert fake.sessions["session-1"]["polls"] >= 2
    assert fake.sessions["session-1"]["config"] == {"maxItemCount": "2000"}
    assert fake.deleted == ["session-1"]
    # videos use =dv and every download carries the bearer token
    downloads = [r for r in fake.requests if r.url.startswith(CONTENT_HOST)]
    assert {r.url for r in downloads} == {
        CONTENT_HOST + "id-photo1=d",
        CONTENT_HOST + "id-photo2=d",
        CONTENT_HOST + "id-video1=dv",
    }
    # nothing should touch the removed Library API
    assert not [r for r in fake.requests if "photoslibrary" in r.url]


def test_incremental_and_reidentified(run_sync):
    root = run_sync(FakeGoogle(ITEMS))

    # pick the same media again but with different ids, plus a new photo and
    # the video that has now finished processing
    again = [dict(item, id="new-" + item["id"]) for item in ITEMS[:3]]
    again.append(picked("new-id-video2", "VID_0002.mp4", "2024-01-02T03:04:06Z", True))
    again.append(picked("id-photo3", "IMG_0003.jpg", "2025-02-03T04:05:06Z"))
    for item in again:
        item["mediaFile"] = dict(item["mediaFile"], baseUrl=CONTENT_HOST + item["id"])
    fake = FakeGoogle(again)
    run_sync(fake)

    downloads = {r.url for r in fake.requests if r.url.startswith(CONTENT_HOST)}
    assert downloads == {
        CONTENT_HOST + "new-id-video2=dv",
        CONTENT_HOST + "id-photo3=d",
    }
    rows = db_rows(root)
    # no duplicate entries or "name (2).jpg" files were created
    # (names are lower cased on case insensitive file systems)
    assert sorted(r["FileName"].lower() for r in rows) == [
        "img_0001.jpg",
        "img_0002.jpg",
        "img_0003.jpg",
        "vid_0001.mp4",
        "vid_0002.mp4",
    ]
    assert all(r["Downloaded"] == 1 for r in rows)
    assert (root / "photos" / "2024" / "01" / "VID_0002.mp4").exists()


def test_same_name_and_time_in_one_session(run_sync):
    """burst shots can share name and timestamp, both must be kept"""
    items = [
        picked("burst-a", "BURST.jpg", "2024-03-03T03:03:03Z"),
        picked("burst-b", "BURST.jpg", "2024-03-03T03:03:03Z"),
    ]
    root = run_sync(FakeGoogle(items))
    folder = root / "photos" / "2024" / "03"
    names = sorted(p.name.lower() for p in folder.iterdir())
    assert names == ["burst (2).jpg", "burst.jpg"]


def test_filters(run_sync):
    root = run_sync(FakeGoogle(ITEMS), "--skip-video", "--start-date", "2024-01-01")
    assert [r["FileName"].lower() for r in db_rows(root)] == ["img_0002.jpg"]


def test_forbidden_not_blacklisted(run_sync):
    fake = FakeGoogle(ITEMS[:2])
    fake.forbidden = {"id-photo1"}
    root = run_sync(fake)
    assert not (root / "photos" / "2023" / "05" / "IMG_0001.jpg").exists()
    bad_ids = root / "gphotos.bad_ids.yaml"
    assert "id-photo1" not in bad_ids.read_text()

    # it works when picked again
    run_sync(FakeGoogle(ITEMS[:2]))
    assert (root / "photos" / "2023" / "05" / "IMG_0001.jpg").exists()


def test_removed_options_rejected(run_sync):
    with pytest.raises(SystemExit):
        run_sync(FakeGoogle(ITEMS), "--album", "Holidays")
    with pytest.raises(SystemExit):
        run_sync(FakeGoogle(ITEMS), "--do-delete")


def test_paging_and_base_url_refresh():
    items = [
        picked("id-{}".format(i), "IMG_{}.jpg".format(i), "2024-01-01T00:00:00Z")
        for i in range(250)
    ]
    fake = FakeGoogle(items, polls_before_set=0)
    api = RestClient(
        "https://photospicker.googleapis.com/$discovery/rest?version=v1",
        fake_session(fake),
    )
    now = [0.0]
    picker = PickerSession(api, max_items=5000, clock=lambda: now[0])
    assert picker.max_items == 2000
    picker.create()
    assert picker.wait_for_selection()
    assert len(picker.list_items()) == 250
    assert fake.list_calls == 3  # 100 + 100 + 50

    assert picker.base_url("id-7") == CONTENT_HOST + "id-7"
    assert fake.list_calls == 3
    now[0] += 51 * 60  # base URLs live for 60 minutes
    picker.base_url("id-7")
    assert fake.list_calls == 6
    assert picker.base_url("not-picked") is None


def test_picker_timeout():
    fake = FakeGoogle([], polls_before_set=10**6)
    api = RestClient(
        "https://photospicker.googleapis.com/$discovery/rest?version=v1",
        fake_session(fake),
    )
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    picker = PickerSession(api, timeout=10, sleep=sleep, clock=lambda: now[0])
    picker.create()
    with pytest.raises(TimeoutError):
        picker.wait_for_selection()


def test_media_parsing():
    media = GooglePhotosMedia(ITEMS[1])
    assert media.id == "id-photo2"
    assert media.orig_name == "IMG_0002.jpg"
    assert media.mime_type == "image/jpeg"
    assert media.create_date.isoformat() == "2024-12-31T23:59:59"
    assert not media.is_video
    assert media.camera_model == "Pixel"
    assert GooglePhotosMedia(ITEMS[2]).is_video


def test_parse_duration():
    assert parse_duration("5s", 1) == 5
    assert parse_duration("1799.5s", 1) == 1799.5
    assert parse_duration(None, 7) == 7
    assert parse_duration("bogus", 7) == 7


def test_old_token_scopes_are_replaced(tmp_path):
    token_file = tmp_path / ".gphotos.token"
    old = {
        "access_token": "x",
        "refresh_token": "y",
        "token_type": "Bearer",
        "expires_at": time.time() + 3600,
        "scope": [
            "https://www.googleapis.com/auth/photoslibrary.readonly",
            "https://www.googleapis.com/auth/photoslibrary.sharing",
        ],
    }
    token_file.write_text(json.dumps(old))
    auth = Authorize([PICKER_SCOPE], token_file, SECRET)
    assert not auth.has_scopes(old)
    assert auth.has_scopes(dict(old, scope=PICKER_SCOPE))

    new = dict(old, scope=[PICKER_SCOPE], access_token="new")
    with mock.patch.object(Authorize, "run_flow", return_value=new) as flow:
        auth.authorize()
    flow.assert_called_once()
    assert auth.session.token["access_token"] == "new"  # type: ignore
    assert json.loads(token_file.read_text())["scope"] == [PICKER_SCOPE]

    # a valid token is reused without a new login
    with mock.patch.object(Authorize, "run_flow") as flow:
        Authorize([PICKER_SCOPE], token_file, SECRET).authorize()
    flow.assert_not_called()


def test_access_token_refresh(tmp_path):
    token_file = tmp_path / ".gphotos.token"
    expired = {
        "access_token": "old",
        "refresh_token": "r",
        "token_type": "Bearer",
        "expires_at": time.time() - 10,
        "scope": [PICKER_SCOPE],
    }
    token_file.write_text(json.dumps(expired))
    auth = Authorize([PICKER_SCOPE], token_file, SECRET)
    auth.authorize()
    fresh = dict(expired, access_token="fresh", expires_at=time.time() + 3600)
    with mock.patch.object(
        auth.session, "refresh_token", return_value=fresh
    ) as refresh:
        assert auth.access_token() == "fresh"
    refresh.assert_called_once()
    assert json.loads(token_file.read_text())["access_token"] == "fresh"
