#!/usr/bin/env python3

import secrets


# Helper Functions
def generate_api_key():
    return secrets.token_hex(32)
