"""agentctl's code: real modules (2026-11-22, the owner: "make the parts real modules").

Each module imports the standard library and the modules before it in MODULES, explicitly, and nothing else: a name
is the module's own or imported. The entry (~/bin/agentctl) imports cli, the composition root: it wires what a module
must not import (`wire_store(process_store)`, the probes, ...) and holds main. cli's imports reach every module.

MODULES is every module in import order, each after every module it imports (devtools/placemodules.py writes it): the
manifest the quality tools (quality/typecheck.py) and the tests' view of the package (tests/agentctl_view.py) read.
"""
MODULES = ("usage", "core", "timefmt", "statefile", "moviestools", "classify", "selfcode", "ghapp", "forge", "repos",
           "buildview", "notifyurl", "config", "workflows", "trailers", "waitingdir", "redretired", "storetoken",
           "claudefiles", "gating", "allowedpaths", "procs", "fixerrecords", "inboxentry", "wait", "cpu",
           "fixersession", "live", "owned", "clones", "worktrees", "fixerenv", "deletions", "machine", "govtickets",
           "testtmp", "procwatch", "inbox", "jvmreap", "maint", "notifier", "patches", "redworkflows", "redrepos",
           "redfamilies", "cistate", "govbudget", "ancestry", "ciwhy", "launchd", "beats", "sdnotify", "storeauth",
           "tickrun", "runsigs", "gatesummary", "dispatchblock", "statemachines", "store", "policy", "redrecord",
           "lander", "leases", "land", "stats", "explorer", "queueslots", "redfixread", "reverts", "dispatches",
           "cleanup", "conditions", "upkeep", "queue", "generated", "conflicts", "fixchange", "queuerecheck",
           "redlink", "redfixes", "redreplay", "issues", "identitychange", "changekind", "owners", "redask",
           "alerts", "projector", "laptoprole", "fixerland", "fixer", "gov", "govcmd", "queueuntil", "red",
           "redmove", "waiting", "queuesubmit", "landwatch", "rundispatch", "blameopen", "checkslot", "remotestore",
           "tunnel", "storestate", "sessionpaths", "serverclone", "landhold", "statusagents", "recordingpins",
           "branches", "redseen", "redobserve", "reduntracked", "redblock", "localrecheck", "cliops", "redauthors",
           "redopen", "redearly", "redclaim", "redverify", "redcrash", "fixerlife", "owner", "redvault", "ask",
           "redpending", "culpritfix", "blamerecords", "remotejobs", "remote", "queuebatch", "queuediff", "redprobe",
           "blame", "reddispatch", "redstep", "redview", "cireport", "watchtick", "redhold", "queueclose",
           "recovery", "queuepush", "netwatch", "tools", "superseded", "park", "orphans", "maintain", "invariants",
           "watchdog", "batchitem", "batchpush", "server", "snapshot", "status", "statuspage", "redstatus",
           "redtick", "syncsteps", "processorlink", "tickservice", "batchrun", "batch", "batchcheck", "landqueue",
           "serversteps", "queueenter", "slots", "cliparser", "processor", "cli")
