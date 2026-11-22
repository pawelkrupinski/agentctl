"""agentctl's code: real modules (2026-11-22, the owner: "make the parts real modules").

Each module imports the standard library and the modules before it in MODULES, explicitly, and nothing else: a name
is the module's own or imported. The entry (~/bin/agentctl) imports cli, the composition root: it wires what a module
must not import (`wire_store(process_store)`, the probes, ...) and holds main. cli's imports reach every module.

MODULES is every module in import order, each after every module it imports (devtools/placemodules.py writes it): the
manifest the quality tools (quality/typecheck.py) and the tests' view of the package (tests/agentctl_view.py) read.
"""
MODULES = ("usage", "core", "timefmt", "statefile", "moviestools", "classify", "store", "selfcode", "ghapp", "forge",
           "repos", "buildview", "policy", "notifyurl", "config", "workflows", "trailers", "waitingdir",
           "redretired", "storetoken", "redrecord", "claudefiles", "lander", "gating", "allowedpaths", "procs",
           "leases", "fixerrecords", "inboxentry", "wait", "land", "cpu", "fixersession", "live", "owned", "stats",
           "clones", "worktrees", "fixerenv", "explorer", "queueslots", "redfixread", "reverts", "deletions",
           "dispatches", "machine", "govtickets", "testtmp", "procwatch", "inbox", "jvmreap", "maint", "cleanup",
           "conditions", "notifier", "upkeep", "queue", "patches", "generated", "conflicts", "fixchange",
           "queuerecheck", "redlink", "redfixes", "redworkflows", "redreplay", "issues", "identitychange",
           "changekind", "redrepos", "redfamilies", "cistate", "owners", "redask", "alerts", "projector",
           "laptoprole", "fixerland", "fixer", "govbudget", "gov", "govcmd", "queueuntil", "ancestry", "red",
           "redmove", "waiting", "queuesubmit", "ciwhy", "branches", "redseen", "redobserve", "reduntracked",
           "landwatch", "redblock", "rundispatch", "blameopen", "redopen", "redearly", "redclaim", "redverify",
           "redcrash", "fixerlife", "owner", "redvault", "ask", "checkslot", "redpending", "remote", "remotestore",
           "queuebatch", "blame", "reddispatch", "redstep", "queuediff", "redview", "cireport", "watchtick",
           "queueclose", "tunnel", "launchd", "recovery", "queuepush", "storestate", "localrecheck", "netwatch",
           "tools", "sessionpaths", "superseded", "park", "orphans", "beats", "maintain", "redhold", "invariants",
           "watchdog", "cliops", "sdnotify", "storeauth", "serverclone", "batchitem", "landhold", "batchpush",
           "batch", "batchcheck", "tickrun", "server", "snapshot", "tickservice", "landqueue", "statusagents",
           "status", "statuspage", "redstatus", "redtick", "syncsteps", "serversteps", "queueenter", "slots",
           "cliparser", "processor", "cli")
