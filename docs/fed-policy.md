# Fed policy: the decisions, the projections, the words and the balance sheet

Status: implemented 2026-08-17. `backend/extensions/fed.py` (the rate),
`backend/extensions/fed_signals.py` (everything else),
`backend/providers/fomc.py`, `bea_releases` in
`backend/providers/govstats.py`, the `fomc` and `fedspeak` event types in
`backend/extensions/calendar.py`, the Fed panel in the Markets view
(`frontend/`), tested in `tests/test_fed.py` and `tests/test_fed_signals.py`.

FRED publishes the fed funds target as a daily level. Nobody reads it that way.
The questions are *when did they move*, *by how much*, *how far has this cycle
travelled*, *how long have they been on hold*, and *what happened to my
portfolio last time they did this* — and none of those are answerable from a
line chart without doing the arithmetic by eye.

Nor is the rate the only thing that moves policy. Between eight meetings a year
sit the projections, the wording of the statement, the chair in front of
Congress, the balance sheet running off, and the facilities that only get used
when something breaks. All of it is published by the Fed itself.

`/economy/fed/*` is that surface:

| Command | What it answers |
|---|---|
| `policy_rate` | The level: target range, midpoint and the effective rate inside it |
| `rate_changes` | Every decision that moved the target since 1982, newest first |
| `cycles` | The runs those decisions group into, with how far and how long |
| `stance` | Where policy stands now, and what it took to get here |
| `meetings` | The FOMC's own schedule, joined to what each meeting did |
| `cycle_performance` | What assets returned across each cycle |
| `projections` | The SEP — GDP, unemployment, inflation, rates — against the previous one |
| `dot_plot` | Participants at each rate level, by year, with the median's shift |
| `statement` | The statement, its vote and dissents, its language, and what changed |
| `communications` | Speeches, congressional testimony and Board releases |
| `balance_sheet` | Size, composition and runoff pace |
| `liquidity` | Emergency lending facilities, and when they were used |
| `data_reaction` | The days the expected path repriced, and what landed on them |

## Splicing the two target eras

Before 16 December 2008 the FOMC set a single target (`DFEDTAR`). Since then it
sets a range (`DFEDTARL` / `DFEDTARU`). Neither series spans the record, so both
are read and the **upper bound carries the history** — the single target is its
own upper bound.

Changes are measured on that upper bound rather than the midpoint, because the
upper bound is the number the announcements are written in. December 2008 moved
policy from 1.00% to "0 to 0.25%":

- on the upper bound that is **−75bp**, which is what was reported at the time;
- on midpoints it is −87.5bp, which is arithmetically true and matches nothing
  anyone said.

The lower bound and midpoint come back on every row, so a caller who wants the
other convention has the inputs.

## A cycle is a run of moves in one direction

It starts at the first move, ends at the last one before the direction
reverses, and long holds *inside* it stay inside it. The 2015-2018 tightening
had twelve months between its first and second hike and is still one cycle,
because that is how it was lived and how it is written about.

The wait at the peak is reported separately as `hold_days` — the gap from a
cycle's last move to the first move of the next one. *How long from the last
hike to the first cut* is what most questions about a hiking cycle are actually
asking, and it is a property of the gap between two cycles rather than of
either one.

```
cycle                  moves  total_bps  from → to      months  hold_days
2022-2023 tightening      11     +525     0.25 → 5.50    16.3        420
2024-2025 easing           6     −175     5.50 → 3.75    14.7        249 (so far)
```

## Decisions are dated by when they take effect

A rate change is effective the morning after the statement, so a decision taken
on the afternoon of the 18th moves FRED's target series on the 19th.
`rate_changes` therefore reports *effective* dates, and `meetings` does the
join: each meeting is credited with a move landing within four days of it, and
a past meeting with no move reads `held` at the rate that was already in force
rather than blank.

That join is also what makes the calendar row useful — a past FOMC date reads
"Cut 25 bps to 3.50-3.75%" rather than just "FOMC meeting".

