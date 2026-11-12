# EU water & nature policy calendar

A calendar that updates itself every morning with meetings, votes, deadlines
and updates on:

Nature Restoration Regulation · Water Framework Directive · Habitats Directive ·
Omnibus VIII (environmental assessments) · Water Resilience Strategy ·
European Water Initiative

**Subscribe in Outlook:** `https://YOUR-USERNAME.github.io/REPOSITORY-NAME/events.ics`
**Did the last run work?** `https://YOUR-USERNAME.github.io/REPOSITORY-NAME/status.html`

## How events are labelled

| Label | Meaning |
|---|---|
| `[WFD]`, `[NRR]`, `[Omnibus VIII]`… | Mentions one of your files |
| `[related]` | Mentions a related topic (rivers, wetlands, drought, biodiversity…) |
| no label | Always shown (e.g. Environment Council, plenary weeks, elections) |

## Sources

Commission calendar · EP plenary dates · ENVI committee agendas ·
EP Think Tank events · Council meetings · Coreper I agendas ·
Have your say deadlines · Legislative Train (WFD revision, Omnibus VIII,
Water Resilience Strategy) · elections and key dates you add by hand.

## Changing things

| To… | Edit |
|---|---|
| add/remove keywords or files | `config.yaml` → `files:` |
| switch a source off | `config.yaml` → `enabled: false` |
| add an election or key date | `manual_events.yaml` |

Edit on GitHub with the pencil icon, then **Commit changes**. The next
morning's run picks it up (or run it now: **Actions → Update policy calendar →
Run workflow**).

## If something breaks

Open `status.html`. A ❌ means that website changed or was down; its events
from the previous successful run stay in the calendar. Copy the error text to
Claude to get a fix.
