# Payment API conventions

- REST endpoints use the `/api/v1` prefix.
- Payment creation accepts an `Idempotency-Key` request header.
- Errors use JSON objects with `code` and `message` fields.
- Timestamps are ISO 8601 UTC strings.