## The projections, and the dot plot as data

Four meetings a year publish the Summary of Economic Projections. The Fed's
accessible HTML carries two things the PDF screenshot does not: **Table 1**,
which folds the *previous* SEP in under each variable, and the **dot plot's own
table** — one row per rate level, one column per year, holding the number of
participants who put their dot there.

So `projections` returns each median beside the one it replaced, and `change`
is a subtraction rather than an impression:

```
variable              horizon      median  previous  change
Federal funds rate    2026            3.8       3.4    +0.4
Federal funds rate    2027            3.6       3.1    +0.5
Federal funds rate    Longer run      3.1       3.1     0.0
Core PCE inflation    2026            3.3       2.7    +0.6
```

`dot_plot` returns the distribution itself, and computes the median dot from
it. That figure can differ from the SEP's stated median by a rounding step —
with an even number of participants the true median falls between two dots
(3.75) while the Fed prints one decimal (3.8). Both are reported rather than
one being quietly preferred.

The `Longer run` row is the committee's own estimate of the neutral rate. It
moves rarely and it reprices everything when it does, which is why it is a
horizon here rather than a footnote.

Two parsing details are load-bearing and tested: a row labelled "March
projection" belongs to the variable *above* it, not to itself; and "Memo:
Projected appropriate policy path" is a spanning section header, which pandas
fills into every cell of the row.

## The statement, and what changed in it

Forward guidance is wording, and wording is only informative against the last
version. `statement` returns:

- the **text**, paragraph by paragraph;
- the **vote** and the **dissent** — "by a 9 – 3 vote", and the sentence naming
  who wanted what instead, which is the committee saying how close this was;
- **language flags**: which phrases from the hawkish, dovish and guidance lists
  appear, listed with counts rather than rolled into a score;
- the **sentence diff** against the previous statement.

The diff is sentence-level rather than word-level because the committee
rewrites clauses; a word diff of "will be" against "is" reads as a change in
guidance when it is a change in tense. The splitter knows not to break on
initials, or the dissent paragraph turns into "Beth M." / "Hammack, Neel
Kashkari, and Lorie K." / "Logan, who preferred…" and one edit reads as three.

## Between meetings

`communications` reads the Board's own feeds — speeches, testimony, monetary
press releases, banking and supervision notices. The speaker is parsed out of
the title ("Cook, Outlook for the U.S. Economy"), and three flags do the work a
reader would otherwise do by eye:

- `congressional` — testimony to the House or Senate, including the semiannual
  monetary policy report;
- `jackson_hole` — the Kansas City Fed's symposium;
- `off_calendar` — an FOMC statement issued on a day with no scheduled meeting,
  which is how an intermeeting move is announced.

No speaker is hardcoded. Who chairs the Fed changes; the command filters by
surname and reports the speakers it saw.

## The balance sheet and the facilities

`balance_sheet` is the second instrument: total assets with Treasuries, MBS,
reserves and the reverse repo facility underneath, plus the change over 4, 13
and 52 weeks. Quantitative tightening is a *runoff rate*, not an announcement,
so the pace column is the policy — a cap raised or lowered shows up here weeks
before anyone calls it a change. The regime label uses a deliberately wide
neutral band (±$20bn a month), because a portfolio this size drifts by a few
billion a month from currency in circulation alone.

`liquidity` is emergency lending, and it is a *usage* series rather than an
announcement: the discount window, the Bank Term Funding Program, other credit
extensions (the 2023 bridge banks), central bank swap lines and repo. March
2023 and March 2020 are legible in these numbers and nowhere else in the
platform. Each facility is flagged against its own 95th percentile, because the
baseline in a calm week is a rounding error.

One alignment detail: the H.4.1 is weekly and the repo facilities are daily.
Read together without care, the weekly frame becomes a mostly-empty daily one
and a "13-week change" silently becomes a 13-day change. The weekly release
sets the index; the daily series are read on those same Wednesdays.

## What this deliberately does not do

