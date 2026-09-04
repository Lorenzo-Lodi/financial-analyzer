"""
Fetch the "mid" price (average of High and Low) for a list of ETFs
identified by ISIN, using Yahoo Finance via the yfinance package, both for
a target date and for several lookback points in the past (3 months,
6 months, 1 year, 2 years, 3 years, 4 years, 5 years).

Yahoo Finance does not index instruments by ISIN natively, so each ISIN is
mapped below to a known-working Yahoo ticker symbol (confirmed by trial).

Confirmed tickers (as of 2026-08-10):

    ISIN            Name                                            Ticker
    --------------  ----------------------------------------------  -------
    IE0032077012    Invesco EQQQ Nasdaq-100 UCITS ETF                EQQQ.L
    IE00B5KQNG97    HSBC S&P 500 UCITS ETF USD                       H4ZF.DE
    IE00B53QDK08    iShares MSCI Japan UCITS ETF USD (Acc)           SXR5.DE
    DE000A0F5UJ7    iShares STOXX Europe 600 Banks UCITS ETF (DE)    EXV1.DE
    IE00B3RBWM25    Vanguard FTSE All-World UCITS ETF (USD) Dist.    VWRL.L
    IE00BKM4GZ66    iShares Core MSCI EM IMI UCITS ETF (Acc)         EIMI.L
    IE00B4K48X80    iShares Core MSCI Europe UCITS ETF EUR (Acc)     EUNK.DE
    LU1681047236    Amundi Core EURO STOXX 50 UCITS ETF EUR (Acc)    V50A.DE
    JE00B1VS3770    WisdomTree Physical Gold (ETC)                  PHAU.L

Note: EQQQ.L, VWRL.L, EIMI.L and PHAU.L trade on the LSE in GBX (pence), not
EUR/USD - convert accordingly before mixing with the other rows.

The target date is today. If today has no data (weekend/holiday, or the
market hasn't closed yet), the script falls back to the most recent day
that does have data.

Lookback dates are computed as target_date - round(months * 30.42) days.
If a ticker has no data on that exact date (weekend/holiday), the mid from
the next available trading day is used instead. Column headers show years
ago (0.00, 0.25, 0.50, 1.00, 2.00, 3.00, 4.00, 5.00) plus, in the HTML
report, the nominal lookback date (YYYY-MM) as a sub-header - this is
still the nominal date, not the actual resolved trading day, which can
differ per ticker.

Every value is normalized (divided) by the oldest lookback value present
(currently 5 years ago, i.e. max(LOOKBACK_MONTHS)), so that column reads
1.0 for every row and the others show growth relative to the start of the
window - a standard "rebased index" reading. Note this reference point is
dynamic: extending LOOKBACK_MONTHS with a more distant point will shift the
anchor and change every value already in the table.

The summary is printed to the console and also written to
etf_values.csv and etf_values.html.

etf_values.html additionally includes a second table: the annualized
return from each lookback date to today, i.e. (today / then) ** (1 /
years_ago) - 1 - the standard trailing-return reading ("if held from then
to now, what annualized return would that be"). The 0.00 (today) column
has no such return and shows a dash.

Downloaded OHLC data is cached locally in a SQLite database
(etf_price_cache.db) so repeat runs don't re-fetch unchanged history.
Rows for closed trading days (before today) are cached permanently
(is_definitive=1). A row for today is cached as provisional
(is_definitive=0) and is still reused by later runs on the same day (its
imprecision doesn't matter for long-term trend tracking) - but once a new
day starts, that row is stale (it now represents a past, closed day) and
gets re-fetched once to pick up its true final High/Low, after which it's
marked definitive and never re-fetched again.

Install dependency first:
    pip install yfinance --break-system-packages   # (or just `pip install yfinance`)
"""

import base64
import html
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import yfinance as yf
import pandas as pd

# ISIN -> Yahoo ticker symbol.
TICKERS = {
    "IE0032077012": "EQQQ.L",
    "IE00B5KQNG97": "H4ZF.DE",
    "IE00B53QDK08": "SXR5.DE",
    "DE000A0F5UJ7": "EXV1.DE",
    "IE00B3RBWM25": "VWRL.L",
    "IE00BKM4GZ66": "EIMI.L",
    "IE00B4K48X80": "EUNK.DE",
    "LU1681047236": "V50A.DE",
    "JE00B1VS3770": "PHAU.L",
}

