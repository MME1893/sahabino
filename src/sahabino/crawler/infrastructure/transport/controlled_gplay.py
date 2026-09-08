from __future__ import annotations

from collections.abc import Mapping

from sahabino.crawler.application.ports.transport import HttpTransport, TransportResponse


class ControlledGPlayHttpClient:
    """The only HTTP surface exposed to the pinned gplay parser/scraper components."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        base_url: str,
        app_details_endpoint: str,
        batch_execute_endpoint: str,
        headers: Mapping[str, str],
    ) -> None:
        self._transport = transport
        self._base_url = base_url
        self._app_endpoint = app_details_endpoint
        self._batch_endpoint = batch_execute_endpoint
        self.headers = dict(headers)

    @classmethod
    def from_gplay_config(cls, transport: HttpTransport) -> ControlledGPlayHttpClient:
        from gplay_scraper.config import Config

        return cls(
            transport,
            base_url=Config.PLAY_STORE_BASE_URL,
            app_details_endpoint=Config.APP_DETAILS_ENDPOINT,
            batch_execute_endpoint=Config.BATCHEXECUTE_ENDPOINT,
            headers=Config.get_headers(),
        )

    def fetch_app_page(self, app_id: str, lang: str, country: str) -> str:
        response = self._request(
            "GET",
            f"{self._base_url}{self._app_endpoint}",
            params={"id": app_id, "hl": lang, "gl": country},
        )
        return response.text

    def fetch_app_page_no_locale(self, app_id: str) -> str:
        response = self._request(
            "GET", f"{self._base_url}{self._app_endpoint}", params={"id": app_id}
        )
        return response.text

    def fetch_reviews_batch(
        self,
        app_id: str,
        lang: str,
        country: str,
        sort: int,
        batch_count: int,
        token: str | None = None,
    ) -> str:
        url = f"{self._base_url}{self._batch_endpoint}"
        import json
        from urllib.parse import urlencode

        page = [batch_count, None, token] if token else [batch_count]
        review_args = [
            None,
            [2, sort, page, None, [None, None, None, None, None, None, None, None, None]],
            [app_id, 7],
        ]
        data = urlencode(
            {
                "f.req": json.dumps(
                    [[["oCPfdb", json.dumps(review_args), None, "generic"]]],
                    separators=(",", ":"),
                )
            }
        )
        response = self._request(
            "POST",
            url,
            params={"hl": lang, "gl": country},
            headers={**self.headers, "content-type": "application/x-www-form-urlencoded"},
            data=data,
        )
        return response.text

    def close(self) -> None:
        self._transport.close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        return self._transport.request(
            method,
            url,
            headers=headers or self.headers,
            data=data,
            params=params,
        )
