"""Shared contracts and infrastructure helpers.

Only cross-cutting code lives here (event envelope/payload schemas, broker
abstraction, outbox relay, logging, error format, JWT helpers). No service
owns data through this package.
"""