NAMES = {
    "IE0032077012": "Invesco EQQQ Nasdaq-100 UCITS ETF",
    "IE00B5KQNG97": "HSBC S&P 500 UCITS ETF USD",
    "IE00B53QDK08": "iShares MSCI Japan UCITS ETF USD (Acc)",
    "DE000A0F5UJ7": "iShares STOXX Europe 600 Banks UCITS ETF (DE)",
    "IE00B3RBWM25": "Vanguard FTSE All-World UCITS ETF (USD) Dist.",
    "IE00BKM4GZ66": "iShares Core MSCI EM IMI UCITS ETF (Acc)",
    "IE00B4K48X80": "iShares Core MSCI Europe UCITS ETF EUR (Acc)",
    "LU1681047236": "Amundi Core EURO STOXX 50 UCITS ETF EUR (Acc)",
    "JE00B1VS3770": "WisdomTree Physical Gold (ETC)",
}

# Terms highlighted with a colored <span> inside the "name" cell of the HTML
# report (see highlight_terms/render_name below). Manually maintained, like
# NAMES itself - add an entry here whenever a future ETF introduces a new
# company/currency not already listed.
COMPANIES = ["Invesco", "HSBC", "iShares", "Vanguard", "Amundi", "WisdomTree"]
FUND_TYPES = ["UCITS ETF", "ETC"]
CURRENCIES = ["USD", "EUR"]
ACC_DIST_TERMS = ["Acc", "Dist"]

# Fund reference data shown in the "Fund details" table (and, for FUND_SIZE/
# INDEX_DESCRIPTION, also reused in the hover tooltip on the name cell).
# Manually curated, like TICKERS/NAMES above. Source: onlyetf.com, Sep 2026 -
# see FUND_DETAILS_SOURCE_NOTE below.
FUND_SIZE = {
    "IE0032077012": "$11.7B",
    "IE00B5KQNG97": "$8.6B",
    "IE00B53QDK08": "$1.5B",
    "DE000A0F5UJ7": "$3.6B",
    "IE00B3RBWM25": "$23.4B",
    "IE00BKM4GZ66": "$38.7B",
    "IE00B4K48X80": "$15.9B",
    "LU1681047236": "$4.1B",
    "JE00B1VS3770": "$6.3B",
}

INDEX_DESCRIPTION = {
    "IE0032077012": "Nasdaq 100 (a selection of 100 stocks chosen from "
    "among non-financial stocks listed on the NASDAQ stock exchange).",
    "IE00B5KQNG97": "S&P 500 (500 largest US stocks).",
    "IE00B53QDK08": "MSCI Japan (leading Japanese stocks).",
    "DE000A0F5UJ7": "STOXX Europe 600 Banks (European banking sector).",
    "IE00B3RBWM25": "FTSE All-World (stocks from developed and emerging "
    "countries worldwide).",
    "IE00BKM4GZ66": "MSCI Emerging Markets Investable Market (IMI) (stocks "
    "from emerging markets worldwide).",
    "IE00B4K48X80": "MSCI Europe (large and mid-cap stocks from European "
    "developed countries).",
    "LU1681047236": "EURO STOXX 50 (50 largest companies in the eurozone).",
    "JE00B1VS3770": "Spot price of gold in US Dollar.",
}

CURRENCY = {
    "IE0032077012": "USD",
    "IE00B5KQNG97": "USD",
    "IE00B53QDK08": "USD",
    "DE000A0F5UJ7": "EUR",
    "IE00B3RBWM25": "USD",
    "IE00BKM4GZ66": "USD",
    "IE00B4K48X80": "EUR",
    "LU1681047236": "EUR",
    "JE00B1VS3770": "USD",
}

POLICY = {
    "IE0032077012": "Distr.",
    "IE00B5KQNG97": "Distr.",
    "IE00B53QDK08": "Acc.",
    "DE000A0F5UJ7": "Distr.",
    "IE00B3RBWM25": "Distr.",
    "IE00BKM4GZ66": "Acc.",
    "IE00B4K48X80": "Acc.",
    "LU1681047236": "Acc.",
    "JE00B1VS3770": "Acc.",
}

