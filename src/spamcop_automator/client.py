"""Drives a SpamCop session: log in, then confirm each pending report."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urljoin

import httpx
from bs4 import Tag

from . import __version__, pages
from .pages import Page

log = logging.getLogger("spamcop")

BASE_URL = "https://www.spamcop.net/"
USER_AGENT = f"spamcop-automator/{__version__}"


class SpamCopError(Exception):
    """The run cannot continue (login failure, site error, blocked account)."""


@dataclass
class Summary:
    sent: int = 0
    skipped: int = 0
    unknown: int = 0
    would_send: int = 0  # dry run only
    stopped_early: str | None = None
    report_urls: list[str] = field(default_factory=list)

    @property
    def handled(self) -> int:
        return self.sent + self.skipped + self.unknown + self.would_send


class SpamCopClient:
    def __init__(
        self,
        username: str,
        password: str,
        *,
        base_url: str = BASE_URL,
        dry_run: bool = False,
        max_reports: int | None = None,
        ad_delay: float = 3.0,
        ad_max_waits: int = 40,
        timeout: float = 60.0,
        dump_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._username = username
        self._password = password
        self._base_url = base_url
        self._dry_run = dry_run
        self._max_reports = max_reports
        self._ad_delay = ad_delay
        self._ad_max_waits = ad_max_waits
        self._dump_dir = dump_dir
        self._dump_count = 0
        self._sleep = sleep
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=timeout,
            transport=transport or httpx.HTTPTransport(retries=2),
        )

    def __enter__(self) -> SpamCopClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- HTTP ---------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: object) -> Page:
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise SpamCopError(f"Request to SpamCop failed: {exc}") from exc
        page = Page(str(response.url), response.text, response.encoding or "iso-8859-1")
        self._dump(page)
        problem = pages.first_match(pages.LOGIN_ERRORS[1:], page.html)
        if problem:
            raise SpamCopError(problem)
        if response.is_error:
            raise SpamCopError(f"SpamCop returned HTTP {response.status_code} for {page.url}")
        return page

    def _get(self, url: str) -> Page:
        return self._request("GET", url)

    def _submit(self, page: Page, form: Tag, pairs: list[tuple[str, str]]) -> Page:
        url = urljoin(page.url, form.get("action") or page.url)
        if (form.get("method") or "get").lower() == "post":
            # Encode in the page's charset, as a browser does.
            body = urlencode(pairs, encoding=page.encoding, errors="replace")
            return self._request(
                "POST",
                url,
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        return self._request("GET", url, params=pairs)

    def _dump(self, page: Page) -> None:
        if self._dump_dir is None:
            return
        self._dump_dir.mkdir(parents=True, exist_ok=True)
        self._dump_count += 1
        path = self._dump_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{self._dump_count:03d}.html"
        path.write_text(f"<!-- {page.url} -->\n{page.html}", encoding="utf-8")
        path.chmod(0o600)

    # -- Flow ---------------------------------------------------------------

    def login(self) -> Page:
        home = self._get(self._base_url)
        form = home.login_form()
        if form is None:
            raise SpamCopError(f"No login form found on {home.url}; the site layout may have changed.")
        pairs = pages.form_payload(
            form, overrides={"username": self._username, "password": self._password}
        )
        page = self._submit(home, form, pairs)
        problem = pages.first_match(pages.LOGIN_ERRORS, page.html)
        if problem:
            raise SpamCopError(problem)
        if page.has_login_form():
            raise SpamCopError(
                "Still seeing the login form after logging in; SpamCop did not accept the session."
            )
        return page

    def run(self) -> Summary:
        """Log in and confirm pending reports until none are left."""
        summary = Summary()
        page = self.login()
        for address in page.forward_addresses():
            log.info("Forward your spam to: %s", address)

        link = page.report_link()
        if link is None:
            return summary
        log.info("Pending report(s) found, processing")

        unsent: set[str] = set()
        while link is not None:
            if self._max_reports is not None and summary.handled >= self._max_reports:
                summary.stopped_early = f"reached the limit of {self._max_reports} report(s)"
                break

            page = self._wait_out_ad(self._get(urljoin(page.url, link)))
            if page.url in unsent:
                # SpamCop keeps offering a report we could not send; stop
                # rather than loop on it forever.
                summary.stopped_early = f"SpamCop keeps offering the same report ({page.url})"
                break

            report_url = page.url
            sent, page = self._handle_report(page, summary)
            summary.report_urls.append(report_url)
            if not sent:
                unsent.add(report_url)

            link = page.report_link()
            if link is None:
                # The page we ended on may simply not show the link.
                page = self._get(self._base_url)
                link = page.report_link()

        return summary

    def _wait_out_ad(self, page: Page) -> Page:
        waits = 0
        while pages.AD_DELAY.search(page.html):
            if waits >= self._ad_max_waits:
                raise SpamCopError(f"Still on the delay page after {waits} tries: {page.url}")
            waits += 1
            log.debug("Waiting out the free-account delay page")
            self._sleep(self._ad_delay)
            page = self._get(page.url)
        return page

    def _handle_report(self, page: Page, summary: Summary) -> tuple[bool, Page]:
        """Deal with one report page. Returns (was it sent, page we are on now)."""
        fatal = pages.first_match(pages.FATAL, page.html)
        if fatal:
            raise SpamCopError(fatal)

        reason = pages.first_match(pages.SKIP, page.html)
        if reason:
            summary.skipped += 1
            log.warning("Skipped: %s (%s)", reason, page.url)
            return False, page

        form = page.send_form() if pages.SENDABLE.search(page.html) else None
        button = pages.find_send_button(form) if form is not None else None
        if form is None or button is None:
            summary.unknown += 1
            log.error(
                "Unrecognised report page, left untouched (%s). "
                "Run with --dump-dir to capture it.",
                page.url,
            )
            return False, page

        pairs = pages.form_payload(form, click=button)
        count = dict(pairs).get("reports", "?")
        notes = pages.all_matches(pages.WARNINGS, page.html)

        if self._dry_run:
            summary.would_send += 1
            log.info("Would send %s report(s): %s", count, page.url)
            for note in notes:
                log.info("  note: %s", note)
            return False, page

        result = self._submit(page, form, pairs)
        summary.sent += 1
        log.info("Sent %s report(s): %s", count, page.url)
        for note in notes:
            log.info("  note: %s", note)
        return True, result
