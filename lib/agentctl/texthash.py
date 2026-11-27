# agentctl, part `texthash`: the sha1 hex digest of a text
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.



def hashlib_sha1(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode()).hexdigest()
