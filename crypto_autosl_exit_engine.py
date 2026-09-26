"""
crypto_autosl_exit_engine.py
============================
Production implementation of AutoSL Exit Mechanisms for Crypto Trading:
1. Breakout Failed Exit (Early Invalidation / Phase 1 Validation Window)
2. Dual-Layer Trailing Stop Loss (Hard Exchange Stop + Soft Dynamic SL)
3. Fast Momentum Extension & Profit Ratchet

Thread-safe, tick-driven, and exchange-agnostic.
Direct drop-in for Crypto Spot and Perpetual Futures.
"""

from core.autosl_engine import CryptoPosition, AutoSLExitEngine

__all__ = ["CryptoPosition", "AutoSLExitEngine"]
