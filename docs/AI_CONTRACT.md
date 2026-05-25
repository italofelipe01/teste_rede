# AI Contract

## Purpose
This contract defines non-negotiable engineering rules for AI-assisted changes in this repository.

## Forbidden Actions
- Do not implement domain fixes as ad-hoc UI conditionals.
- Do not hardcode special-case values for specific hops, hosts, or incidents in `dashboard.js` or `dashboard.html`.
- Do not bypass architecture layers to "quick-fix" output formatting or behavior.
- Do not ship incomplete work without lint/type/test validation for impacted scope.
- Do not silently change data contracts (CSV/API keys/units) without updating docs and decision log.

## Layer Responsibility Rule
- Domain logic must live in backend Python modules (`monitor_rota.py`, `portal_rede.py`) or dedicated domain/service modules created for that purpose.
- UI logic (`dashboard.js`, `dashboard.html`, `dashboard.css`) must only handle presentation, interaction, filtering, and rendering.
- If behavior correctness depends on business rules (outage detection, aggregation, metric semantics), implement in Domain/Service first.

## Testing and Quality Gates (Hard Requirement)
Before task completion:
- Syntax/type checks must pass for modified files.
- Functional checks for modified flows must pass (monitor loop, CSV output, API snapshot, dashboard parsing).
- No unresolved runtime errors in the changed path.

## Documentation Requirements
Any architectural or contract change requires:
- Update `DECISIONS.md` with rationale/trade-offs.
- Update `projectmap.md` if flow/layer boundaries changed.
- Add or update `specs/<feature_name>.md` for new core feature work before implementation.

## Delivery Integrity
- Prefer maintainable abstractions over one-off fixes.
- Keep units explicit (`ms`, `%`, `s`, `pkt`) across contracts and UI labels.
- Preserve backward compatibility when feasible; otherwise document migration impact.
