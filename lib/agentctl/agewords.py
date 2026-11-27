# agentctl, part `agewords`: a number of seconds as the largest whole unit: days, hours or minutes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.



def fmt_age(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 86400} d" if secs >= 86400 else f"{secs // 3600} h" if secs >= 3600 else f"{secs // 60} min"