**Implied hike probabilities.** They come from fed funds futures, and CME's
FedWatch numbers are licensed data with no free feed. Rather than manufacture a
percentage out of something that is not futures, `stance` reports the market's
own rate: the 2-year Treasury yield against the target midpoint. A 2-year well
below the midpoint is the market pricing cuts; well above it, hikes. Same
directional read, no invented precision.

**Tone scoring of a press conference.** The transcripts are PDFs, and this
project has no PDF dependency. What is parseable — the statement — is the
document the committee actually voted on, so that is where the language flags
are applied. The press conference is flagged and linked on the meeting row.

**A causal claim about data days.** `data_reaction` lists the days the 2-year
Treasury moved more than a threshold and names the events that landed on them —
an FOMC decision, the minutes, testimony, a BEA release. The yield moved, and
the event happened; the link between them is left to the reader.

**CPI and jobs release dates, key-free.** The BLS blocks automated readers
outright (HTTP 403 on both the schedule pages and its own RSS), so those two
dates have no free source here. BEA's feed covers PCE and GDP; a free
`MFT_FRED_API_KEY` fills in the official US release calendar. Days with no
known event are still listed and marked `none` — a blank means "nothing this
platform can date", not "nothing happened".

**Meeting history before ~2021.** The Fed's calendar page carries roughly the
current year, the five before it and the one ahead. Older meetings live on
per-year archive pages that would need a scrape each; the decisions themselves
do not, because FRED's target series runs back to 1982. So `meetings` covers
the recent schedule and `rate_changes` covers the record.

## Reading the level series

`policy_rate` is daily by default. `frequency=w|m|q|a` thins it to period-end
observations for drawing long histories — the target is a step function, so the
last observation in a period *is* that period's rate. A move and a reversal
inside one period would collapse to the later of the two, which is why the
decisions always come from the daily series and never from the thinned one.

FRED ignores its own resample argument on a multi-series download (the request
comes back as a zip of one CSV per series, unresampled), so the thinning
happens here rather than upstream.

## On the calendar

Two event types come from the Fed rather than from either market feed — Yahoo's
macro calendar lists the release of a statistic, not the meeting that sets the
rate those statistics are read against.

- **`fomc`** — the decision day, with the 2pm ET statement time, importance 3
  and a detail line saying what the meeting did ("Cut 25 bps to 3.50-3.75%") or
  what it carries ahead of time (the dot plot, a press conference). The minutes
  release, three weeks later, is a row of its own on the same type.
- **`fedspeak`** — speeches, congressional testimony and Board press releases.
  Published records rather than a schedule, so these land after they happen;
  testimony, Jackson Hole and the policy documents rank as major, ordinary
  speeches below that.

The press feed announces the statement and the minutes too, so when both types
are selected the `fomc` row wins and the duplicate is dropped — except for a
statement dated away from a scheduled meeting, which is kept, because that one
is the news.

## In the UI

The Markets view has a Federal Reserve panel. The stance strip is always
visible — target range, effective rate, the last move and how long policy has
been on hold, the current cycle's travel, the real policy rate against core
PCE, the 2-year spread and the next meeting. Under it, four tabs:

- **Rate path** — the policy path over 5/10/25 years or the whole record, with
  tables of recent hikes and cuts and the cycles they belong to.
- **Projections & dots** — the SEP grid with a revision arrow per cell, and the
  dot plot drawn as participants at each level with the median's shift.
- **Balance sheet** — size and composition, the runoff pace, and the facilities
  table with each one's peak and whether it is being used now.
- **Statement & speeches** — the vote, the dissent, the language chips and the
  sentence diff, beside the feed of speeches and testimony.

Each tab fetches its own data the first time it is opened; a reader looking at
the rate path does not pay for the SEP, the H.4.1 and three RSS feeds.

## Related commands

`/fixedincome/rate/policy` returns the policy rates as raw FRED series (fed
funds, IORB, discount window, prime) — reach for that one for the levels and
this menu for the decisions. `/economy/central_bank_holdings` covers the other
half of policy, the balance sheet.