REPLICATION = {
    "IE0032077012": "Phys",
    "IE00B5KQNG97": "Phys",
    "IE00B53QDK08": "Phys",
    "DE000A0F5UJ7": "Phys",
    "IE00B3RBWM25": "Phys",
    "IE00BKM4GZ66": "Phys",
    "IE00B4K48X80": "Phys",
    "LU1681047236": "Phys",
    "JE00B1VS3770": "Phys",
}

TER = {
    "IE0032077012": "0.30%",
    "IE00B5KQNG97": "0.09%",
    "IE00B53QDK08": "0.12%",
    "DE000A0F5UJ7": "0.47%",
    "IE00B3RBWM25": "0.14%",
    "IE00BKM4GZ66": "0.18%",
    "IE00B4K48X80": "0.12%",
    "LU1681047236": "0.09%",
    "JE00B1VS3770": "0.39%",
}

HOLDINGS = {
    "IE0032077012": "103",
    "IE00B5KQNG97": "502",
    "IE00B53QDK08": "168",
    "DE000A0F5UJ7": "57",
    "IE00B3RBWM25": "3758",
    "IE00BKM4GZ66": "2991",
    "IE00B4K48X80": "398",
    "LU1681047236": "56",
    "JE00B1VS3770": "-",
}

# ISO format (YYYY-MM-DD) - source dates were given as DD/MM/YYYY.
START_DATE = {
    "IE0032077012": "2002-12-02",
    "IE00B5KQNG97": "2010-05-14",
    "IE00B53QDK08": "2010-01-11",
    "DE000A0F5UJ7": "2001-04-25",
    "IE00B3RBWM25": "2012-05-22",
    "IE00BKM4GZ66": "2014-05-30",
    "IE00B4K48X80": "2009-09-25",
    "LU1681047236": "2008-09-16",
    "JE00B1VS3770": "2007-04-24",
}

FUND_DETAILS_SOURCE_NOTE = "Reference values from justetf.com, September 2026"

DAYS_PER_MONTH = 30.42
LOOKBACK_MONTHS = [3, 6, 12, 24, 36, 48, 60]
MAX_FORWARD_FILL_DAYS = 14
MAX_BACKWARD_FILL_DAYS = 10

CACHE_DB_PATH = "etf_price_cache.db"

# Cell text colors for the HTML report: green if the ratio increased versus
# the previous (older) date, red otherwise, black for the oldest column
# (which has no older date to compare against and is always 1.0).
COLOR_INCREASE = "#1e7e34"
COLOR_DECREASE = "#c0392b"
COLOR_NEUTRAL = "#000000"

HTML_STYLE = """
body { font-family: Arial, Helvetica, sans-serif; font-size: 18px; }
table { border-collapse: collapse; margin-top: 0.5em; }
th, td { border: 1px solid #bbb; padding: 6px 12px; text-align: center; }
th { background-color: #f0f0f0; }
th.date-row { font-weight: normal; font-size: 0.8em; color: #666; }
td:nth-child(2) { text-align: left; }
td:nth-child(1), th:nth-child(1) { min-width: 146px; }
td:nth-child(2), th:nth-child(2) { min-width: 420px; }
.company-name { color: #2979ff; }
.fund-type    { color: #d500f9; }
.currency     { color: #00bfa5; }
.acc-dist     { color: #ff6d00; }
summary { list-style: none; cursor: pointer; display: inline; }
summary::-webkit-details-marker { display: none; }
summary::after { content: "▸"; margin-left: 6px; color: #666; }
details[open] summary::after { content: "▾"; }
table.fund-details { table-layout: fixed; }
table.fund-details td, table.fund-details th { overflow-wrap: break-word; }
table.fund-details th:nth-child(1), table.fund-details td:nth-child(1) { width: 146px; }
table.fund-details th:nth-child(2), table.fund-details td:nth-child(2) { width: 420px; }
table.fund-details th:nth-child(3), table.fund-details td:nth-child(3) { width: 70px; }
table.fund-details th:nth-child(4), table.fund-details td:nth-child(4) { width: 100px; }
table.fund-details th:nth-child(5), table.fund-details td:nth-child(5) { width: 90px; }
table.fund-details th:nth-child(6), table.fund-details td:nth-child(6) { width: 70px; }
table.fund-details th:nth-child(7), table.fund-details td:nth-child(7) { width: 70px; }
table.fund-details th:nth-child(8), table.fund-details td:nth-child(8) { width: 90px; }
table.fund-details th:nth-child(9), table.fund-details td:nth-child(9) { width: 90px; }
"""

