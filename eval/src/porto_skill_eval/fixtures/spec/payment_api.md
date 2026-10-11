# Payment API draft

`POST /payments` creates a payment. The request contains `merchant_id` and `amount`; the response contains `payment_id` and `status`. There is no idempotency field, error model, or measurable acceptance criterion.
