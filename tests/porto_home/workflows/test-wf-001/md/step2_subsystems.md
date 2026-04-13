# Subsystem Identification

## Overview

Based on business requirements analysis, 2 subsystems were identified.

### order-service

| Attribute | Value |
|-----------|-------|
| **Type** | new |
| **Responsibility** | Order processing and management |

Dependencies: payment-gateway

### payment-gateway

| Attribute | Value |
|-----------|-------|
| **Type** | extend |
| **Responsibility** | Payment processing integration |

Dependencies: none
