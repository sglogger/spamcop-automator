# spamcop-automator

Confirms all pending spam reports on [SpamCop.net](https://www.spamcop.net/) for you.

After you forward spam to your SpamCop submission address, SpamCop parses each
message and then waits for you to press "Send Spam Report(s) Now" on every
single one. This tool logs in, walks through the pending reports and presses
that button.

It is a Python port of [spamcop.pl](https://github.com/Monter/spamcop.pl) by
Monter ([project page](https://dev.techlog.pl/projects/Linux/spamcop/)).

## Install

Requires Python 3.10 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install .
```

Or, with pipx: `pipx install .`

## Credentials

Either set environment variables:

```sh
export SPAMCOP_USERNAME='you@example.com'
export SPAMCOP_PASSWORD='...'
```

or create `~/.config/spamcop-automator/config.ini` (and `chmod 600` it):

```ini
[spamcop]
username = you@example.com
password = ...
```

Environment variables take precedence. Use `--config PATH` for a file elsewhere.

## Usage

```sh
spamcop-automator              # confirm everything that is pending
spamcop-automator --dry-run    # log in and look, send nothing
spamcop-automator --quiet      # only print skipped reports and errors
```

| Option | Meaning |
| --- | --- |
| `-n`, `--dry-run` | Log in and inspect the next pending report without sending it. |
| `-m N`, `--max-reports N` | Stop after N reports. |
| `-q`, `--quiet` | Print only skipped reports and errors. |
| `-v`, `--verbose` | Debug output. |
| `--log-file PATH` | Also append the log to a file. |
| `--dump-dir DIR` | Save every fetched page for debugging. The pages contain account data. |
| `-c PATH`, `--config PATH` | Config file to read credentials from. |

Exit status: `0` success (including "nothing pending"), `1` login or site
failure or an unrecognised report page, `2` missing credentials.

### Cron

Only one instance runs at a time; a second one exits immediately. SpamCop needs
a little while to parse submitted spam, so running every few minutes works well:

```cron
*/10 * * * * /path/to/.venv/bin/spamcop-automator --quiet --log-file "$HOME/spamcop.log"
```

## What it does with each report

- **Sends it** when SpamCop offers the send form. Notes such as "possible
  forgery" or "too many links" are logged but do not block sending.
- **Skips it** when SpamCop says it cannot be reported: ISP resolved the issue,
  no source IP found, reports already sent, email too old, no body text,
  mailhost configuration problem, and similar.
- **Stops the run** when your account has the bounce flag set (you have to
  clear it on the website), when SpamCop reports a server error, or when
  SpamCop keeps offering a report that could not be sent.
- **Leaves it alone** and exits with status 1 when the page is not recognised.
  Run again with `--dump-dir` to capture the page.

Free accounts see a "please wait" page before each report; the tool waits it out.

## Notes

- SpamCop has no API. This tool reads the website's HTML, so a redesign on
  their side can break it.
- Reports are sent exactly as SpamCop pre-selects them, without review. You
  are responsible for what you report.
- Free SpamCop accounts are limited in how many reports they may send per day.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests run against a fake SpamCop; they never contact the real site.

## License

GPL-2.0, the same as the original spamcop.pl. See [LICENSE](LICENSE).
