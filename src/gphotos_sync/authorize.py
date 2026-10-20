import logging
import time
from datetime import timezone
from json import JSONDecodeError, dump, load
from pathlib import Path
from typing import List, Optional

from google_auth_oauthlib.flow import InstalledAppFlow
from requests.adapters import HTTPAdapter
from requests_oauthlib import OAuth2Session
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


# OAuth endpoints given in the Google API documentation
authorization_base_url = "https://accounts.google.com/o/oauth2/v2/auth"
token_uri = "https://www.googleapis.com/oauth2/v4/token"


class Authorize:
    def __init__(
        self,
        scope: List[str],
        token_file: Path,
        secrets_file: Path,
        max_retries: int = 5,
        port: int = 8080,
    ):
        """A very simple class to handle Google API authorization flow
        for the requests library. Includes saving the token and automatic
        token refresh.

        Args:
            scope: list of the scopes for which permission will be granted
            token_file: full path of a file in which the user token will be
            placed. After first use the previous token will also be read in from
            this file
            secrets_file: full path of the client secrets file obtained from
            Google Api Console
        """
        self.max_retries = max_retries
        self.scope: List[str] = scope
        self.token_file: Path = token_file
        self.session = None
        self.token = None
        self.secrets_file = secrets_file
        self.port = port

        try:
            with secrets_file.open("r") as stream:
                all_json = load(stream)
            secrets = all_json["installed"]
            self.client_id = secrets["client_id"]
            self.client_secret = secrets["client_secret"]
            self.redirect_uri = secrets["redirect_uris"][0]
            self.token_uri = secrets["token_uri"]
            self.extra = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }

        except (JSONDecodeError, IOError):
            print("missing or bad secrets file: {}".format(secrets_file))
            exit(1)

    def load_token(self) -> Optional[str]:
        try:
            with self.token_file.open("r") as stream:
                token = load(stream)
        except (JSONDecodeError, IOError):
            return None
        return token

    def save_token(self, token: str):
        with self.token_file.open("w") as stream:
            dump(token, stream)
        try:
            self.token_file.chmod(0o600)
        except (PermissionError,):
            log.warning("Could not change permissions of the token file")

    def has_scopes(self, token: dict) -> bool:
        """check that a saved token was granted all of the scopes we need.
        Tokens issued before the March 2025 Google Photos API changes carry
        the (now removed) photoslibrary scopes and must be replaced."""
        granted = token.get("scope") or []
        if isinstance(granted, str):
            granted = granted.split()
        return set(self.scope).issubset(set(granted))

    def run_flow(self) -> dict:
        flow = InstalledAppFlow.from_client_secrets_file(
            self.secrets_file, scopes=self.scope
        )
        # localhost and bind to 0.0.0.0 always works even in a container.
        flow.run_local_server(open_browser=False, bind_addr="0.0.0.0", port=self.port)

        credentials = flow.credentials
        # Mapping for backward compatibility
        return {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_type": "Bearer",
            "scope": list(credentials.granted_scopes or credentials.scopes or []),
            "expires_at": credentials.expiry.replace(tzinfo=timezone.utc).timestamp(),
        }

    def authorize(self):
        """Initiates OAuth2 authentication and authorization flow"""
        token = self.load_token()

        if token and not self.has_scopes(token):
            log.warning(
                "Saved token %s does not grant the required scopes %s, "
                "requesting a new authorization",
                self.token_file,
                self.scope,
            )
            token = None

        if not token:
            token = self.run_flow()
            if not self.has_scopes(token):
                raise PermissionError(
                    "Authorization did not grant the required scopes {}. Please "
                    "re-run with --new-token and tick all permission boxes on "
                    "the consent screen".format(self.scope)
                )
            self.save_token(token)

        self.session = OAuth2Session(
            self.client_id,
            token=token,
            auto_refresh_url=self.token_uri,
            auto_refresh_kwargs=self.extra,
            token_updater=self.save_token,
        )

        # set up the retry behaviour for the authorized session
        retries = Retry(
            total=self.max_retries,
            backoff_factor=5,
            status_forcelist=[500, 502, 503, 504, 429],
            allowed_methods=frozenset(["GET", "POST", "DELETE"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        # apply the retry behaviour to our session by replacing the default HTTPAdapter
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def access_token(self, min_valid_seconds: int = 300) -> str:
        """return a bearer access token that is valid for at least
        min_valid_seconds, refreshing it if required. Used for media downloads
        which (with the Picker API) must carry an Authorization header"""
        token = self.session.token  # type: ignore
        if token.get("expires_at", 0) - time.time() < min_valid_seconds:
            token = self.session.refresh_token(  # type: ignore
                self.token_uri, **self.extra
            )
            self.save_token(token)
        return token["access_token"]