# Browser-tab icon, embedded as a base64 SVG data URI so the report stays a
# single self-contained HTML file with no extra .ico asset to ship alongside it.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<text y="0.85em" font-size="90">\U0001f4c8</text></svg>'
)
FAVICON_HREF = "data:image/svg+xml;base64," + base64.b64encode(
    _FAVICON_SVG.encode("utf-8")
).decode("ascii")


def ratio_cell(values: list[float | None], i: int) -> tuple[str, str]:
    """Cell formatter for the ratio table: green/red depending on whether
    the ratio increased or decreased versus the next (older) date column.
    The oldest column (no next value to compare against) is left black."""
    value = values[i]
    if pd.isna(value):
        return "", COLOR_NEUTRAL
    text = f"{value:.3f}"
    next_value = values[i + 1] if i + 1 < len(values) else None
    if i == len(values) - 1 or next_value is None or pd.isna(next_value):
        color = COLOR_NEUTRAL
    elif value > next_value:
        color = COLOR_INCREASE
    else:
        color = COLOR_DECREASE
    return text, color


def rate_cell(values: list[float | None], i: int) -> tuple[str, str]:
    """Cell formatter for the annualized-return table: a signed percentage
    colored green/red by its own sign, or a dash where there's no rate
    (the 0.00/today column, which has no holding period to annualize)."""
    value = values[i]
    if value is None or pd.isna(value):
        return "-", COLOR_NEUTRAL
    color = (
        COLOR_INCREASE if value > 0 else COLOR_DECREASE if value < 0 else COLOR_NEUTRAL
    )
    return f"{value:+.1f}%", color


def highlight_terms(text: str, terms: list[str], css_class: str) -> str:
    """Wrap the first whole-word/whole-phrase match from `terms` found in
    `text` with a <span class="css_class">, leaving the rest untouched.
    Word-boundary matching avoids false hits like "EUR" inside "EURO"."""
    for term in terms:
        match = re.search(rf"\b{re.escape(term)}\b", text)
        if match:
            return (
                text[: match.start()]
                + f'<span class="{css_class}">{match.group()}</span>'
                + text[match.end() :]
            )
    return text


def render_name(name: str) -> str:
    """Build the HTML for the name cell: the plain name, HTML-escaped, with
    company/type/currency/acc-dist terms wrapped in colored spans."""
    text = html.escape(name)
    text = highlight_terms(text, COMPANIES, "company-name")
    text = highlight_terms(text, FUND_TYPES, "fund-type")
    text = highlight_terms(text, CURRENCIES, "currency")
    text = highlight_terms(text, ACC_DIST_TERMS, "acc-dist")
    return text


def render_fund_details_table(isins: list[str], anchor: datetime) -> str:
    """Build the flat "Fund details" reference table: one row per ISIN. Age
    is computed from START_DATE relative to `anchor` rather than stored, so
    it never goes stale between runs (Start Date itself isn't shown - Age
    already conveys it more compactly)."""
    headers = [
        "ISIN", "Name", "Ccy", "Fund size", "Policy", "Repl.",
        "TER", "Holdings", "Age (yrs)",
    ]
    lines = ['<table class="fund-details">', "  <thead>", "    <tr>"]
    lines += [f"      <th>{html.escape(h)}</th>" for h in headers]
    lines += ["    </tr>", "  </thead>", "  <tbody>"]

    for isin in isins:
        start_date = datetime.strptime(START_DATE[isin], "%Y-%m-%d")
        age = round((anchor - start_date).days / 365.25, 1)
        lines.append("    <tr>")
        lines.append(f"      <td>{html.escape(isin)}</td>")
        lines.append(
            f"      <td><details><summary>{render_name(NAMES[isin])}</summary>"
            f"{html.escape(INDEX_DESCRIPTION[isin])}</details></td>"
        )
        for value in (
            CURRENCY[isin], FUND_SIZE[isin], POLICY[isin], REPLICATION[isin],
            TER[isin], HOLDINGS[isin], f"{age:g}",
        ):
            lines.append(f"      <td>{html.escape(value)}</td>")
        lines.append("    </tr>")

    lines += ["  </tbody>", "</table>"]
    return "\n".join(lines)


