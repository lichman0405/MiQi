"""Structured runtime events for desktop-backend and future consumers.

This module defines typed event models and an ``EventEmitter`` that can be
subscribed to independently of the existing ``MessageBus``/``OutboundMessage``
path used by CLI and gateway.  Events are emitted *alongside* normal bus
traffic — they never replace it.
"""
