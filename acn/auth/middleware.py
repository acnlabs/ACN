"""
ACN Auth0 Middleware

Simplified Auth0 integration for ACN (reuses Backend auth module logic).
"""

import os
import sys

# Import from Backend auth module
backend_path = os.path.join(os.path.dirname(__file__), "../../../backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

try:
    from app.auth.auth0 import (
        get_subject,
        require_permission,
        verify_token,
    )
except ImportError:
    # Fallback: define stub functions for development without Auth0
    from fastapi import Depends
    
    async def verify_token() -> dict:
        """Stub: Returns empty payload when Auth0 is not available"""
        return {"sub": "system@clients", "permissions": ["agent:admin"]}
    
    def require_permission(permission: str):
        """Stub: Always grants permission when Auth0 is not available"""
        async def permission_checker() -> dict:
            return await verify_token()
        return permission_checker
    
    async def get_subject() -> str:
        """Stub: Returns 'system' owner when Auth0 is not available"""
        return "system"


__all__ = [
    "verify_token",
    "require_permission",
    "get_subject",
]

