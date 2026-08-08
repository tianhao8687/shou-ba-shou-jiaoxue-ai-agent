# Harbor AgentOps Design System

## Direction

Operational control tower inspired by Xiamen Harbor navigation charts: white instrument surfaces, navy typography, marine blue actions, green completed routes, and amber safety gates. The interface should feel precise and calm under pressure.

## Tokens

| Role | Value |
|---|---|
| Navy 950 | `#061A3B` |
| Marine blue | `#0867E8` |
| Success green | `#07875B` |
| Approval amber | `#ED650C` |
| Failure red | `#DC3545` |
| Canvas | `#F5F8FC` |
| Panel | `rgba(255,255,255,.96)` |
| Border | `#D9E2EF` |
| Body text | `#52647F` |

Typography uses Segoe UI Variable / PingFang SC for readable Chinese UI and Cascadia Code for IDs, coordinates, timing, tool names, and metrics.

## Component rules

- Cards use 8–11px radii, thin blue-gray borders, and restrained depth.
- Primary actions are marine blue; orange is reserved for approval and operational risk.
- Every status combines color with text/icon. Color is never the only signal.
- Workflows remain horizontally scrollable on phones; the page itself must not overflow.
- Data tables have their own scroll containers below 760px.
- All focus states are visible and all clickable controls use native semantic elements.
- Motion is limited to loading, radar sweep, and small state transitions; reduced-motion is respected.
- Icons come from Lucide; emojis are not used as interface icons.