def render_html_table(
    df: pd.DataFrame,
    id_columns: list[str],
    value_headers: list[str],
    date_headers: list[str],
    cell_fn: Callable[[list[float | None], int], tuple[str, str]],
    tooltips: dict[str, str] | None = None,
    tooltip_column: str = "name",
) -> str:
    """Build an HTML table for `df` with a spanning "years ago" header row
    above `value_headers`, followed by a `date_headers` sub-row (the nominal
    lookback date for each value column). `cell_fn(values, i)` formats each
    value column, returning (text, color) for the i-th value in that row.

    If `tooltips` is given (ISIN -> tooltip text), it's attached as a
    `title` attribute to each row's `tooltip_column` cell, shown by the
    browser on hover."""
    lines = [
        "<table>",
        "  <thead>",
        "    <tr>",
        f'      <th colspan="{len(id_columns)}"></th>',
        f'      <th colspan="{len(value_headers)}">years ago</th>',
        "    </tr>",
        "    <tr>",
    ]
    for col in id_columns + value_headers:
        lines.append(f"      <th>{html.escape(col)}</th>")
    lines.append("    </tr>")

    lines.append("    <tr>")
    for _ in id_columns:
        lines.append('      <th class="date-row"></th>')
    for date in date_headers:
        lines.append(f'      <th class="date-row">{html.escape(date)}</th>')
    lines.append("    </tr>")

    lines += ["  </thead>", "  <tbody>"]

    for _, row in df.iterrows():
        lines.append("    <tr>")
        for col in id_columns:
            tooltip = (
                tooltips.get(row["isin"])
                if tooltips and col == tooltip_column
                else None
            )
            title_attr = f' title="{html.escape(tooltip)}"' if tooltip else ""
            cell_html = (
                render_name(str(row[col]))
                if col == "name"
                else html.escape(str(row[col]))
            )
            lines.append(f"      <td{title_attr}>{cell_html}</td>")

        values = [row[h] for h in value_headers]
        for i in range(len(values)):
            text, color = cell_fn(values, i)
            lines.append(f'      <td style="color:{color}">{text}</td>')
        lines.append("    </tr>")

    lines += ["  </tbody>", "</table>"]
    return "\n".join(lines)


def init_db(conn: sqlite3.Connection) -> None:
    """Create the price cache tables if they don't already exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT,
            date TEXT,
            high REAL,
            low REAL,
            is_definitive INTEGER,
            PRIMARY KEY (ticker, date)
        )
    """)
    # Tracks the [range_start, range_end) actually requested from Yahoo for
    # each ticker so far - not just the min/max of returned rows, since a
    # requested end date can fall on a weekend/holiday or in the future
    # (this script always requests a bit past today) and would then never
    # be matched by any real trading day in `prices`.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_status (
            ticker TEXT PRIMARY KEY,
            range_start TEXT,
            range_end TEXT
        )
    """)
    conn.commit()


def delete_orphaned_provisional_rows(conn: sqlite3.Connection, today_str: str) -> int:
    """Delete every provisional (is_definitive=0) row dated before today.

    A provisional row is normally healed into a definitive one by the next
    day's fetch re-downloading that date with its final High/Low. But if
    Yahoo Finance stops returning that date at all - e.g. it briefly served
    a same-day quote for a non-trading holiday, then dropped it from history
    once the session closed with no actual trades - the row is never
    revisited by store_prices and would otherwise stay provisional forever,
    permanently flagging that ticker as stale on every future run. Once a
    provisional row is in the past, it's already had its one chance to be
    confirmed; if it wasn't, waiting longer won't help; only deleting it
    clears the flag. Returns the number of rows deleted."""
    cursor = conn.execute(
        "DELETE FROM prices WHERE is_definitive = 0 AND date < ?", (today_str,)
    )
    conn.commit()
    return cursor.rowcount


def get_fetch_status(
    conn: sqlite3.Connection, ticker: str
) -> tuple[str | None, str | None]:
    """Return the [range_start, range_end) already requested from Yahoo
    for `ticker`, or (None, None) if nothing's been fetched yet."""
    row = conn.execute(
        "SELECT range_start, range_end FROM fetch_status WHERE ticker = ?", (ticker,)
    ).fetchone()
    return row if row else (None, None)


