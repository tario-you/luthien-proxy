# Objective

Configure V2 gateway so Codex CLI can relay chat completions without invalid API key errors.

## Acceptance Checklist
- V2 configuration enables OpenAI provider and desired gateway auth mode.
- Gateway restart succeeds and health endpoint confirms readiness.
- Chat completions via Codex CLI succeed without 401/404 from proxy.

---
