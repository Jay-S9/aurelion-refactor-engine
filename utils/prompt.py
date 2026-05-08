"""
Aurelion Refactor Engine - Confirmation Prompt
Safe, clear user confirmation for destructive operations.
"""

import sys


def confirm_action(message: str, default: bool = False) -> bool:
    """
    Display a Y/N confirmation prompt.
    Returns True if the user confirms, False otherwise.
    
    Args:
        message: Question to display to the user.
        default: Default answer if user just hits Enter.
                 False = default NO (safer for destructive ops).
    """
    suffix = " [y/N] " if not default else " [Y/n] "
    
    try:
        answer = input(f"\n  ⚡ {message}{suffix}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if not answer:
        return default

    return answer in ("y", "yes")