def update_fetch_status(
    conn: sqlite3.Connection, ticker: str, start: str, end: str
) -> None:
    """Record that [start, end) has been requested for `ticker`, merging
    with whatever was already covered so the tracked range only grows."""
    old_start, old_end = get_fetch_status(conn, ticker)
    new_start = min(start, old_start) if old_start else start
    new_end = max(end, old_end) if old_end else end
    conn.execute(
        "INSERT OR REPLACE INTO fetch_status (ticker, range_start, range_end) "
        "VALUES (?, ?, ?)",
        (ticker, new_start, new_end),
    )
    conn.commit()


def has_stale_row(conn: sqlite3.Connection, ticker: str, today_str: str) -> bool:
    """True if `ticker` has a provisional row for a date before today -
    left over from a previous day's run, needing a refresh to pick up its
    now-final High/Low."""
    row = conn.execute(
        "SELECT 1 FROM prices WHERE ticker = ? AND is_definitive = 0 "
        "AND date < ? LIMIT 1",
        (ticker, today_str),
    ).fetchone()
    return row is not None


def load_mid_series_from_cache(
    conn: sqlite3.Connection, ticker: str, start: str, end: str
) -> pd.Series | None:
    """Build the same (High+Low)/2 Series shape as fetch_mid_series, but
    read from the local cache instead of the network."""
    rows = conn.execute(
        "SELECT date, high, low FROM prices WHERE ticker = ? "
        "AND date >= ? AND date < ? ORDER BY date",
        (ticker, start, end),
    ).fetchall()
    if not rows:
        return None
    dates = pd.to_datetime([r[0] for r in rows])
    mids = [(r[1] + r[2]) / 2 for r in rows]
    return pd.Series(mids, index=dates)


