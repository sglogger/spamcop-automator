"""Recognising SpamCop pages and extracting links and forms from them.

SpamCop has no API, so everything here works on the HTML the site returns.
The text markers are the ones spamcop.pl v3.6 matches on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from bs4 import BeautifulSoup, Tag

REPORT_LINK_TEXT = "Report Now"
SEND_BUTTON_VALUE = "Send Spam Report(s) Now"
SEND_FORM_NAME = "sendreport"


@dataclass(frozen=True)
class Marker:
    pattern: re.Pattern[str]
    message: str


def _m(pattern: str, message: str) -> Marker:
    return Marker(re.compile(pattern), message)


# Problems with the site or the account: nothing can be processed.
LOGIN_ERRORS = (
    _m(r"Login.failed", "Login failed. Check your username and password (both are case sensitive)."),
    _m(
        r"an.error.occurred.while.processing.this.directive",
        "SpamCop server problem (error processing directive). Try again in a minute or two.",
    ),
    _m(r"500.Internal.Server.Error", "SpamCop returned 500 Internal Server Error. Try again in a minute or two."),
)

# Interstitial shown to free accounts before a report page.
AD_DELAY = re.compile(r"subscribe.to.remove")

# The account is blocked until the user fixes it by hand; going on is pointless.
FATAL = (
    _m(
        r"Bounce.error",
        "Your email address has returned a bounce. Log in to SpamCop and reset the "
        "bounce flag; reports cannot be confirmed until then.",
    ),
)

# This one report cannot be sent; move on to the next. Order matters.
SKIP = (
    _m(r"resolved.this.issue", "ISP resolved this issue, no report needed"),
    _m(
        r"Mailhost.configuration.problem",
        "Mailhost configuration problem: register every address where you receive spam",
    ),
    _m(r"No.source.IP.address.found", "No source IP address found, cannot proceed"),
    _m(r"No.blank.line.delineating", "No blank line delineating headers from body"),
    _m(r"No.valid.email.addresses", "No valid email addresses to report to"),
    _m(r"spam.have.already.been.sent", "Reports regarding this spam have already been sent"),
    _m(r"Reports.have.already.been.sent", "Reports have already been sent"),
    _m(r"this.email.is.too.old.to", "Email is too old to report (must be reported within 2 days)"),
    _m(r"No.body.text.provided", "No body text provided; spam must have body text"),
    _m(r"Failed.to.load.spam.header", "Failed to load spam header"),
)

SENDABLE = re.compile(r"Send.Spam.Report")

# Notes on a report that can still be sent.
WARNINGS = (
    _m(r"Possible.forgery", "Possible forgery: receiving system not associated with any of your mailhosts"),
    _m(r"is.not.a.routeable.IP.address", "Parsing found a non-routeable IP address"),
    _m(r"ISP.does.not.wish.to.receive.reports.regarding", "An ISP does not wish to receive reports"),
    _m(r"Too.many.links", "The spam contains too many links"),
)

_FORWARD_ADDRESS = re.compile(r'spam.to:.<a.href="mailto:([^"]+)"', re.IGNORECASE)


def first_match(markers: tuple[Marker, ...], html: str) -> str | None:
    for marker in markers:
        if marker.pattern.search(html):
            return marker.message
    return None


def all_matches(markers: tuple[Marker, ...], html: str) -> list[str]:
    return [m.message for m in markers if m.pattern.search(html)]


@dataclass
class Page:
    url: str
    html: str
    encoding: str = "iso-8859-1"

    @cached_property
    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.html, "html.parser")

    def report_link(self) -> str | None:
        """href of the first "Report Now" link, if the page has one."""
        for a in self.soup.find_all("a", href=True):
            if a.get_text(" ", strip=True).lower() == REPORT_LINK_TEXT.lower():
                return a["href"]
        return None

    def forward_addresses(self) -> list[str]:
        return _FORWARD_ADDRESS.findall(self.html)

    def has_login_form(self) -> bool:
        return self.login_form() is not None

    def login_form(self) -> Tag | None:
        for form in self.soup.find_all("form"):
            if form.find("input", attrs={"name": "username"}) and form.find(
                "input", attrs={"name": "password"}
            ):
                return form
        return None

    def send_form(self) -> Tag | None:
        form = self.soup.find("form", attrs={"name": SEND_FORM_NAME})
        if form is None:
            # Fall back to whichever form carries the send button.
            button = find_send_button(self.soup)
            form = button.find_parent("form") if button else None
        return form


def find_send_button(scope: Tag) -> Tag | None:
    buttons = scope.find_all("input", attrs={"type": re.compile("^submit$", re.I)})
    for button in buttons:
        if button.get("value") == SEND_BUTTON_VALUE:
            return button
    for button in buttons:
        if SENDABLE.search(button.get("value") or ""):
            return button
    return None


def form_payload(
    form: Tag, overrides: dict[str, str] | None = None, click: Tag | None = None
) -> list[tuple[str, str]]:
    """The name/value pairs a browser would submit for ``form``.

    ``click`` is the submit button being pressed; other submit buttons are
    left out, as a browser would.
    """
    overrides = dict(overrides or {})
    pairs: list[tuple[str, str]] = []

    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        if el.name == "input":
            kind = (el.get("type") or "text").lower()
            if kind in {"submit", "image", "button", "reset", "file"}:
                continue
            if kind in {"checkbox", "radio"}:
                if el.has_attr("checked"):
                    pairs.append((name, el.get("value", "on")))
                continue
            pairs.append((name, overrides.pop(name, el.get("value", ""))))
        elif el.name == "textarea":
            pairs.append((name, overrides.pop(name, el.get_text())))
        else:  # select
            options = el.find_all("option")
            chosen = [o for o in options if o.has_attr("selected")]
            if not chosen and options and not el.has_attr("multiple"):
                chosen = options[:1]
            for option in chosen:
                pairs.append((name, option.get("value", option.get_text())))

    pairs.extend(overrides.items())
    if click is not None and click.get("name"):
        pairs.append((click["name"], click.get("value", "")))
    return pairs
