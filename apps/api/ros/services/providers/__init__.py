"""External provider clients (managed-backend provisioning).

Thin, network-I/O-isolated wrappers over third-party control-plane APIs used by
`services/backend_provisioning.py`. Kept out of the service layer so provisioning stays
unit-testable with a fake HTTP transport.
"""
