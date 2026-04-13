# order-service - System Requirements

## Executive Summary

| Attribute | Value |
|-----------|-------|
| **Name** | order-service |
| **Type** | new |
| **Responsibility** | Order processing |

## Business Capabilities

| ID | Capability | Priority |
|----|------------|----------|
| BC-001 | Order creation | P0 |

## API Requirements

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/v1/orders | Create order |
| GET | /api/v1/orders/{id} | Get order |