def store_prices(
    conn: sqlite3.Connection, ticker: str, data: pd.DataFrame, today_str: str
) -> None:
    """Upsert every row of a freshly downloaded OHLC DataFrame into the
    cache. Today's row is stored provisional (is_definitive=0); every
    other (closed) day is stored definitive (is_definitive=1)."""
    rows = [
        (
            ticker,
            date.strftime("%Y-%m-%d"),
            float(row["High"]),
            float(row["Low"]),
            0 if date.strftime("%Y-%m-%d") == today_str else 1,
        )
        for date, row in data.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO prices (ticker, date, high, low, is_definitive) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def fetch_mid_series(
    conn: sqlite3.Connection,
    ticker: str,
    start: str,
    end: str,
    today_str: str,
    stats: dict[str, int],
) -> pd.Series | None:
    """Return a Series of mid prices (average of High and Low) indexed by
    date for `ticker` over [start, end), or None if nothing is available.

    Serves from the local SQLite cache when it already covers the
    requested range and has no stale (pre-today, still-provisional) rows;
    otherwise downloads from Yahoo Finance and caches the result. `stats` is
    incremented in place: stats["cached"] for a ticker fully served from the
    local cache, stats["fetched"] for one requiring a fresh Yahoo download."""
    fetched_start, fetched_end = get_fetch_status(conn, ticker)
    range_covered = (
        fetched_start is not None and fetched_start <= start and fetched_end >= end
    )
    if range_covered and not has_stale_row(conn, ticker, today_str):
        stats["cached"] += 1
        return load_mid_series_from_cache(conn, ticker, start, end)

    stats["fetched"] += 1
    data = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
    if data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    store_prices(conn, ticker, data, today_str)
    update_fetch_status(conn, ticker, start, end)
    return load_mid_series_from_cache(conn, ticker, start, end)


def mid_on_or_after(mid_series: pd.Series | None, target_date: datetime) -> float | None:
    """Return the mid price on `target_date`, or the next available trading
    day within MAX_FORWARD_FILL_DAYS. Returns None if nothing is found."""
    if mid_series is None:
        return None
    for delta in range(MAX_FORWARD_FILL_DAYS + 1):
        ts = pd.Timestamp(target_date + timedelta(days=delta))
        if ts in mid_series.index:
            return float(mid_series.loc[ts])
    return None


def mid_on_or_before(mid_series: pd.Series | None, target_date: datetime) -> float | None:
    """Return the mid price on `target_date`, or the most recent prior
    trading day within MAX_BACKWARD_FILL_DAYS. Returns None if nothing is
    found - used for the anchor date, which can't be forward-filled since
    that would mean looking into the future."""
    if mid_series is None:
        return None
    for delta in range(MAX_BACKWARD_FILL_DAYS + 1):
        ts = pd.Timestamp(target_date - timedelta(days=delta))
        if ts in mid_series.index:
            return float(mid_series.loc[ts])
    return None


if __name__ == "__main__":

    # UTC throughout - both the anchor date used for price fetching/caching
    # and the displayed "Last update" timestamp derive from this single
    # `now`, so they can never disagree. This also matches the environment
    # that actually matters: the scheduled GitHub Actions run (run.yml) is
    # cron-scheduled in UTC on a UTC-clocked runner, so there's no local
    # timezone to reconcile with there.
    now = datetime.now(timezone.utc)
    # Naive (tzinfo stripped) - anchor is compared against/combined with the
    # naive dates used throughout the fetching/caching pipeline (the SQLite
    # cache's plain "YYYY-MM-DD" strings, yfinance/pandas' naive DatetimeIndex),
    # so it has to stay naive too; only its calendar day is UTC-based now.
    anchor = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    target_date: str = anchor.strftime("%Y-%m-%d")
    target_date_detailed: str = (
        now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d} UTC"
    )

    # Nominal lookback dates, shared across all tickers.
    lookback_dates = {
        months: anchor - timedelta(days=round(months * DAYS_PER_MONTH))
        for months in LOOKBACK_MONTHS
    }
    # Column headers express years ago (0, 0.25, 0.5, 1, 2, 3, 4, 5) - no
    # trailing decimals for whole years, matching the reference-date text below.
    years_ago_headers = {
        months: f"{round(months / 12, 2):g}" for months in [0] + LOOKBACK_MONTHS
    }
    # Nominal lookback date (YYYY-MM) shown as a sub-header for each
    # years-ago column - same nominal date for every ticker, distinct from
    # the actual resolved trading day used per ticker.
    date_headers = {0: anchor.strftime("%Y-%m")}
    date_headers.update(
        {months: date.strftime("%Y-%m") for months, date in lookback_dates.items()}
    )

    history_start = (
        min(lookback_dates.values()) - timedelta(days=MAX_FORWARD_FILL_DAYS)
    ).strftime("%Y-%m-%d")
    history_end = (anchor + timedelta(days=MAX_FORWARD_FILL_DAYS + 1)).strftime(
        "%Y-%m-%d"
    )

    months_list = [0] + LOOKBACK_MONTHS

    conn = sqlite3.connect(CACHE_DB_PATH)
    init_db(conn)
    deleted_orphans = delete_orphaned_provisional_rows(conn, target_date)
    if deleted_orphans:
        print(
            f"Cleaned up {deleted_orphans} orphaned provisional price row(s) "
            "that Yahoo Finance never confirmed."
        )

    fetch_stats = {"fetched": 0, "cached": 0}
    results = []
    rate_results = []
    for isin, name in NAMES.items():
        ticker = TICKERS[isin]
        print(f"{isin} - {name} ({ticker})")

        mid_series = fetch_mid_series(
            conn, ticker, history_start, history_end, target_date, fetch_stats
        )

        raw = {0: mid_on_or_before(mid_series, anchor)}
        for months, date in lookback_dates.items():
            raw[months] = mid_on_or_after(mid_series, date)

        # Normalize every value against the oldest lookback mid, so that
        # column reads 1.0 for every row and the others show growth
        # relative to the start of the window (a rebased index).
        oldest_month: float = max(LOOKBACK_MONTHS)
        reference_value = raw[oldest_month]
        row = {"isin": isin, "name": name, "ticker": ticker}
        for months, value in raw.items():
            row[years_ago_headers[months]] = (
                round(value / reference_value, 3)
                if value is not None and reference_value
                else None
            )
        results.append(row)

        # Annualized return from each lookback date to today:
        # (today / then) ** (1 / years_ago) - 1. This is the standard
        # trailing-return reading ("if held from then to now, what
        # annualized return would that be"), so every column shares the
        # same endpoint (today) rather than comparing to its neighbor.
        today_value = raw[0]
        rate_row = {"isin": isin, "name": name, "ticker": ticker}
        for months in months_list:
            if months == 0:
                rate = None
            else:
                older = raw[months]
                years_ago = months / 12
                rate = (
                    round(((today_value / older) ** (1 / years_ago) - 1) * 100, 1)
                    if today_value is not None and older
                    else None
                )
            rate_row[years_ago_headers[months]] = rate
        rate_results.append(rate_row)

    conn.close()

    print(
        f"\nData source: {fetch_stats['fetched']} of {len(TICKERS)} ETFs fetched "
        f"fresh from Yahoo Finance, {fetch_stats['cached']} served from the local "
        f"cache ({CACHE_DB_PATH})."
    )

    df = pd.DataFrame(results)
    df_rates = pd.DataFrame(rate_results)
    print(f"\nLast update: {target_date_detailed}")
    print(
        f"=== Summary (mid prices, normalized to {round(oldest_month/12,2):g} "
        "years ago) ==="
    )
    print(df.to_string(index=False))

    df.to_csv("etf_values.csv", index=False)

    id_columns = ["isin", "name", "ticker"]
    value_headers = [years_ago_headers[months] for months in [0] + LOOKBACK_MONTHS]
    date_header_list = [date_headers[months] for months in [0] + LOOKBACK_MONTHS]
    tooltips = {
        isin: f"Size: {FUND_SIZE[isin]}\nTracks: {INDEX_DESCRIPTION[isin]}"
        for isin in NAMES
    }

    with open("etf_values.html", "w", encoding="utf-8") as f:
        f.write(
            '<html><head><meta charset="utf-8">'
            f"<title>ETFs - {target_date}</title>"
            f'<link rel="icon" href="{FAVICON_HREF}">'
            f"<style>{HTML_STYLE}</style></head><body>\n"
        )
        f.write(f"<p>Last update: {target_date_detailed}</p>\n")
        f.write(
            f"\n<h2>Price ratios (normalized to {round(oldest_month/12,2):g} "
            "years ago)</h2>\n"
        )
        f.write(
            '<p style="color:#666; font-style:italic; max-width:1000px;">'
            "Each value is the price ratio: the fund's price on that "
            f"date divided by its price {round(oldest_month/12,2):g} years "
            "ago. Each cell is colored green or red depending on whether "
            "the price rose or fell compared to the previous point in the row.</p>\n"
        )
        f.write(
            render_html_table(
                df,
                id_columns,
                value_headers,
                date_header_list,
                ratio_cell,
                tooltips=tooltips,
            )
        )
        f.write("\n<h2>Annualized returns (to present day)</h2>\n")
        f.write(
            '<p style="color:#666; font-style:italic; max-width:1000px;">'
            "Each value is the annualized return: the compounded yearly "
            "growth rate that the fund would have provided if bought at "
            "that point in the past and held until today.</p>\n"
        )
        f.write(
            render_html_table(
                df_rates,
                id_columns,
                value_headers,
                date_header_list,
                rate_cell,
                tooltips=tooltips,
            )
        )
        f.write("\n<h2>Fund details</h2>\n")
        f.write(render_fund_details_table(list(TICKERS), anchor))
        f.write(f'\n<p style="color:#666; font-style:italic;">{FUND_DETAILS_SOURCE_NOTE}</p>\n')
        f.write("\n</body></html>\n")
