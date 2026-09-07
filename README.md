# MyWaterAdvisor for Home Assistant

[![HACS Custom][hacs-badge]][hacs-url]
[![License: MIT][license-badge]][license-url]

Custom Home Assistant integration for the **MyWaterAdvisor** customer portal
(the "Harmony Encore" water-meter platform used by [mywateradvisor2.com](https://mywateradvisor2.com)
and similar utility-branded portals).

Polls your water utility's portal hourly and exposes consumption, cost,
leak alerts, billing-cycle usage/forecast, and vacation mode as native Home
Assistant entities — with full history backfilled into the **Energy**
dashboard.

> **Disclaimer:** This integration talks to reverse-engineered, undocumented
> endpoints (see [`api.py`](custom_components/mywateradvisor/api.py) for the
> confirmed request/response shapes). It is not affiliated with or endorsed
> by MyWaterAdvisor, Harmony Encore, or Master Meter. Endpoints may change or
> break without notice.

## Features

- **Energy dashboard integration** — real hourly consumption and a computed
  cost backfilled as external statistics (`Water Meter Consumption` /
  `Water Meter Cost`), independent of the live entities so dashboard history
  survives reloads and account re-adds.
- **Leak and alert monitoring** — a `Leak Alert` binary sensor plus an
  `Active Alerts` count sourced from the portal's own alerting.
- **Vacation mode** — toggle a switch to schedule/cancel a vacation, or call
  a service for a specific date range and daily limit.
- **Billing-cycle tracking** — usage so far, an end-of-cycle forecast, and an
  editable consumption budget synced back to the portal.
- **Reauth support** — a failed login prompts Home Assistant's standard
  re-authentication flow instead of silently going unavailable.

## Installation

### HACS (recommended)

1. In HACS, go to **Integrations → ⋮ → Custom repositories**.
2. Add this repository URL with category **Integration**.
3. Install **MyWaterAdvisor**, then restart Home Assistant.

### Manual

1. Copy `custom_components/mywateradvisor` into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

Configuration is done entirely through the UI:

**Settings → Devices & Services → Add Integration → MyWaterAdvisor**

Enter the email and password you use to log into the MyWaterAdvisor portal.

## Entities

| Entity | Type | Notes |
|---|---|---|
| Total Usage | Sensor | Lifetime running total, gallons (`total_increasing`) |
| Daily Usage | Sensor | Resets at local midnight |
| Billing Cycle Usage | Sensor | Usage so far in the current billing cycle |
| Billing Cycle Forecast | Sensor | Portal's own end-of-cycle projection |
| Forecasted Cost | Sensor | Billing Cycle Forecast × Price Per Gallon |
| Neighborhood Average Usage | Sensor | Diagnostic — most recent completed month |
| Active Alerts | Sensor | Diagnostic — count of active portal alerts |
| Last Reading Time | Sensor | Diagnostic — timestamp of the last real reading |
| Serial Number | Sensor | Diagnostic |
| Service Address | Sensor | Diagnostic |
| Debug | Sensor | Diagnostic, **disabled by default** — raw last-poll payload |
| Leak Alert | Binary sensor | On when the portal reports a suspected leak |
| Billing Cycle Limit | Number | Editable consumption budget sent to the portal |
| Price Per Gallon | Number | Local $/gallon rate used only for the cost statistic |
| Vacation Mode | Switch | On schedules a 30-day vacation from today; off cancels it |

## Services

All three services accept an optional `device_id` to target one account;
omitted, they apply to every configured MyWaterAdvisor account.

| Service | Description |
|---|---|
| `mywateradvisor.schedule_vacation` | Schedule a specific start/end date and optional daily limit |
| `mywateradvisor.cancel_vacation` | Cancel a vacation by ID, or the currently active one |
| `mywateradvisor.clear_billing_cycle_limit` | Remove the configured consumption budget |

## Known limitations

- The portal's `avghouseholds` (Neighborhood Average) response shape is
  unconfirmed against a live account and is parsed defensively.
- The API is undocumented and reverse-engineered — see `scripts/diagnose_api.py`
  for a standalone credential/endpoint troubleshooting tool.

## License

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-orange.svg
[hacs-url]: https://github.com/hacs/integration
[license-badge]: https://img.shields.io/badge/license-MIT-blue.svg
[license-url]: LICENSE
