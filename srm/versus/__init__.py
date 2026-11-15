"""Blind comparison of several versions of one song (A/B, A/B/C, ... by the number of files).

``core`` holds the logic (no GUI), ``store`` the resumable state, ``player`` the gap-free
switching mixer and ``gui`` the Russian desktop interface. Source audio is only read until the
user explicitly applies a result.
"""
