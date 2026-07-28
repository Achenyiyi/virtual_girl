# Windows Read-only Actions

## Production boundary

The first Windows action provider intentionally exposes only three fixed operations:

| CLI command | Action type | Data returned |
| --- | --- | --- |
| `/action status` | `check_system_status` | OS, CPU count, memory, disk, uptime |
| `/action window` | `read_window_title` | Foreground window title |
| `/action app` | `read_active_app` | Foreground process name and PID |

It does not contain a shell runner and cannot open applications, read the clipboard, send input,
access arbitrary files, browse the network, send messages, install software, or change settings.
Requests with parameters, a mismatched method, a forged risk level, an unknown action type, or a
missing or foreign sandbox ID are rejected before provider execution. Preview and sandbox
verification may inspect an otherwise valid request without an ID; `execute()` may not.

The boundary is an immutable capability allowlist, not a Windows Sandbox virtual machine. It is
sufficient for these parameterless read-only Win32 queries. It must not be reused to justify
enabling file writes or any other mutating operation; those require a separate OS-isolated
provider and additional acceptance testing.

## Enabling the provider

Actions are disabled by default. Use a custom configuration only after deciding that foreground
window and process metadata are acceptable for the local user:

```yaml
providers:
  action:
    enabled: true
    type: windows_readonly
    sandbox_enabled: true
    audit_db_path: ./data/action_audit.db
    timeout_seconds: 5.0
    max_concurrent_actions: 1
    max_pending_confirmations: 10
    confirmation_ttl_seconds: 120.0
    max_text_characters: 4096
```

When enabled, the application fails startup if:

- the provider is not running on Windows or its Win32 probe fails;
- sandboxing is disabled in configuration;
- a durable audit path is missing or unavailable; or
- the existing append-only audit hash chain fails verification.

Every requested, executing, and executed stage is persisted before or around the provider
boundary. Action parameters and errors pass through the existing credential redaction layer.
Provider result data is not persisted in the audit ledger.

Any future confirmation-requiring provider inherits a bounded pending queue and a short
confirmation TTL. Expired confirmations are revoked and audited, and excess proposals are denied;
an approval can therefore never authorize an arbitrarily old request or grow memory without bound.

## Privacy and operational notes

Window titles can contain document names, websites, or conversation names. Results are shown only
in the current terminal session and aren't added to companion memory. Keep actions disabled on
shared machines unless this metadata exposure is acceptable.

The real-Windows integration test executes `check_system_status` and verifies its SQLite audit
chain. Additional tests prove that a mutating `open_app` request and tampered request envelopes do
not reach provider execution.
