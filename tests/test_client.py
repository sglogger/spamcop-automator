"""End-to-end tests against a fake SpamCop served through httpx.MockTransport."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from spamcop_automator.client import SpamCopClient, SpamCopError

BASE = "https://spamcop.test/"

# Markup copied from the real login form (unquoted attributes and all).
LOGIN_PAGE = """<html><body>
<form method="post" action="/mcgi">
<input size=15 type="text" name="username">
<input size=8 type="password" name="password">
<input type="submit" value="Login">
<input type="hidden" name="duration" value="+12h">
<input type="hidden" name="action" value="cookielogin">
<input type="hidden" name="returnurl" value="/">
</form></body></html>"""


def home(next_id: str | None) -> str:
    link = f'<a href="/sc?id={next_id}">Report Now</a>' if next_id else "No unreported spam"
    return (
        '<html><body>Forward your spam to: <a href="mailto:submit.abc@spam.spamcop.net">'
        f"submit.abc@spam.spamcop.net</a><br>{link}</body></html>"
    )


def sendable(report_id: str, extra: str = "") -> str:
    return f"""<html><body>{extra}
<form action="/sc" method="post" name="sendreport">
<input type="hidden" name="action" value="submit">
<input type="hidden" name="crc" value="crc-{report_id}">
<input type="hidden" name="reports" value="2">
<input type="checkbox" name="send1" value="on" checked>
<input type="checkbox" name="send2" value="on">
<textarea name="notes"></textarea>
<input type="submit" name="submit1" value="Send Spam Report(s) Now">
<input type="submit" name="cancel" value="Cancel">
</form></body></html>"""


class FakeSpamCop:
    """Queue of pending reports: (id, html of the report page)."""

    def __init__(self, reports: list[tuple[str, str]], *, password: str = "secret") -> None:
        self.reports = list(reports)
        self.password = password
        self.logged_in = False
        self.submissions: list[dict[str, list[str]]] = []
        self.ad_views_left = 0
        self.sticky = False  # skipped reports stay at the head of the queue

    @property
    def next_id(self) -> str | None:
        return self.reports[0][0] if self.reports else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/" and request.method == "GET":
            return self._html(home(self.next_id) if self.logged_in else LOGIN_PAGE)

        if path == "/mcgi" and request.method == "POST":
            form = parse_qs(request.content.decode())
            assert form["action"] == ["cookielogin"]
            if form["password"] != [self.password]:
                return self._html("<html>Login failed" + LOGIN_PAGE)
            self.logged_in = True
            return httpx.Response(302, headers={"Location": "/"})

        assert self.logged_in, "request without a session"

        if path == "/sc" and request.method == "GET":
            if self.ad_views_left:
                self.ad_views_left -= 1
                return self._html("<html>Please wait - subscribe to remove this delay</html>")
            report_id = request.url.params["id"]
            html = dict(self.reports)[report_id]
            if "sendreport" not in html and not self.sticky:
                self.reports = [r for r in self.reports if r[0] != report_id]
                html += home(self.next_id)
            return self._html(html)

        if path == "/sc" and request.method == "POST":
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            self.submissions.append(form)
            crc = form["crc"][0]
            self.reports = [r for r in self.reports if f"crc-{r[0]}" != crc]
            return self._html("<html>Reports sent." + home(self.next_id))

        return httpx.Response(404)

    @staticmethod
    def _html(body: str) -> httpx.Response:
        return httpx.Response(200, content=body.encode("iso-8859-1"),
                              headers={"Content-Type": "text/html; charset=iso-8859-1"})


def make_client(site: FakeSpamCop, **kwargs) -> SpamCopClient:
    kwargs.setdefault("sleep", lambda _s: None)
    return SpamCopClient(
        "user", kwargs.pop("password", "secret"), base_url=BASE,
        transport=httpx.MockTransport(site), **kwargs,
    )


def test_sends_all_pending_reports():
    site = FakeSpamCop([("a", sendable("a")), ("b", sendable("b"))])
    summary = make_client(site).run()

    assert (summary.sent, summary.skipped, summary.unknown) == (2, 0, 0)
    assert site.reports == []
    first = site.submissions[0]
    assert first["crc"] == ["crc-a"]
    assert first["submit1"] == ["Send Spam Report(s) Now"]
    assert first["send1"] == ["on"]
    assert "send2" not in first  # unchecked checkbox
    assert "cancel" not in first  # the button that was not clicked


def test_nothing_pending():
    site = FakeSpamCop([])
    summary = make_client(site).run()
    assert summary.handled == 0
    assert site.submissions == []


def test_login_failure():
    site = FakeSpamCop([("a", sendable("a"))])
    with pytest.raises(SpamCopError, match="Login failed"):
        make_client(site, password="wrong").run()


def test_skips_unsendable_and_continues():
    site = FakeSpamCop([
        ("old", "<html>Sorry, this email is too old to file a spam report"),
        ("a", sendable("a")),
        ("resolved", "<html>ISP has resolved this issue"),
    ])
    summary = make_client(site).run()
    assert (summary.sent, summary.skipped) == (1, 2)
    assert [s["crc"] for s in site.submissions] == [["crc-a"]]


def test_waits_out_delay_page():
    site = FakeSpamCop([("a", sendable("a"))])
    site.ad_views_left = 3
    naps: list[float] = []
    summary = make_client(site, sleep=naps.append, ad_delay=3).run()
    assert summary.sent == 1
    assert naps == [3, 3, 3]


def test_dry_run_sends_nothing():
    site = FakeSpamCop([("a", sendable("a")), ("b", sendable("b"))])
    summary = make_client(site, dry_run=True).run()
    assert site.submissions == []
    assert summary.sent == 0
    assert summary.would_send == 1
    assert summary.stopped_early  # same report offered again


def test_stops_instead_of_looping_on_a_stuck_report():
    site = FakeSpamCop([("stuck", "<html>No source IP address found")])
    site.sticky = True
    summary = make_client(site).run()
    assert summary.skipped == 1
    assert "same report" in summary.stopped_early


def test_bounce_flag_aborts():
    site = FakeSpamCop([("a", "<html>Bounce error"), ("b", sendable("b"))])
    with pytest.raises(SpamCopError, match="bounce"):
        make_client(site).run()
    assert site.submissions == []


def test_unknown_page_is_left_alone():
    site = FakeSpamCop([("x", "<html>Something entirely new")])
    site.sticky = True
    summary = make_client(site).run()
    assert summary.unknown == 1
    assert site.submissions == []


def test_max_reports():
    site = FakeSpamCop([(i, sendable(i)) for i in "abc"])
    summary = make_client(site, max_reports=2).run()
    assert summary.sent == 2
    assert len(site.reports) == 1
    assert "limit" in summary.stopped_early


def test_server_error_page_aborts():
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="[an error occurred while processing this directive]")

    client = SpamCopClient("u", "p", base_url=BASE, transport=httpx.MockTransport(broken))
    with pytest.raises(SpamCopError, match="server problem"):
        client.run()
